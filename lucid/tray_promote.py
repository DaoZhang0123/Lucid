"""Force every Windows tray icon to be visible on the taskbar.

Windows 11 stores per-app tray-icon visibility under::

    HKEY_CURRENT_USER\\Control Panel\\NotifyIconSettings\\<id>

Each subkey has an ``IsPromoted`` DWORD: ``1`` = always show on the taskbar,
``0`` = hide in the overflow flyout (the little "^" arrow). The "其他系统托
盘图标" (Other system tray icons) page in 个性化 → 任务栏 is just a UI on top
of these values.

This module flips every existing entry to ``IsPromoted = 1`` so e.g. the
``lucid`` taskbar monitor never has to chase icons that were sent to the
overflow. We **do not** clear or restart explorer — the registry mutation
is enough; new icons created later will still default to whatever Windows
chose, so the daily schedule re-runs this sweep.

Pure stdlib (``winreg``); fails silently / returns ``{"skipped": ...}`` on
non-Windows systems.
"""
from __future__ import annotations

import os
from typing import Any

_REG_PATH = r"Control Panel\NotifyIconSettings"


def _is_windows() -> bool:
    return os.name == "nt"


def promote_all_tray_icons() -> dict[str, Any]:
    """Set ``IsPromoted = 1`` on every ``NotifyIconSettings\\<id>`` subkey.

    Returns a summary dict with counts and (best-effort) per-app entries that
    changed, so the schedule log shows what happened.
    """
    if not _is_windows():
        return {"skipped": "non-windows"}

    import winreg  # local import: not available on POSIX

    summary: dict[str, Any] = {
        "total": 0,
        "already_promoted": 0,
        "promoted_now": 0,
        "errors": 0,
        "changed": [],
    }

    try:
        root = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _REG_PATH,
            0,
            winreg.KEY_READ,
        )
    except FileNotFoundError:
        return {"skipped": "registry-key-missing"}
    except OSError as exc:
        return {"error": f"open root failed: {exc}"}

    subkeys: list[str] = []
    try:
        i = 0
        while True:
            try:
                subkeys.append(winreg.EnumKey(root, i))
            except OSError:
                break
            i += 1
    finally:
        winreg.CloseKey(root)

    summary["total"] = len(subkeys)

    for sk in subkeys:
        full = f"{_REG_PATH}\\{sk}"
        try:
            handle = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                full,
                0,
                winreg.KEY_READ | winreg.KEY_SET_VALUE,
            )
        except OSError:
            summary["errors"] += 1
            continue
        try:
            current = 0
            try:
                current, _ = winreg.QueryValueEx(handle, "IsPromoted")
            except FileNotFoundError:
                current = 0
            if int(current) == 1:
                summary["already_promoted"] += 1
                continue
            try:
                winreg.SetValueEx(handle, "IsPromoted", 0, winreg.REG_DWORD, 1)
            except OSError:
                summary["errors"] += 1
                continue
            summary["promoted_now"] += 1
            # Best-effort identifier for the log: prefer ExecutablePath, then
            # Tooltip, falling back to the registry id itself.
            label = ""
            for name in ("ExecutablePath", "Tooltip"):
                try:
                    val, _ = winreg.QueryValueEx(handle, name)
                    if val:
                        label = str(val)
                        break
                except FileNotFoundError:
                    continue
            summary["changed"].append(label or sk)
        finally:
            winreg.CloseKey(handle)

    # Trim long lists so log lines stay readable.
    if len(summary["changed"]) > 30:
        summary["changed"] = summary["changed"][:30] + ["...(truncated)"]
    return summary


__all__ = ["promote_all_tray_icons"]
