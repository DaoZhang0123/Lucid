"""操作技巧 tools.md（全局） + tips/<app>.md（按需加载）。

设计思想：
* 老的 tools.md 把所有 App 的提示都堆在一起，每次任务起手全量注入到 system prompt，
  既污染 context 又把不相关 App 的信息塞给模型。
* 现在拆成两层：
    - **全局** ``tools.md`` —— 只放跨 App 的通用原则（键盘优先、不覆盖用户工作、
      保存对话框输入路径、截图技巧），起手时自动注入。
    - **App 局部** ``apps/<slug>/tips.md`` —— 每个 App 一个子文件夹，方便后续在同一
      子文件夹下放更多 per-app 工件（regions.json / launcher.json / 截图样本等）。
      仅在模型主动调 ``load_app_tips(app=...)`` 或 ``launch_app(name=...)`` 时才注入。
* 模型用 ``learn_tip(text=..., kind=..., app=?)`` 写入；不传 ``app`` 默认入全局，
  传了就路由到 ``tips/<app>.md`` 并按需创建。

文件首次缺失会用 seed 写入；与 ``memory.md`` 同放在 ``%LOCALAPPDATA%\\dev.lucid\\`` 下。
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from .config import ToolsConfig
from . import apps as apps_pkg

_HEADER = "# Lucid Operation Tips Library\n"

# Global seed —— 跨 App 都用得上的原则。
# 与原来的 _SEED_BODY 相比：移走了 wechat / save-dialog / launch-app 几条到独立文件。
_SEED_BODY = """\
- [seed · keyboard] Prefer keyboard shortcuts over mouse clicks; screenshot coordinates aren't always exact, so anything you can do via the keyboard, do via the keyboard.
- [seed · keyboard] Switch / browse open windows with alt+tab / alt+shift+tab / win+tab; do NOT switch windows by clicking taskbar icons.
- [seed · browser] Default browser is **Microsoft Edge**. For any "open a web page / search the web / log into a site / read this URL" task, use Edge unless the user explicitly names another browser. Fastest path: `launch_app(name="edge")` (or `win+r` then type `msedge <url>` and Enter). Don't open Chrome / Firefox / etc. by default.
- [seed · read-webpage] **For reading webpages, prefer the `read_webpage` meta-tool over launching a real browser + screenshot.** It runs Edge headlessly behind the scenes, dumps the post-JS DOM, and returns plaintext — typically 1-3s vs 10-30s+ for the visual path (launch → render → L2/L3 screenshots → vision-read), and costs **no vision tokens**. Use it for: search results (`https://bing.com/search?q=...`), news / articles, weather / stock pages, doc pages, GitHub READMEs — anything where you only need the **text content**, not the layout. Only open a real browser window when (a) the user explicitly says "open" the page, (b) the page needs login state the user already has — then use `read_webpage(active_tab=true)` to read their live tab via CDP without opening anything new, (c) you must interact (click, fill a form), or (d) the headless dump returned nothing useful (heavy SPA / captcha / anti-bot) — say so and either retry with a different URL or escalate to the visual browser as a last resort.
- [seed · search] Default search engine is bing.com. Fastest path is `read_webpage(url="https://bing.com/search?q=keywords")` — returns the result list as plaintext in 1-3s, no browser window opened. Only use `win+r` → `https://bing.com/search?q=...` (which opens Edge visually) when the user asked to **see** the results in their browser. Switch to Google / Baidu / etc. only when the user explicitly asks.
- [seed · text-edit] Select all ctrl+a, copy ctrl+c, paste ctrl+v, undo ctrl+z, save ctrl+s, find ctrl+f.
- [seed · system] Run dialog win+r, File Explorer win+e, show desktop win+d; for Notepad use win+r then type notepad and Enter.
- [seed · window] Window layout: win+left/right snap to half, win+up maximise, win+down minimise / restore, win+m minimise all.
- [seed · type-text] Use action="type" + text="..." for text input; the local driver pastes via clipboard, works for CJK / paths / English alike. `\\n` inside text becomes a soft line break inside the focused widget, NOT an Enter — chat apps (WeChat, Telegram, ...) will keep the whole multi-line block as a single message; press Return separately to send.
- [seed · don't-overwrite] Before any 'open / new / save', assume the user already has work in progress; opening a fresh instance is always safer than overwriting.
- [seed · don't-overwrite] When saving, if the dialog's default filename points at an existing file, ctrl+a select-all and type a new explicit name (a timestamp helps).
- [seed · don't-overwrite] Before closing any window/tab, confirm it's one you opened yourself; never close the user's pre-existing windows.
- [seed · launch-app] Prefer the new `launch_app(name)` meta tool over manually clicking taskbar/tray icons or double-clicking desktop shortcuts. Call `list_apps()` to see what's pre-registered. If `launch_app` returns "not found" then fall back to `win+r` + exe alias, the start menu, or visual icon-clicking — and remember to `learn_tip(app="<name>", text="works on this machine")` so next time `launch_app` succeeds.
- [seed · paths] Desktop path: %USERPROFILE%\\Desktop or C:\\Users\\<user>\\Desktop, can be typed directly as an absolute path.
- [seed · screenshot] Before clicking small buttons / icons, take an L2 active_window or L3 cursor_local screenshot to see clearly and avoid misclicks.
- [seed · click-precise-coords] **Never give an "approximate" coordinate from memory or eyeballing — the y/x you pass to `computer` must be read off the most recent screenshot of the very thing you're targeting.** Hand-wavy phrases in your reasoning ("around y=142", "roughly the middle of the list", "should be near the top") are red flags: lists scroll, rows have variable heights, and a 30-pixel error puts the click in the wrong row, the sidebar, or the chat panel. Required workflow before any non-trivial click: (1) make sure the target element is **visible in the current screenshot** (the post-step L2/L3 auto-attached after the previous tool, or a fresh `screenshot(level="active_window")` if anything has scrolled / repainted since); (2) **visually locate** the element in that image and read its **center pixel** in that image's own coordinate frame; (3) issue the click at exactly that (x, y) — the framework reverse-maps to screen pixels for you. If the target is not visible in the current screenshot, do **not** guess at a number — either scroll / take a fresh screenshot first, or use a **keyboard shortcut alternative** (Ctrl+F search, Tab navigation, accelerator keys, address-bar typing) which sidesteps coordinate-picking entirely. When the keyboard path exists, prefer it.
- [seed · uwp-availability] **`Get-AppxPackage <name>` returning Ok is NOT proof the UWP app actually launches.** If immediately after that `start ms-<name>:` (or `launch_app(name=...)` URI path) opens the Windows "Find a compatible app" / "How do you want to open this?" dialog, the package is registered but its activation handler is broken/dezombied. Treat that as **unavailable** right away — close the dialog (Esc), report `task failed: <name> unavailable on this machine` (or fall back to a non-UWP path the user accepts), do NOT keep retrying or screenshotting the dialog.
- [seed · process-list] To check whether a process is running, prefer `Get-Process <name> -ErrorAction SilentlyContinue` (PowerShell) or `(Get-Process <name>).MainWindowTitle` for the title. **Avoid `tasklist /V`** — on Windows 11 it routinely takes 60-300 s because it walks every process's window-title via WMI; a single call can blow your shell timeout and effectively kill the turn. `Get-Process` returns in well under a second.
- [seed · taskbar-enum] **Don't hover-and-zoom over tiny taskbar / tray icons.** At 4K / 3440 widths each icon is a few pixels and L3 tiles rarely contain readable text. To list **pinned** taskbar apps: `run_shell powershell -c "Get-ChildItem $env:APPDATA\\Microsoft\\Internet Explorer\\Quick Launch\\User Pinned\\TaskBar -Filter *.lnk | Select-Object -ExpandProperty BaseName"`. To list **currently-running** windows with their titles: `run_shell powershell -c "Get-Process | Where-Object MainWindowTitle | Select-Object ProcessName,MainWindowTitle"`. A single fullscreen screenshot to confirm icons are visually present is fine; never hover-enumerate. Once the shell output has the answer, the **same turn** must emit `task complete:` — no more screenshots.
- [seed · windows-version] To read the Windows build / version string, use `Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion' | Select-Object ProductName, DisplayVersion, CurrentBuild, UBR` — milliseconds, no admin needed. Do **not** use `Get-ComputerInfo` (multi-second WMI roundtrip) or `winver` (opens a GUI window) for this.
"""

# Per-app seed bodies are no longer hardcoded here — each App lives in its own
# ``lucid.apps.<slug>`` module exporting ``SLUG`` / ``TITLE`` / ``TIPS`` /
# ``LAUNCHER``. Drop a new file in that package to add a new App.
def _app_seeds() -> dict[str, tuple[str, str]]:
    """``{slug: (title, body)}`` — built fresh from the apps registry every call
    so reloading a module during dev is reflected.
    """
    return {slug: (title, body) for slug, (title, body) in apps_pkg.all_tips_seeds().items() if body.strip()}


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _user_data_dir() -> Path:
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "dev.lucid"
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".lucid"
    return Path.cwd()


def tools_path(cfg: ToolsConfig) -> Path:
    p = Path(cfg.path)
    if p.is_absolute():
        return p
    return _user_data_dir() / cfg.path


def apps_dir(cfg: ToolsConfig) -> Path:
    """Root folder holding one subfolder per app (``apps/<slug>/``).

    Each subfolder owns the per-app artefacts: ``tips.md`` (this module),
    later ``regions.json`` / ``launcher.json`` etc. Adding a new app == dropping
    a new ``apps/<slug>/`` folder.
    """
    sub = getattr(cfg, "apps_dir", "apps") or "apps"
    p = Path(sub)
    if p.is_absolute():
        return p
    return _user_data_dir() / sub


def _slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    s = s.strip("-")
    return s


def app_dir(cfg: ToolsConfig, app: str) -> Path:
    return apps_dir(cfg) / _slugify(app)


def app_tips_path(cfg: ToolsConfig, app: str) -> Path:
    return app_dir(cfg, app) / "tips.md"


def app_disabled_marker(cfg: ToolsConfig, app: str) -> Path:
    """Marker file that suppresses re-seeding for a built-in app the user
    has explicitly deleted. Lives next to the tips file as ``.disabled``."""
    return app_dir(cfg, app) / ".disabled"


def is_app_disabled(cfg: ToolsConfig, app: str) -> bool:
    return app_disabled_marker(cfg, app).is_file()


# ---------------------------------------------------------------------------
# Seed / read / write
# ---------------------------------------------------------------------------


def _ensure_global_seeded(cfg: ToolsConfig) -> Path:
    p = tools_path(cfg)
    if not p.is_file():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_HEADER + "\n" + _SEED_BODY, encoding="utf-8")
        return p
    # Pristine-refresh: if every entry line is still a seed entry (user hasn't
    # learned anything new globally), refresh from the in-code _SEED_BODY so
    # updates to the seed propagate to existing installs without manual reset.
    try:
        existing = p.read_text(encoding="utf-8")
    except OSError:
        return p
    entry_lines = [ln for ln in existing.splitlines() if ln.lstrip().startswith("- [")]
    if entry_lines and all(("[seed " in ln or "· seed " in ln) for ln in entry_lines):
        fresh = _HEADER + "\n" + _SEED_BODY
        if existing.strip() != fresh.strip():
            p.write_text(fresh, encoding="utf-8")
    return p


def _ensure_app_seeded(cfg: ToolsConfig, app_slug: str) -> Path | None:
    """If we have a seed for this app and the file is missing, write it.

    Additionally, if the file exists but contains ONLY seed entries (no
    user/agent additions) and the registry's current seed body differs from
    the on-disk text, rewrite the file with the fresh seed. This keeps users
    who never customised an app's tips on the latest in-code recipes (e.g.
    when we strengthen ``edge`` with a "use ctrl+t" recipe).

    Returns the path if a file exists (after possibly seeding), else None.
    """
    p = app_tips_path(cfg, app_slug)
    seed = _app_seeds().get(app_slug)
    # If the user has explicitly deleted a built-in app's tips, honour that
    # and never re-seed until they reset/re-enable.
    if is_app_disabled(cfg, app_slug):
        return p if p.is_file() else None
    if not p.is_file():
        if seed is None:
            return p if p.is_file() else None
        title, body = seed
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {title}\n\n{body}", encoding="utf-8")
        return p
    if seed is not None:
        try:
            existing = p.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        # "Pristine" = every entry line is a seed entry. If so, refresh from
        # the registry. Non-entry lines (header, blank lines, prose) are
        # ignored when classifying.
        entry_lines = [ln for ln in existing.splitlines() if ln.lstrip().startswith("- [")]
        if entry_lines and all(("[seed " in ln or "· seed " in ln) for ln in entry_lines):
            title, body = seed
            fresh = f"# {title}\n\n{body}"
            if existing.strip() != fresh.strip():
                p.write_text(fresh, encoding="utf-8")
    return p


def seed_all_apps(cfg: ToolsConfig) -> list[str]:
    """Make sure every known app seed file exists. Returns slugs touched.
    Skips apps the user has explicitly disabled via ``.disabled`` marker."""
    out: list[str] = []
    for slug in _app_seeds().keys():
        if is_app_disabled(cfg, slug):
            continue
        if _ensure_app_seeded(cfg, slug) is not None:
            out.append(slug)
    return out


# ---------------------------------------------------------------------------
# Read for prompt
# ---------------------------------------------------------------------------


def read_tools(cfg: ToolsConfig) -> str:
    """Read the global tools.md raw text (with header)."""
    if not cfg.enabled:
        return ""
    p = _ensure_global_seeded(cfg)
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def read_app_tips(cfg: ToolsConfig, app: str) -> str:
    """Read one app's tips file. Empty string if not present and no seed available."""
    if not cfg.enabled:
        return ""
    slug = _slugify(app)
    if not slug:
        return ""
    p = _ensure_app_seeded(cfg, slug)
    if p is None or not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def list_app_tips(cfg: ToolsConfig) -> list[dict]:
    """List available per-app tip files (existing on disk + known seeds).

    Each entry: ``{slug, title, file, has_user_entries, lines}``. Used by
    ``list_apps``-style meta tools so the model knows what app names it can
    pass to ``load_app_tips``.
    """
    seed_all_apps(cfg)
    d = apps_dir(cfg)
    out: list[dict] = []
    seen: set[str] = set()
    if d.is_dir():
        # New layout: each app is a subfolder with tips.md inside.
        candidates = sorted(
            (sub / "tips.md" for sub in d.iterdir() if sub.is_dir()),
            key=lambda p: p.parent.name,
        )
        for f in candidates:
            if not f.is_file():
                continue
            slug = f.parent.name
            seen.add(slug)
            try:
                txt = f.read_text(encoding="utf-8")
            except OSError:
                continue
            title = ""
            for line in txt.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            entry_lines = [ln for ln in txt.splitlines() if ln.startswith("- [")]
            user_entries = sum(1 for ln in entry_lines if "[seed " not in ln and "· seed " not in ln)
            out.append({
                "slug": slug,
                "title": title or slug,
                "file": str(f),
                "lines": len(entry_lines),
                "has_user_entries": user_entries > 0,
                "is_seeded": slug in _app_seeds(),
            })
    # also surface seed-only slugs that haven't been written yet
    for slug, (title, _body) in _app_seeds().items():
        if slug in seen:
            continue
        if is_app_disabled(cfg, slug):
            # User-deleted built-in: keep it hidden until they reset.
            continue
        out.append({
            "slug": slug,
            "title": title,
            "file": str(app_tips_path(cfg, slug)),
            "lines": 0,
            "has_user_entries": False,
            "is_seeded": True,
        })
    return out


def _strip_header(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("#"):
        nl = raw.find("\n")
        if nl > 0:
            return raw[nl + 1 :].strip()
        return ""
    return raw


def tools_for_prompt(cfg: ToolsConfig) -> str:
    """Global tips section, injected into system prompt at task start."""
    raw = read_tools(cfg).strip()
    if not raw:
        return ""
    body = _strip_header(raw)
    if cfg.max_chars > 0 and len(body) > cfg.max_chars:
        body = body[-cfg.max_chars :]
        nl = body.find("\n")
        if nl > 0:
            body = body[nl + 1 :]
    if not body.strip():
        return ""
    apps = [it for it in list_app_tips(cfg) if it.get("lines", 0) > 0]
    catalog = ""
    if apps:
        bullets = "\n".join(f"  - `{it['slug']}` — {it['title']}" for it in apps)
        catalog = (
            "\nApp-specific tips are stored separately and are NOT loaded by default. "
            "Call `load_app_tips(app=\"<slug>\")` to pull a specific App's tips into the conversation, "
            "or `launch_app(name=\"<slug>\")` (which auto-loads them on success). Available app tip files:\n"
            + bullets
            + "\n"
        )
    return (
        "\n## Operation tips (dynamically learned; use learn_tip to add new ones when you discover them)\n"
        + body.strip()
        + "\n"
        + catalog
    )


def app_tips_for_prompt(cfg: ToolsConfig, app: str) -> str:
    """Format one app's tips file as an injectable user message body. If the
    app's registry entry declares ``INCLUDES = (...)`` (e.g. ``edge`` includes
    ``browser``), each included app's tips are appended below as a separate
    section so the model gets the cross-cutting shortcuts together with the
    app-specific ones.
    """
    raw = read_app_tips(cfg, app).strip()
    if not raw:
        return ""
    body = _strip_header(raw)
    title = ""
    for line in raw.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    head = f"## App tips for `{_slugify(app)}`"
    if title:
        head += f" ({title})"
    parts = [head, body.strip()]
    # Auto-include transitive tips so e.g. launch_app("edge") drags in the
    # generic browser tips without the model having to chain load_app_tips.
    seen: set[str] = {_slugify(app)}
    queue: list[str] = []
    app_def = apps_pkg.get_app(_slugify(app))
    if app_def is not None:
        queue.extend(app_def.includes)
    while queue:
        inc_slug = _slugify(queue.pop(0))
        if not inc_slug or inc_slug in seen:
            continue
        seen.add(inc_slug)
        inc_raw = read_app_tips(cfg, inc_slug).strip()
        if not inc_raw:
            continue
        inc_body = _strip_header(inc_raw)
        inc_title = ""
        for line in inc_raw.splitlines():
            if line.startswith("# "):
                inc_title = line[2:].strip()
                break
        sub_head = f"### Inherited tips from `{inc_slug}`"
        if inc_title:
            sub_head += f" ({inc_title})"
        parts.append(sub_head)
        parts.append(inc_body.strip())
        inc_def = apps_pkg.get_app(inc_slug)
        if inc_def is not None:
            queue.extend(inc_def.includes)
    return "\n".join(p for p in parts if p) + "\n"


# ---------------------------------------------------------------------------
# Append / write
# ---------------------------------------------------------------------------


def append_tip(
    cfg: ToolsConfig,
    text: str,
    kind: str = "tip",
    source: str = "agent",
    app: str | None = None,
) -> bool:
    """Append a tip. ``app`` empty / None → global tools.md. Otherwise routed
    to ``tips/<slug>.md`` (created from seed if known, blank-with-header otherwise).
    """
    if not cfg.enabled:
        return False
    text = (text or "").strip()
    if not text:
        return False
    text = re.sub(r"\s+", " ", text)
    if cfg.max_entry_chars > 0 and len(text) > cfg.max_entry_chars:
        text = text[: cfg.max_entry_chars - 1] + "…"
    kind = (kind or "tip").lower()
    if kind not in ("tip", "success", "failure"):
        kind = "tip"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"- [{ts} · {source} · {kind}] {text}\n"
    if app:
        slug = _slugify(app)
        p = app_tips_path(cfg, slug)
        if not p.is_file():
            seed = _app_seeds().get(slug)
            p.parent.mkdir(parents=True, exist_ok=True)
            if seed:
                p.write_text(f"# {seed[0]}\n\n{seed[1]}", encoding="utf-8")
            else:
                p.write_text(f"# Tips for {slug}\n\n", encoding="utf-8")
    else:
        p = _ensure_global_seeded(cfg)
    with p.open("a", encoding="utf-8") as f:
        f.write(entry)
    _rotate(cfg, p)
    return True


def write_tools_raw(cfg: ToolsConfig, text: str) -> bool:
    p = tools_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = text or ""
    if not text.startswith("#"):
        text = _HEADER + "\n" + text
    p.write_text(text, encoding="utf-8")
    return True


def write_app_tips_raw(cfg: ToolsConfig, app: str, text: str) -> bool:
    slug = _slugify(app)
    if not slug:
        return False
    p = app_tips_path(cfg, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not text.startswith("#"):
        seed = _app_seeds().get(slug)
        title = seed[0] if seed else f"Tips for {slug}"
        text = f"# {title}\n\n" + text
    p.write_text(text, encoding="utf-8")
    return True


def reset_to_seed(cfg: ToolsConfig) -> bool:
    """Reset global tools.md to its seed (per-app files untouched)."""
    p = tools_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_HEADER + "\n" + _SEED_BODY, encoding="utf-8")
    return True


def reset_app_to_seed(cfg: ToolsConfig, app: str) -> bool:
    slug = _slugify(app)
    seed = _app_seeds().get(slug)
    if not seed:
        return False
    p = app_tips_path(cfg, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# {seed[0]}\n\n{seed[1]}", encoding="utf-8")
    # Reset = re-enable: clear any prior delete marker.
    marker = app_disabled_marker(cfg, slug)
    if marker.exists():
        try:
            marker.unlink()
        except OSError:
            pass
    return True


def delete_app_tips(cfg: ToolsConfig, app: str) -> dict:
    """Delete a per-app tips file.

    For user-created apps (no in-code seed): removes ``apps/<slug>/tips.md``
    and the enclosing folder if empty.

    For built-in apps (with a registered seed): removes the tips file AND
    writes a ``.disabled`` marker so the seeder won't recreate it. Call
    :func:`reset_app_to_seed` to re-enable the built-in defaults.
    """
    slug = _slugify(app)
    if not slug:
        return {"ok": False, "reason": "invalid slug"}
    p = app_tips_path(cfg, slug)
    if p.exists():
        try:
            p.unlink()
        except OSError as e:
            return {"ok": False, "reason": f"unlink failed: {e}"}
    is_builtin = slug in _app_seeds()
    if is_builtin:
        # Drop a marker so _ensure_app_seeded won't resurrect the file.
        marker = app_disabled_marker(cfg, slug)
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("deleted by user\n", encoding="utf-8")
        except OSError as e:
            return {"ok": False, "reason": f"marker write failed: {e}"}
        return {"ok": True, "disabled": True}
    # User-created app: try to clean up the now-empty folder.
    parent = p.parent
    if parent.is_dir():
        try:
            next(parent.iterdir())
        except StopIteration:
            try:
                parent.rmdir()
            except OSError:
                pass
        except OSError:
            pass
    return {"ok": True}


def _rotate(cfg: ToolsConfig, p: Path) -> None:
    if cfg.max_entries <= 0:
        return
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return
    lines = raw.splitlines()
    entry_idxs = [i for i, ln in enumerate(lines) if ln.startswith("- [")]
    if len(entry_idxs) <= cfg.max_entries:
        return
    seed_idxs = [i for i in entry_idxs if "· seed " in lines[i] or "[seed " in lines[i]]
    learned_idxs = [i for i in entry_idxs if i not in seed_idxs]
    keep = max(0, cfg.max_entries - len(seed_idxs))
    drop_count = max(0, len(learned_idxs) - keep)
    drop_set = set(learned_idxs[:drop_count])
    new_lines = [ln for i, ln in enumerate(lines) if i not in drop_set]
    p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
