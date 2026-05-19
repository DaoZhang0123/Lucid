"""Skill repositories — opt-in catalogues the agent can pull SKILL.md files from.

A *repo* here is just a GitHub repository (or generic URL with a tree-walk
endpoint) the user has whitelisted as a source of trusted skills. Compare to
``install_skill_url`` in ``skills.py``, which downloads a SINGLE raw SKILL.md
the user pastes in — that path is marked ``source = "online"`` and the safety
policy treats the body with extra suspicion.

By contrast, anything pulled from an *enabled* repo is marked
``source = "repo"`` and treated like a user-installed skill (default enabled,
no untrusted banner) — because the user explicitly opted into the repo as a
whole. Repo enabling is the trust gate.

Storage:
    ~/.lucid/skill_repos.json          — repo list (presets + user-added)
    ~/.lucid/skill_repo_cache.json     — fetched index per repo (TTL)

The agent gains two meta tools (registered in ``meta_tools.py``):
    * ``search_skills(query)``        — search across enabled repos
    * ``install_repo_skill(name)``    — download + install a specific match

UI surface lives in ``app/src/routes/skills/+page.svelte``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import skills as skills_mod


_REPOS_FILENAME = "skill_repos.json"
_CACHE_FILENAME = "skill_repo_cache.json"
_CACHE_TTL_S = 6 * 3600   # re-fetch repo tree every 6 hours
_HTTP_TIMEOUT_S = 12
_MAX_TREE_ENTRIES = 4000  # GitHub tree-walk hard cap
_MAX_SKILL_BYTES = 64 * 1024
# Sibling-file download budget when pulling a whole skill folder
# (Anthropic skills like ``skills/pptx`` ship scripts/, reference docs, etc.
# next to SKILL.md, and the playbook is useless without them).
_MAX_SIBLING_FILES = 100
_MAX_SIBLING_FILE_BYTES = 1 * 1024 * 1024     # per file
_MAX_SIBLING_TOTAL_BYTES = 8 * 1024 * 1024    # cumulative per install


# ---------------------------------------------------------------------------
# Built-in presets (always present, default disabled).
# The user enables them with a checkbox in the Skills page.
# ---------------------------------------------------------------------------
_BUILTIN_REPOS: tuple[dict[str, Any], ...] = (
    {
        "id": "anthropics-skills",
        "name": "Anthropic Skills (official)",
        "url": "https://github.com/anthropics/skills",
        "description": (
            "Official Anthropic Agent Skills repository. Authoritative source "
            "for document-skills (pptx, docx, xlsx), data analysis, and "
            "internal-use playbooks."
        ),
        "builtin": True,
    },
    {
        "id": "obra-superpowers",
        "name": "Superpowers (community)",
        "url": "https://github.com/obra/superpowers",
        "description": (
            "Community skill collection by Jesse Vincent. Broad coverage of "
            "everyday developer / desktop workflows."
        ),
        "builtin": True,
    },
    {
        "id": "openclaw-skills",
        "name": "openclaw (community)",
        "url": "https://github.com/openclaw/openclaw/tree/main",
        "description": (
            "openclaw/openclaw — community skill collection. The `skills/` "
            "folder contains many high-quality SKILL.md playbooks; the "
            "tree-walk picks them up automatically."
        ),
        "builtin": True,
    },
)


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------

def _root() -> Path:
    p = Path.home() / ".lucid"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _repos_path() -> Path:
    return _root() / _REPOS_FILENAME


def _cache_path() -> Path:
    return _root() / _CACHE_FILENAME


def _short_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Repo list (CRUD)
# ---------------------------------------------------------------------------

def _load_state() -> dict[str, Any]:
    """Return the on-disk repo state, merged with built-in defaults.

    Built-in repos are always present (the user can't delete them, only
    enable/disable). User-added repos live alongside.
    """
    p = _repos_path()
    state: dict[str, Any] = {"repos": {}}
    builtin_ids = {r["id"] for r in _BUILTIN_REPOS}
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("repos"), dict):
                for k, v in raw["repos"].items():
                    if not isinstance(v, dict):
                        continue
                    # Drop stale built-in entries whose id no longer matches
                    # any currently-defined preset (e.g. preset was renamed
                    # or removed across a Lucid upgrade) — those persisted
                    # rows only carry `{"enabled": ...}` and would otherwise
                    # surface in the UI as fieldless ghosts.
                    is_persisted_builtin = set(v.keys()) <= {"enabled"}
                    if is_persisted_builtin and k not in builtin_ids:
                        continue
                    state["repos"][k] = v
        except (OSError, ValueError):
            pass
    # Merge built-ins (preserve their saved ``enabled`` flag if present;
    # default to enabled on first install so users don't have to discover the
    # toggle before ``search_skills`` returns anything).
    for preset in _BUILTIN_REPOS:
        rid = preset["id"]
        prev = state["repos"].get(rid) or {}
        state["repos"][rid] = {
            **preset,
            "enabled": bool(prev.get("enabled", True)),
        }
    return state


def _save_state(state: dict[str, Any]) -> None:
    # Don't persist built-in metadata that's hard-coded; only the
    # ``enabled`` flag (so future code updates to ``_BUILTIN_REPOS``
    # propagate cleanly).
    out: dict[str, Any] = {"repos": {}}
    for rid, repo in state.get("repos", {}).items():
        if not isinstance(repo, dict):
            continue
        if repo.get("builtin"):
            out["repos"][rid] = {"enabled": bool(repo.get("enabled", False))}
        else:
            out["repos"][rid] = {
                "id": rid,
                "name": str(repo.get("name") or "").strip(),
                "url": str(repo.get("url") or "").strip(),
                "description": str(repo.get("description") or "").strip(),
                "enabled": bool(repo.get("enabled", True)),
                "builtin": False,
            }
    tmp = _repos_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _repos_path())


def list_repos() -> list[dict[str, Any]]:
    """Return all repos (built-ins + user-added), built-ins first."""
    state = _load_state()
    items = list(state["repos"].values())
    items.sort(key=lambda r: (0 if r.get("builtin") else 1, (r.get("name") or "").lower()))
    return items


def _normalise_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        raise ValueError("url required")
    if not (u.startswith("http://") or u.startswith("https://")):
        raise ValueError("only http(s) URLs are accepted")
    return u.rstrip("/")


def add_repo(url: str, name: str = "", description: str = "") -> dict[str, Any]:
    """Add a user-defined repo. Returns the new repo dict."""
    u = _normalise_url(url)
    state = _load_state()
    # Dedup by URL.
    for rid, repo in state["repos"].items():
        if (repo.get("url") or "").rstrip("/") == u:
            raise ValueError(f"repo already exists: {u}")
    rid = "u-" + _short_id(u)
    state["repos"][rid] = {
        "id": rid,
        "name": (name or _infer_repo_name(u)).strip(),
        "url": u,
        "description": (description or "").strip(),
        "enabled": True,    # user-added defaults to enabled (opt-in by action)
        "builtin": False,
    }
    _save_state(state)
    _invalidate_cache(rid)
    return state["repos"][rid]


def delete_repo(rid: str) -> bool:
    """Delete a user-added repo. Built-ins are kept; only disabled if asked."""
    state = _load_state()
    repo = state["repos"].get(rid)
    if not repo:
        return False
    if repo.get("builtin"):
        # Built-ins can only be disabled, not deleted.
        repo["enabled"] = False
    else:
        state["repos"].pop(rid, None)
    _save_state(state)
    _invalidate_cache(rid)
    return True


def set_repo_enabled(rid: str, enabled: bool) -> dict[str, Any] | None:
    state = _load_state()
    repo = state["repos"].get(rid)
    if not repo:
        return None
    repo["enabled"] = bool(enabled)
    _save_state(state)
    if not enabled:
        _invalidate_cache(rid)
    return repo


def _infer_repo_name(url: str) -> str:
    parts = urllib.parse.urlparse(url)
    path = (parts.path or "").strip("/").split("/")
    if len(path) >= 2:
        return f"{path[0]}/{path[1]}"
    return parts.netloc or url


# ---------------------------------------------------------------------------
# GitHub repo introspection
# ---------------------------------------------------------------------------

_GH_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)(?:/tree/(?P<branch>[^/]+))?/?$",
    re.IGNORECASE,
)


def _parse_github(url: str) -> tuple[str, str, str | None] | None:
    """Parse ``https://github.com/<owner>/<repo>[/tree/<branch>]``."""
    m = _GH_URL_RE.match((url or "").strip().rstrip("/"))
    if not m:
        return None
    return m.group("owner"), m.group("repo"), m.group("branch")


def _http_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Lucid-SkillRepos/0.1",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # nosec
        data = resp.read(2 * 1024 * 1024)
    return json.loads(data.decode("utf-8"))


def _http_text(url: str, max_bytes: int = _MAX_SKILL_BYTES) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Lucid-SkillRepos/0.1"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # nosec
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"remote skill exceeds {max_bytes} bytes")
    return data.decode("utf-8", errors="strict")


def _http_bytes(url: str, max_bytes: int) -> bytes:
    """Fetch raw bytes (for sibling files which may be binary, e.g. images)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Lucid-SkillRepos/0.1"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # nosec
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"remote file exceeds {max_bytes} bytes")
    return data


def _default_branch(owner: str, repo: str) -> str:
    try:
        meta = _http_json(f"https://api.github.com/repos/{owner}/{repo}")
        return str(meta.get("default_branch") or "main")
    except (urllib.error.URLError, ValueError, OSError):
        return "main"


def _fetch_github_tree(owner: str, repo: str, branch: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(branch_used, entries)`` for ``owner/repo`` via the trees API."""
    br = branch or _default_branch(owner, repo)
    tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{br}?recursive=1"
    tree = _http_json(tree_url)
    entries = tree.get("tree") or []
    if len(entries) > _MAX_TREE_ENTRIES:
        entries = entries[:_MAX_TREE_ENTRIES]
    return br, entries


def _list_skill_md_in_github(owner: str, repo: str, branch: str | None) -> list[dict[str, Any]]:
    """Walk a GitHub repo tree once and return every ``SKILL.md`` path.

    Each entry: ``{"path": str, "raw_url": str}``.
    """
    br, entries = _fetch_github_tree(owner, repo, branch)
    out: list[dict[str, Any]] = []
    for e in entries:
        path = str(e.get("path") or "")
        if not path or e.get("type") != "blob":
            continue
        # Match both ``SKILL.md`` (Anthropic spec) and ``skill.md`` (common
        # lowercase variant). Case-insensitive on the filename only.
        if path.rsplit("/", 1)[-1].lower() != "skill.md":
            continue
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{br}/{urllib.parse.quote(path)}"
        out.append({"path": path, "raw_url": raw})
    return out


# ---------------------------------------------------------------------------
# Cache (index of all SKILL.md files per repo, with parsed frontmatter)
# ---------------------------------------------------------------------------

def _load_cache() -> dict[str, Any]:
    p = _cache_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    tmp = _cache_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _cache_path())


def _invalidate_cache(rid: str) -> None:
    cache = _load_cache()
    if rid in cache:
        cache.pop(rid, None)
        _save_cache(cache)


def _refresh_repo_index(repo: dict[str, Any], force: bool = False) -> dict[str, Any]:
    """Fetch (or reuse from cache) the index of skills in this repo.

    Returns ``{"fetched_ms": int, "skills": [{path, raw_url, name, description}, ...]}``.
    """
    rid = repo["id"]
    cache = _load_cache()
    entry = cache.get(rid)
    now_ms = int(time.time() * 1000)
    if not force and entry and (now_ms - int(entry.get("fetched_ms") or 0)) < _CACHE_TTL_S * 1000:
        return entry
    gh = _parse_github(repo["url"])
    if not gh:
        raise ValueError(f"only github.com URLs are supported (got {repo['url']!r})")
    owner, name, branch = gh
    paths = _list_skill_md_in_github(owner, name, branch)
    skills_meta: list[dict[str, Any]] = []
    # We don't download every SKILL.md just to read frontmatter (too many
    # requests); instead the index keeps path + raw_url, and we lazily
    # fetch full content only when ``search_skills`` decides a hit is
    # promising. Path itself is a strong fuzzy-search signal.
    for p in paths:
        path = p["path"]
        # Heuristic display name: parent directory of the SKILL.md file,
        # falling back to the path itself.
        parts = path.rsplit("/", 2)
        display = parts[-2] if len(parts) >= 2 else path
        skills_meta.append({
            "path": path,
            "raw_url": p["raw_url"],
            "name": display,
            "description": "",
        })
    entry = {
        "fetched_ms": now_ms,
        "skills": skills_meta,
        "repo_url": repo["url"],
    }
    cache[rid] = entry
    _save_cache(cache)
    return entry


def refresh_all(force: bool = False) -> dict[str, Any]:
    """Force-refresh the index of every enabled repo. UI hook."""
    out: dict[str, Any] = {"repos": []}
    for repo in list_repos():
        if not repo.get("enabled"):
            continue
        try:
            idx = _refresh_repo_index(repo, force=force)
            out["repos"].append({
                "id": repo["id"],
                "name": repo["name"],
                "count": len(idx.get("skills") or []),
                "ok": True,
            })
        except Exception as e:  # noqa: BLE001 — surface to UI
            out["repos"].append({
                "id": repo["id"],
                "name": repo["name"],
                "count": 0,
                "ok": False,
                "error": str(e),
            })
    return out


# ---------------------------------------------------------------------------
# Search + install
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")


def _score(query: str, candidate: str) -> int:
    """Tiny fuzzy scorer — counts how many query tokens appear in candidate.

    Tokens are case-folded ascii words. Substring match counts; exact-word
    match scores higher. Not a search engine — good enough for ~hundreds of
    skill paths.
    """
    q_tokens = _WORD_RE.findall((query or "").lower())
    c_lower = (candidate or "").lower()
    c_tokens = set(_WORD_RE.findall(c_lower))
    if not q_tokens:
        return 0
    score = 0
    for t in q_tokens:
        if t in c_tokens:
            score += 3
        elif t in c_lower:
            score += 1
    return score


def search(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Search every enabled repo's cached index for ``query``.

    Returns the top ``limit`` matches as ``[{repo_id, repo_name, path,
    raw_url, name, score}, ...]``. Auto-refreshes stale caches; silently
    skips repos that fail to fetch (e.g. offline / rate-limited).
    """
    hits: list[dict[str, Any]] = []
    for repo in list_repos():
        if not repo.get("enabled"):
            continue
        try:
            idx = _refresh_repo_index(repo, force=False)
        except Exception:  # noqa: BLE001
            continue
        for s in idx.get("skills") or []:
            sc = _score(query, f"{s.get('name', '')} {s.get('path', '')}")
            if sc <= 0:
                continue
            hits.append({
                "repo_id": repo["id"],
                "repo_name": repo["name"],
                "repo_url": repo["url"],
                "path": s["path"],
                "raw_url": s["raw_url"],
                "name": s["name"],
                "score": sc,
            })
    hits.sort(key=lambda h: (-h["score"], h["path"]))
    return hits[:limit]


def install_from_repo(
    repo_id: str,
    path: str,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Download a specific SKILL.md from a repo and install it locally.

    The installed skill is tagged ``source = "repo"`` (default enabled,
    trusted because the user opted into the repo). Skills installed this
    way appear in the Skills page with a "repo" badge and show ``source_url``
    pointing at the raw file.
    """
    state = _load_state()
    repo = state["repos"].get(repo_id)
    if not repo:
        raise ValueError(f"unknown repo: {repo_id!r}")
    if not repo.get("enabled"):
        raise PermissionError(
            f"repo {repo.get('name')!r} is disabled — enable it in the Skills page first"
        )
    # Find the entry in the cached index (or refresh once).
    idx = _refresh_repo_index(repo, force=False)
    match = next(
        (s for s in (idx.get("skills") or []) if s.get("path") == path),
        None,
    )
    if not match:
        # Cache may be stale; retry with a forced refresh once.
        idx = _refresh_repo_index(repo, force=True)
        match = next(
            (s for s in (idx.get("skills") or []) if s.get("path") == path),
            None,
        )
    if not match:
        raise ValueError(f"skill path {path!r} not found in repo {repo['name']!r}")
    raw = _http_text(match["raw_url"])
    meta, body = skills_mod._parse_skill_md(raw)  # noqa: SLF001 — internal helper
    if not meta:
        raise ValueError(f"remote file has no YAML frontmatter: {match['raw_url']}")
    payload = {
        "name": meta.get("name") or match["name"],
        "description": meta.get("description") or f"From {repo['name']}: {path}",
        "body": body,
        "version": meta.get("version"),
        "license": meta.get("license"),
        "source": "repo",
        "source_url": match["raw_url"],
        "source_repo": repo["url"],
    }
    item = skills_mod.add_skill(payload, cfg=cfg)
    # Pull sibling files (scripts/, reference/, images, etc.) that live in
    # the same folder as SKILL.md. Anthropic-style skills (e.g.
    # ``skills/pptx``) are useless without these — SKILL.md instructs the
    # agent to read or execute them. We skip the SKILL.md itself (already
    # written via ``add_skill``) and refuse to escape the skill folder.
    try:
        siblings = _download_skill_folder(
            repo_url=repo["url"],
            skill_md_path=path,
            dest_dir=skills_mod._store_dir() / item["slug"],  # noqa: SLF001
        )
        item["sibling_files"] = siblings
    except Exception as e:  # noqa: BLE001 — siblings are best-effort
        item["sibling_files_error"] = str(e)
    return item


def _download_skill_folder(
    *,
    repo_url: str,
    skill_md_path: str,
    dest_dir: Any,
) -> list[str]:
    """Download every blob that lives in the same folder as ``skill_md_path``.

    Returns the list of relative paths written (excluding ``SKILL.md`` itself).
    Hard-capped by ``_MAX_SIBLING_FILES`` / per-file / cumulative byte budgets.
    """
    from pathlib import Path as _Path

    gh = _parse_github(repo_url)
    if not gh:
        return []
    owner, name, branch = gh
    br, entries = _fetch_github_tree(owner, name, branch)
    if "/" in skill_md_path:
        folder = skill_md_path.rsplit("/", 1)[0]
        prefix = folder + "/"
    else:
        # SKILL.md at repo root — pulling the whole repo would be insane,
        # bail out (no siblings).
        return []
    written: list[str] = []
    total_bytes = 0
    for e in entries:
        if len(written) >= _MAX_SIBLING_FILES:
            break
        p = str(e.get("path") or "")
        if e.get("type") != "blob" or not p.startswith(prefix):
            continue
        rel = p[len(prefix):]
        if not rel:
            continue
        # Skip SKILL.md itself (case-insensitive on the filename).
        if rel.rsplit("/", 1)[-1].lower() == "skill.md":
            continue
        # Sanity-check path: no absolute, no ``..`` segments.
        rel_parts = rel.split("/")
        if any(part in ("", "..") for part in rel_parts):
            continue
        raw_url = f"https://raw.githubusercontent.com/{owner}/{name}/{br}/{urllib.parse.quote(p)}"
        try:
            data = _http_bytes(raw_url, max_bytes=_MAX_SIBLING_FILE_BYTES)
        except (urllib.error.URLError, OSError, ValueError):
            continue
        total_bytes += len(data)
        if total_bytes > _MAX_SIBLING_TOTAL_BYTES:
            break
        out_path = _Path(dest_dir) / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, out_path)
        written.append(rel)
    return written


__all__ = [
    "list_repos",
    "add_repo",
    "delete_repo",
    "set_repo_enabled",
    "refresh_all",
    "search",
    "install_from_repo",
]
