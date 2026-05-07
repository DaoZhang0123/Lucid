"""Icon proposal queue (doze v1).

Proposals are emitted by the doze reflector via the ``propose_icon`` tool when
it spots an icon-meaning declaration in a thread transcript. They live in
``<user data>/icon_proposals/`` until the user accepts (→ commits to
``icon_memory``) or rejects (→ deleted).

Layout::

    <user data>/icon_proposals/
        index.json       # {"items": [{id, label, description, source_thread,
                         #              source_file, x, y, w, h, file, added_ms}]}
        <id>.png         # cropped PNG (already validated)
"""
from __future__ import annotations

import io
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

from .config import Config


def _user_data_dir() -> Path:
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "dev.ctrlapp"
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".ctrlapp"
    return Path.cwd()


def proposals_dir(_cfg: Config) -> Path:
    return _user_data_dir() / "icon_proposals"


def _index_path(cfg: Config) -> Path:
    return proposals_dir(cfg) / "index.json"


def _load_index(cfg: Config) -> dict[str, Any]:
    p = _index_path(cfg)
    if not p.is_file():
        return {"items": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"items": []}


def _save_index(cfg: Config, data: dict[str, Any]) -> None:
    p = _index_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_proposals(cfg: Config) -> list[dict[str, Any]]:
    return list(_load_index(cfg).get("items", []))


def add_proposal(
    cfg: Config,
    *,
    png_bytes: bytes,
    label: str,
    description: str,
    source_thread: str,
    source_file: str,
    x: int,
    y: int,
    w: int,
    h: int,
) -> dict[str, Any] | None:
    """Persist a cropped PNG + metadata. Returns the new entry or None on bad input."""
    label = (label or "").strip()
    description = (description or "").strip()
    if not label or not png_bytes:
        return None
    try:
        with Image.open(io.BytesIO(png_bytes)) as im:
            im.verify()
    except Exception:
        return None
    iid = uuid.uuid4().hex[:8]
    fname = f"{iid}.png"
    d = proposals_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    (d / fname).write_bytes(png_bytes)
    entry = {
        "id": iid,
        "file": fname,
        "label": label[:80],
        "description": description[:300],
        "source_thread": source_thread,
        "source_file": source_file,
        "x": int(x),
        "y": int(y),
        "w": int(w),
        "h": int(h),
        "added_ms": int(time.time() * 1000),
    }
    data = _load_index(cfg)
    items: list[dict[str, Any]] = list(data.get("items") or [])
    items.append(entry)
    # Soft cap to avoid runaway: keep latest 200.
    if len(items) > 200:
        for old in items[:-200]:
            try:
                (d / old.get("file", "")).unlink(missing_ok=True)
            except OSError:
                pass
        items = items[-200:]
    data["items"] = items
    _save_index(cfg, data)
    return entry


def read_proposal_png(cfg: Config, pid: str) -> bytes | None:
    for it in _load_index(cfg).get("items", []):
        if it.get("id") == pid:
            try:
                return (proposals_dir(cfg) / it.get("file", "")).read_bytes()
            except OSError:
                return None
    return None


def reject(cfg: Config, pid: str) -> bool:
    data = _load_index(cfg)
    items = list(data.get("items") or [])
    new = [it for it in items if it.get("id") != pid]
    if len(new) == len(items):
        return False
    for it in items:
        if it.get("id") == pid:
            try:
                (proposals_dir(cfg) / it.get("file", "")).unlink(missing_ok=True)
            except OSError:
                pass
    data["items"] = new
    _save_index(cfg, data)
    return True


def accept(cfg: Config, pid: str, *, override_label: str = "", override_desc: str = "") -> dict[str, Any] | None:
    """Move a proposal into the live ``icon_memory`` atlas.

    Returns the registered icon entry, or ``None`` on failure (proposal not
    found / icons disabled / add_icon refused).
    """
    from . import icon_memory as icon_mod  # local import to avoid cycles
    if not cfg.icons.enabled:
        return None
    data = _load_index(cfg)
    items = list(data.get("items") or [])
    target = None
    for it in items:
        if it.get("id") == pid:
            target = it
            break
    if target is None:
        return None
    png = read_proposal_png(cfg, pid)
    if not png:
        return None
    label = (override_label or target.get("label") or "").strip()
    desc = (override_desc or target.get("description") or "").strip()
    entry = icon_mod.add_icon(cfg.icons, png, label, description=desc)
    if entry is None:
        return None
    # Successfully committed → drop the proposal.
    reject(cfg, pid)
    return entry


def clear_all(cfg: Config) -> int:
    data = _load_index(cfg)
    items = list(data.get("items") or [])
    d = proposals_dir(cfg)
    for it in items:
        try:
            (d / it.get("file", "")).unlink(missing_ok=True)
        except OSError:
            pass
    _save_index(cfg, {"items": []})
    return len(items)
