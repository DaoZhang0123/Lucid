"""Per-app preferences for taskbar notification source selection.

There are two complementary detection channels:

* **uia**    — :mod:`lucid.taskbar_uia_monitor` (event-driven, zero LLM cost)
* **visual** — :mod:`lucid.taskbar_monitor`     (pixel diff + LLM confirm)

Most apps work great on UIA (Teams, Outlook, Slack, Discord — they update
their taskbar Name on unread). A few apps (WeChat — Name unchanged but
``HelpText='已请求注意'`` while flashing) also work on UIA via HelpText.
Some apps may publish *neither* signal and only paint a bitmap badge — those
need the visual channel.

This file lets **doze** (the offline reflection loop) record, per app, which
channel(s) have proven reliable. The monitors load this on startup and re-load
on demand via :func:`reload`.

File location: ``~/.lucid/taskbar_sources.json``

Schema (v1)::

    {
      "schema_version": 1,
      "global": {
        "default_allowed_sources": ["uia", "visual"]
      },
      "apps": {
        "微信":            {"allowed_sources": ["uia", "visual"], "primary": "uia",
                            "notes": "uia via HelpText flash, verified 2026-05-19"},
        "Microsoft Teams": {"allowed_sources": ["uia"], "primary": "uia",
                            "notes": "uia via tray Name change"},
        "SomeQuirkyApp":   {"allowed_sources": ["visual"], "primary": "visual",
                            "notes": "uia never fires; falls back to pixel diff"}
      }
    }

Doze does NOT need to write the file directly — it should call
:func:`set_app_pref` which handles read-modify-write + atomic replace.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Literal

SourceName = Literal["uia", "visual"]

SCHEMA_VERSION = 1
DEFAULT_PATH = Path.home() / ".lucid" / "taskbar_sources.json"

_WRITE_LOCK = threading.Lock()

_DEFAULT_PREFS: dict = {
    "schema_version": SCHEMA_VERSION,
    "global": {
        # Out-of-the-box: try both. Per-app overrides win.
        "default_allowed_sources": ["uia", "visual"],
    },
    # Seed two well-known apps so first run already knows what to do.
    # Doze can refine these from observation later.
    "apps": {
        "微信": {
            "allowed_sources": ["uia", "visual"],
            "primary": "uia",
            "notes": "uia fires via HelpText='已请求注意' on FlashWindowEx",
        },
        "WeChat": {
            "allowed_sources": ["uia", "visual"],
            "primary": "uia",
            "notes": "same as 微信 (English locale install)",
        },
        "Microsoft Teams": {
            "allowed_sources": ["uia", "visual"],
            "primary": "uia",
            "notes": "uia fires via tray Name change ('|新活动' / 'new activity')",
        },
    },
}


def path() -> Path:
    """Resolve the preferences file path. Overridable via LUCID_TASKBAR_SOURCES env."""
    p = os.environ.get("LUCID_TASKBAR_SOURCES")
    return Path(p) if p else DEFAULT_PATH


def load() -> dict:
    """Load the preferences file. Returns defaults (and writes them) on first run."""
    fp = path()
    if not fp.is_file():
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(fp, _DEFAULT_PREFS)
        except OSError:
            pass
        return json.loads(json.dumps(_DEFAULT_PREFS))  # deep copy
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return json.loads(json.dumps(_DEFAULT_PREFS))
        # Forward-compat: if schema_version is higher than we know, still try
        # to read the keys we recognise. Backward-compat: missing keys → defaults.
        data.setdefault("schema_version", SCHEMA_VERSION)
        data.setdefault("global", {})
        data["global"].setdefault(
            "default_allowed_sources",
            list(_DEFAULT_PREFS["global"]["default_allowed_sources"]),
        )
        data.setdefault("apps", {})
        return data
    except (OSError, json.JSONDecodeError):
        return json.loads(json.dumps(_DEFAULT_PREFS))


def _atomic_write(fp: Path, data: dict) -> None:
    fp.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False,
        dir=str(fp.parent), prefix=".taskbar_sources-", suffix=".tmp",
    ) as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, fp)


def save(prefs: dict) -> None:
    """Atomic save. Caller is responsible for keeping the dict shape valid."""
    with _WRITE_LOCK:
        _atomic_write(path(), prefs)


def set_app_pref(
    app_name: str,
    *,
    allowed_sources: list[SourceName] | None = None,
    primary: SourceName | None = None,
    notes: str | None = None,
) -> dict:
    """Read-modify-write a single app entry. Returns the updated app entry.

    Intended for use by doze.

    Example::

        taskbar_sources.set_app_pref(
            "微信",
            allowed_sources=["uia"],  # observed: visual triggers too many false positives
            primary="uia",
            notes="doze 2026-05-20: 18 uia hits, 6 false visual hits",
        )
    """
    if not app_name:
        raise ValueError("app_name required")
    with _WRITE_LOCK:
        prefs = load()
        apps = prefs.setdefault("apps", {})
        entry = apps.get(app_name, {})
        if allowed_sources is not None:
            entry["allowed_sources"] = list(allowed_sources)
        if primary is not None:
            entry["primary"] = primary
        if notes is not None:
            entry["notes"] = notes
        apps[app_name] = entry
        _atomic_write(path(), prefs)
        return entry


def decide_source_allowed(prefs: dict, app_name: str, source: SourceName) -> str:
    """Return ``"allow"`` or ``"deny"`` for emitting ``source`` for ``app_name``.

    Used by both monitors before emitting ``taskbar_notify_confirmed``.

    Resolution order:
      1. If ``apps[app_name].allowed_sources`` is set, honour it strictly.
      2. Otherwise fall back to ``global.default_allowed_sources``.
      3. If neither is set, allow.
    """
    app_entry = (prefs.get("apps") or {}).get(app_name) or {}
    allowed = app_entry.get("allowed_sources")
    if allowed is None:
        allowed = (prefs.get("global") or {}).get(
            "default_allowed_sources", ["uia", "visual"]
        )
    if not allowed:
        return "allow"
    return "allow" if source in allowed else "deny"


__all__ = [
    "SourceName",
    "SCHEMA_VERSION",
    "DEFAULT_PATH",
    "path",
    "load",
    "save",
    "set_app_pref",
    "decide_source_allowed",
]
