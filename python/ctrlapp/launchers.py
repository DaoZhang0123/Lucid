"""`launch_app` meta tool —— 用 Windows 原生接口启动 / 切换 App，绕开视觉。

设计：4 步策略链（详见 Docs/screenshot.md §12）
  1. **已在跑** → 找窗口 → SetForegroundWindow（零启动、零截图）
  2. **全局快捷键** → pyautogui.hotkey
  3. **协议 URI**   → start <uri>
  4. **可执行别名 / 路径** → subprocess.Popen("<exe>", shell=True)

数据：`<user data>/launchers.json` 持久化每个 launcher entry。首次启动 sidecar 时
seed 一份默认表（wechat / vscode / outlook / chrome / explorer / notepad / settings / run）。
用户可以从 `/launchers` 前端页面手动校准（覆盖默认）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import LaunchersConfig
from . import apps as apps_pkg


# ---------------------------------------------------------------------------
# Default launcher table (registry-driven)
# ---------------------------------------------------------------------------

# 每条 launcher entry 字段：
#   name           人类可读名（中文 OK）
#   description    短描述，注入 system prompt
#   shortcut       全局快捷键（pyautogui.hotkey 风格："ctrl+alt+w" 或 ["win+r","wechat","enter"]）
#   uri            shell URI（"weixin://" / "ms-outlook://" / "ms-settings:" / ...）
#   exe            可执行别名 / 绝对路径（subprocess.Popen 的字符串）
#   process        进程名（用于 check_app_running，大小写不敏感）
#   window_title   窗口标题完全 / 子串（用于 find_window）
#   window_title_re 窗口标题正则（优先于 window_title）
#
# 默认值不再写死在这个文件里 —— 每个 App 在 ``ctrlapp.apps.<slug>`` 模块里自己声明
# ``LAUNCHER = {...}``。新增 App 只要加一个文件。运行时用户可以通过
# ``<user data>/launchers.json`` 覆盖（前端 UI / agent 的 ``update_launcher`` meta tool）。


def _default_launchers() -> dict[str, dict[str, Any]]:
    return apps_pkg.all_launchers()


# ---------------------------------------------------------------------------
# Path / persistence
# ---------------------------------------------------------------------------


def _user_data_dir() -> Path:
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "dev.ctrlapp"
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".ctrlapp"
    return Path.cwd()


def launchers_path(cfg: LaunchersConfig) -> Path:
    p = Path(cfg.path)
    if p.is_absolute():
        return p
    return _user_data_dir() / cfg.path


def _slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    return s.strip("-")


def _load_overrides(cfg: LaunchersConfig) -> dict[str, dict[str, Any]]:
    p = launchers_path(cfg)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, dict):
            out[_slugify(k)] = v
    return out


def _save_overrides(cfg: LaunchersConfig, data: dict[str, dict[str, Any]]) -> None:
    p = launchers_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_launchers(cfg: LaunchersConfig) -> list[dict[str, Any]]:
    """Merge defaults + user overrides; return list sorted by name."""
    overrides = _load_overrides(cfg)
    merged: dict[str, dict[str, Any]] = {}
    for slug, spec in _default_launchers().items():
        merged[slug] = {**spec, "slug": slug, "source": "default"}
    for slug, spec in overrides.items():
        if slug in merged:
            merged[slug] = {**merged[slug], **spec, "slug": slug, "source": "user"}
        else:
            merged[slug] = {**spec, "slug": slug, "source": "user"}
    out = list(merged.values())
    out.sort(key=lambda it: it.get("name") or it.get("slug") or "")
    return out


def get_launcher(cfg: LaunchersConfig, name: str) -> dict[str, Any] | None:
    slug = _slugify(name)
    for it in list_launchers(cfg):
        if it["slug"] == slug:
            return it
    return None


def upsert_launcher(cfg: LaunchersConfig, slug: str, spec: dict[str, Any]) -> dict[str, Any]:
    slug = _slugify(slug)
    if not slug:
        raise ValueError("slug required")
    overrides = _load_overrides(cfg)
    cur = overrides.get(slug, {})
    cur.update({k: v for k, v in spec.items() if v is not None and v != ""})
    overrides[slug] = cur
    _save_overrides(cfg, overrides)
    return get_launcher(cfg, slug) or {"slug": slug, **cur}


def delete_launcher_override(cfg: LaunchersConfig, slug: str) -> bool:
    slug = _slugify(slug)
    overrides = _load_overrides(cfg)
    if slug not in overrides:
        return False
    overrides.pop(slug)
    _save_overrides(cfg, overrides)
    return True


# ---------------------------------------------------------------------------
# Process / window detection
# ---------------------------------------------------------------------------


@dataclass
class WindowMatch:
    hwnd: int
    title: str
    pid: int


def _is_running_by_name(process_name: str) -> tuple[bool, int | None]:
    """Returns (running, pid) — pid of the first matching process or None."""
    if not process_name:
        return False, None
    try:
        import psutil  # type: ignore[import-not-found]
    except Exception:
        return False, None
    target = process_name.lower()
    for p in psutil.process_iter(["name", "pid"]):
        try:
            n = (p.info.get("name") or "").lower()
        except Exception:
            continue
        if n == target:
            return True, int(p.info.get("pid") or 0) or None
    return False, None


def _find_windows(spec: dict[str, Any]) -> list[WindowMatch]:
    """Enumerate visible top-level windows that match the spec's title rules."""
    if sys.platform != "win32":
        return []
    title_sub = spec.get("window_title")
    title_re = spec.get("window_title_re")
    if not title_sub and not title_re:
        return []
    pat = None
    if title_re:
        try:
            pat = re.compile(title_re, re.IGNORECASE)
        except re.error:
            pat = None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[WindowMatch] = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
    )

    def cb(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
        ok = False
        if pat is not None:
            ok = bool(pat.search(title))
        elif title_sub:
            ok = title_sub in title
        if ok:
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append(WindowMatch(hwnd=int(hwnd), title=title, pid=int(pid.value)))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found


def _force_foreground(hwnd: int) -> bool:
    """Bring hwnd to the front, working around the focus-stealing protection."""
    if sys.platform != "win32" or not hwnd:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    try:
        if fg_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        ok = bool(user32.SetForegroundWindow(hwnd))
        if not ok:
            # Fallback: simulate Alt key tap then retry; Windows will accept.
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 0x0002, 0)
            ok = bool(user32.SetForegroundWindow(hwnd))
        return ok
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)


def window_client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Return the screen-space (x, y, w, h) of an hwnd's **client area**,
    using DWM extended frame bounds (no shadow) and ClientRect+ClientToScreen
    (no title bar / menu bar). See Docs/screenshot.md §13.3 for the rationale.

    Returns None if the window is hidden / minimised / hwnd invalid.
    """
    if sys.platform != "win32" or not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        if not user32.IsWindowVisible(hwnd):
            return None
        if user32.IsIconic(hwnd):
            return None
        # GetClientRect → (0, 0, w, h) of the client area
        crect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(crect)):
            return None
        w = int(crect.right - crect.left)
        h = int(crect.bottom - crect.top)
        if w <= 0 or h <= 0:
            return None
        # ClientToScreen on the (0,0) client corner gives the screen origin
        pt = wintypes.POINT(int(crect.left), int(crect.top))
        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return None
        return (int(pt.x), int(pt.y), w, h)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API used by meta_tools / sidecar
# ---------------------------------------------------------------------------


def check_app_running(cfg: LaunchersConfig, name: str) -> dict[str, Any]:
    """Lightweight inspection. Always returns a dict, never raises."""
    spec = get_launcher(cfg, name)
    if spec is None:
        return {"known": False, "running": False, "has_window": False, "error": f"no launcher named {name!r}"}
    proc = spec.get("process") or ""
    running, pid = _is_running_by_name(proc) if proc else (False, None)
    wins = _find_windows(spec)
    out: dict[str, Any] = {
        "known": True,
        "slug": spec["slug"],
        "name": spec.get("name", spec["slug"]),
        "process": proc,
        "running": bool(running or wins),
        "pid": pid,
        "has_window": bool(wins),
    }
    if wins:
        w = wins[0]
        out["hwnd"] = w.hwnd
        out["window_title"] = w.title
        if not pid:
            out["pid"] = w.pid
        if len(wins) > 1:
            out["window_count"] = len(wins)
    return out


def _send_hotkey(combo: Any) -> None:
    try:
        import pyautogui  # type: ignore[import-not-found]
    except Exception:
        return
    if isinstance(combo, list):
        for step in combo:
            if not step:
                continue
            if "+" in step:
                pyautogui.hotkey(*[k.strip() for k in step.split("+") if k.strip()])
            else:
                pyautogui.typewrite(step, interval=0.02)
            time.sleep(0.15)
        return
    if isinstance(combo, str):
        if "+" in combo:
            pyautogui.hotkey(*[k.strip() for k in combo.split("+") if k.strip()])
        else:
            pyautogui.press(combo)


def launch_app(cfg: LaunchersConfig, name: str) -> dict[str, Any]:
    """Try to start / focus an app. Returns a structured result dict."""
    spec = get_launcher(cfg, name)
    if spec is None:
        return {"ok": False, "method": None, "message": f"no launcher named {name!r}; call list_apps() to see available names"}
    slug = spec["slug"]

    # Step 1: already-running window → focus
    wins = _find_windows(spec)
    if wins:
        w = wins[0]
        ok = _force_foreground(w.hwnd)
        return {
            "ok": True,
            "method": "focus_existing_window",
            "slug": slug,
            "name": spec.get("name", slug),
            "hwnd": w.hwnd,
            "window_title": w.title,
            "pid": w.pid,
            "message": (
                f"activated existing window of {spec.get('name', slug)} "
                f"(hwnd=0x{w.hwnd:x}, pid={w.pid}); SetForegroundWindow={'ok' if ok else 'soft-fail'}"
            ),
        }

    # Step 2: shortcut
    shortcut = spec.get("shortcut")
    if shortcut:
        try:
            _send_hotkey(shortcut)
            time.sleep(0.4)
            wins2 = _find_windows(spec)
            return {
                "ok": True,
                "method": "shortcut",
                "slug": slug,
                "name": spec.get("name", slug),
                "shortcut": shortcut,
                "hwnd": (wins2[0].hwnd if wins2 else None),
                "message": f"launched {spec.get('name', slug)} via shortcut {shortcut!r}",
            }
        except Exception as e:
            last_err = f"shortcut failed: {e}"
        else:
            last_err = ""
    else:
        last_err = ""

    # Step 3: URI
    uri = spec.get("uri")
    if uri:
        try:
            # Use start via cmd so URIs like ms-settings: get routed correctly.
            subprocess.Popen(["cmd", "/c", "start", "", uri], shell=False)
            time.sleep(0.4)
            wins2 = _find_windows(spec)
            return {
                "ok": True,
                "method": "uri",
                "slug": slug,
                "name": spec.get("name", slug),
                "uri": uri,
                "hwnd": (wins2[0].hwnd if wins2 else None),
                "message": f"launched {spec.get('name', slug)} via uri {uri}",
            }
        except Exception as e:
            last_err = f"uri failed: {e}"

    # Step 4: exe alias / path
    exe = spec.get("exe")
    if exe:
        try:
            subprocess.Popen(exe, shell=True)
            time.sleep(0.5)
            wins2 = _find_windows(spec)
            return {
                "ok": True,
                "method": "exe",
                "slug": slug,
                "name": spec.get("name", slug),
                "exe": exe,
                "hwnd": (wins2[0].hwnd if wins2 else None),
                "message": f"launched {spec.get('name', slug)} via exe {exe!r}",
            }
        except Exception as e:
            last_err = f"exe failed: {e}"

    return {
        "ok": False,
        "slug": slug,
        "name": spec.get("name", slug),
        "method": None,
        "message": (
            f"could not launch {spec.get('name', slug)}; tried "
            f"{'shortcut, ' if shortcut else ''}{'uri, ' if uri else ''}{'exe' if exe else ''}"
            f"{'; ' + last_err if last_err else ''}"
        ),
    }


def focus_window(title_substring: str) -> dict[str, Any]:
    """Bring the first visible window whose title contains ``title_substring`` to the foreground."""
    if not title_substring:
        return {"ok": False, "message": "title_substring required"}
    wins = _find_windows({"window_title": title_substring})
    if not wins:
        return {"ok": False, "message": f"no visible window with title containing {title_substring!r}"}
    w = wins[0]
    ok = _force_foreground(w.hwnd)
    return {
        "ok": True,
        "hwnd": w.hwnd,
        "window_title": w.title,
        "pid": w.pid,
        "message": f"focused window 0x{w.hwnd:x} ({w.title!r}); ok={ok}",
    }


def catalog_for_prompt(cfg: LaunchersConfig) -> str:
    """Compact one-liner per launcher, injected at task start."""
    items = list_launchers(cfg)
    if not items:
        return ""
    lines = []
    for it in items:
        marks = []
        if it.get("shortcut"):
            marks.append("shortcut")
        if it.get("uri"):
            marks.append("uri")
        if it.get("exe"):
            marks.append("exe")
        if it.get("window_title") or it.get("window_title_re"):
            marks.append("window")
        flag = ", ".join(marks)
        lines.append(f"  - `{it['slug']}` ({it.get('name', it['slug'])}) — {flag}")
    return (
        "\n## Available `launch_app` slugs\n"
        "Pass any of these to `launch_app(name=...)`. The tool first checks if the app is already running "
        "(via process / window enumeration) — if so it just focuses the existing window without launching a duplicate. "
        "Use `check_app_running(name)` to inspect without launching.\n"
        + "\n".join(lines)
        + "\n"
    )


# ---------------------------------------------------------------------------
# Window discovery helper used by /regions calibration
# ---------------------------------------------------------------------------


def find_app_window(cfg: LaunchersConfig, app: str) -> WindowMatch | None:
    spec = get_launcher(cfg, app)
    if spec is None:
        return None
    wins = _find_windows(spec)
    return wins[0] if wins else None
