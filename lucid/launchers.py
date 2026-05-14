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
from .window import active_window as get_active_window


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
# 默认值不再写死在这个文件里 —— 每个 App 在 ``lucid.apps.<slug>`` 模块里自己声明
# ``LAUNCHER = {...}``。新增 App 只要加一个文件。运行时用户可以通过
# ``<user data>/launchers.json`` 覆盖（前端 UI / agent 的 ``update_launcher`` meta tool）。


def _default_launchers() -> dict[str, dict[str, Any]]:
    return apps_pkg.all_launchers()


# ---------------------------------------------------------------------------
# Path / persistence
# ---------------------------------------------------------------------------


def _user_data_dir() -> Path:
    return Path.home() / ".lucid"


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
        if _process_name_matches(target, n):
            return True, int(p.info.get("pid") or 0) or None
    return False, None


def _process_name_matches(pattern: str, actual: str) -> bool:
    pattern = (pattern or "").strip().lower()
    actual = (actual or "").strip().lower()
    if not pattern or not actual:
        return False
    if pattern == actual:
        return True
    try:
        return bool(re.fullmatch(pattern, actual, re.IGNORECASE))
    except re.error:
        return False


def _title_matches(spec: dict[str, Any], title: str) -> bool:
    title_sub = spec.get("window_title")
    title_re = spec.get("window_title_re")
    pat = None
    if title_re:
        try:
            pat = re.compile(title_re, re.IGNORECASE)
        except re.error:
            pat = None
    if pat is not None:
        return bool(pat.search(title or ""))
    if title_sub:
        return str(title_sub) in (title or "")
    return False


def _launch_timeout(spec: dict[str, Any], default: float) -> float:
    try:
        return max(0.1, float(spec.get("launch_timeout_s") or default))
    except Exception:
        return default


def _foreground_title() -> str | None:
    try:
        info = get_active_window()
    except Exception:
        info = None
    if not info or not info.title:
        return None
    return info.title


def _foreground_window_match(spec: dict[str, Any]) -> WindowMatch | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd or not user32.IsWindowVisible(hwnd):
        return None
    length = int(user32.GetWindowTextLengthW(hwnd) or 0)
    if length <= 0:
        return None
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value or ""
    if not _title_matches(spec, title):
        return None
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    pid_int = int(pid.value or 0)
    proc_target = (spec.get("process") or "").lower()
    if proc_target:
        try:
            import psutil  # type: ignore[import-not-found]
            proc = psutil.Process(pid_int)
            pname = (proc.name() or "").lower()
            if not _process_name_matches(proc_target, pname):
                return None
        except Exception:
            return None
    return WindowMatch(hwnd=hwnd, title=title, pid=pid_int)


def _find_windows(spec: dict[str, Any]) -> list[WindowMatch]:
    """Enumerate visible top-level windows that match the spec's title rules.

    Also filters by ``spec["process"]`` when provided — title-only matching is
    too brittle (e.g. an Explorer window whose tab/folder name happens to
    contain "计算器" would otherwise match the Calculator launcher).
    """
    if sys.platform != "win32":
        return []
    if not spec.get("window_title") and not spec.get("window_title_re"):
        return []
    proc_target = (spec.get("process") or "").lower()
    pid_to_pname: dict[int, str] = {}
    if proc_target:
        try:
            import psutil  # type: ignore[import-not-found]
            for p in psutil.process_iter(["name", "pid"]):
                try:
                    pid_to_pname[int(p.info.get("pid") or 0)] = (p.info.get("name") or "").lower()
                except Exception:
                    continue
        except Exception:
            pid_to_pname = {}
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
        if not _title_matches(spec, title):
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid_int = int(pid.value)
        # When a process name is declared, require the window to actually
        # belong to that process. Skip the check only when psutil failed to
        # enumerate processes (pid_to_pname empty) — fall back to title-only.
        if proc_target and pid_to_pname:
            pname = pid_to_pname.get(pid_int, "")
            if not _process_name_matches(proc_target, pname):
                return True
        found.append(WindowMatch(hwnd=int(hwnd), title=title, pid=pid_int))
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


def _wait_for_windows(spec: dict[str, Any], timeout: float = 1.5, interval: float = 0.1) -> list[WindowMatch]:
    """Poll `_find_windows(spec)` up to `timeout` seconds; return the first
    non-empty result, or `[]` if the deadline elapses. Used after we trigger
    a launch (hotkey / URI / exe) so that slow-painting windows like WeChat's
    main panel get a fair chance to appear before we declare failure."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        wins = _find_windows(spec)
        if wins:
            return wins
        fg = _foreground_window_match(spec)
        if fg is not None:
            return [fg]
        if time.monotonic() >= deadline:
            return []
        time.sleep(interval)


def launch_app(cfg: LaunchersConfig, name: str, page: str | None = None) -> dict[str, Any]:
    """Try to start / focus an app. Returns a structured result dict."""
    spec = get_launcher(cfg, name)
    if spec is None:
        return {"ok": False, "method": None, "message": (
            f"no launcher named {name!r}; call list_apps() to see available names. "
            f"If this is an app the user wants but isn't registered yet, you can create it on the fly via "
            f"`update_launcher(name={name!r}, exe='<alias-or-abs-path>', process='<X.exe>', window_title_re='<regex>')` — "
            f"unknown slugs are auto-created."
        )}
    slug = spec["slug"]

    # If `page` is provided, the caller wants a URI deep-link (e.g.
    # ms-settings:display) — skip the focus-existing path and go straight to
    # the URI step, otherwise the existing window is brought up at its current
    # page and the deep-link is silently ignored.
    page_clean = (page or "").strip()
    if page_clean and spec.get("uri"):
        deeplink_uri = f"{spec['uri']}{page_clean}"
        try:
            subprocess.Popen(["cmd", "/c", "start", "", deeplink_uri], shell=False)
            wins2 = _wait_for_windows(spec, timeout=_launch_timeout(spec, 2.5))
            if wins2:
                _force_foreground(wins2[0].hwnd)
                return {
                    "ok": True,
                    "method": "uri_deeplink",
                    "slug": slug,
                    "name": spec.get("name", slug),
                    "uri": deeplink_uri,
                    "page": page_clean,
                    "hwnd": wins2[0].hwnd,
                    "window_title": wins2[0].title,
                    "pid": wins2[0].pid,
                    "foreground_title": _foreground_title(),
                    "message": f"launched {spec.get('name', slug)} via uri {deeplink_uri}",
                }
            last_err = (
                f"uri deep-link {deeplink_uri} fired but no matching window appeared "
                f"within {_launch_timeout(spec, 2.5):.1f}s"
            )
        except Exception as e:
            last_err = f"uri deep-link failed: {e}"

    # Step 1: already-running window → focus
    wins = _find_windows(spec)
    if wins:
        w = wins[0]
        ok = _force_foreground(w.hwnd)
        msg = (
            f"activated existing window of {spec.get('name', slug)} "
            f"(hwnd=0x{w.hwnd:x}, pid={w.pid}); SetForegroundWindow={'ok' if ok else 'soft-fail'}"
        )
        extra: dict[str, Any] = {}
        # For browsers, surface whether the *existing* instance has CDP enabled.
        # If it doesn't, an `--remote-debugging-port=9222` flag was not on the
        # original launch and `read_webpage(active_tab=true)` will time out —
        # tell the model now so it can degrade to a screenshot+OCR path.
        if slug in ("chrome", "edge"):
            try:
                from .webread import cdp_probe
                cdp_ok = cdp_probe(port=9222, timeout_s=0.4)
            except Exception:
                cdp_ok = False
            extra["cdp_available"] = cdp_ok
            if cdp_ok:
                msg += "; CDP enabled at :9222 (read_webpage(active_tab=true) OK)"
            else:
                msg += (
                    "; CDP NOT enabled on this existing instance — "
                    "read_webpage(active_tab=true) WILL FAIL. To enable: close ALL "
                    f"{slug} windows, then call launch_app('{slug}') again so we can "
                    "start it with --remote-debugging-port=9222. Otherwise use a "
                    "screenshot/OCR path for content."
                )
        return {
            "ok": True,
            "method": "focus_existing_window",
            "slug": slug,
            "name": spec.get("name", slug),
            "hwnd": w.hwnd,
            "window_title": w.title,
            "foreground_title": _foreground_title(),
            "pid": w.pid,
            "message": msg,
            **extra,
        }

    # Step 2: shortcut
    shortcut = spec.get("shortcut")
    if shortcut:
        try:
            _send_hotkey(shortcut)
            wait_s = _launch_timeout(spec, 1.5)
            wins2 = _wait_for_windows(spec, timeout=wait_s)
            note = ""
            if not wins2:
                # Many app hotkeys (e.g. WeChat's Ctrl+Alt+W) are TOGGLES:
                # if the app's main window was already visible but our title /
                # process matcher missed it in step 1, the hotkey just *hid* it.
                # Detection: nothing visible after the hotkey + a generous
                # wait. Recovery: send the same hotkey again to toggle back
                # on, then poll once more. This is harmless when the first
                # press was a real "show" that simply hadn't painted yet —
                # in that case the second press would hide it again, BUT we
                # only re-send when wins2 is empty, so by definition there
                # was nothing to hide.
                _send_hotkey(shortcut)
                wins2 = _wait_for_windows(spec, timeout=wait_s)
                if wins2:
                    note = " (hotkey is a toggle; first press hid an existing window, second press restored it)"
            if wins2:
                w = wins2[0]
                _force_foreground(w.hwnd)
                return {
                    "ok": True,
                    "method": "shortcut",
                    "slug": slug,
                    "name": spec.get("name", slug),
                    "shortcut": shortcut,
                    "hwnd": w.hwnd,
                    "window_title": w.title,
                    "foreground_title": _foreground_title(),
                    "pid": w.pid,
                    "message": f"launched {spec.get('name', slug)} via shortcut {shortcut!r}{note}",
                }
            # No window after two attempts → fall through to next launch
            # method, but remember the hint.
            last_err = (
                f"shortcut {shortcut!r} fired but no matching window appeared "
                f"within 3s (hotkey may not be registered, or app failed to start); "
                f"trying next method"
            )
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
            wins2 = _wait_for_windows(spec, timeout=_launch_timeout(spec, 2.0))
            if wins2:
                _force_foreground(wins2[0].hwnd)
                return {
                    "ok": True,
                    "method": "uri",
                    "slug": slug,
                    "name": spec.get("name", slug),
                    "uri": uri,
                    "hwnd": wins2[0].hwnd,
                    "window_title": wins2[0].title,
                    "foreground_title": _foreground_title(),
                    "pid": wins2[0].pid,
                    "message": f"launched {spec.get('name', slug)} via uri {uri}",
                }
            last_err = (
                f"uri {uri!r} fired but no matching window appeared within "
                f"{_launch_timeout(spec, 2.0):.1f}s; trying next method"
            )
        except Exception as e:
            last_err = f"uri failed: {e}"

    # Step 4: exe alias / path
    exe = spec.get("exe")
    if exe:
        try:
            subprocess.Popen(exe, shell=True)
            wins2 = _wait_for_windows(spec, timeout=_launch_timeout(spec, 2.0))
            if wins2:
                _force_foreground(wins2[0].hwnd)
                return {
                    "ok": True,
                    "method": "exe",
                    "slug": slug,
                    "name": spec.get("name", slug),
                    "exe": exe,
                    "hwnd": wins2[0].hwnd,
                    "window_title": wins2[0].title,
                    "foreground_title": _foreground_title(),
                    "pid": wins2[0].pid,
                    "message": f"launched {spec.get('name', slug)} via exe {exe!r}",
                }
            running, pid = _is_running_by_name(spec.get("process") or "")
            fg_title = _foreground_title()
            if running:
                return {
                    "ok": True,
                    "method": "exe",
                    "slug": slug,
                    "name": spec.get("name", slug),
                    "exe": exe,
                    "pid": pid,
                    "pending_window": True,
                    "foreground_title": fg_title,
                    "message": (
                        f"spawned {spec.get('name', slug)} via exe {exe!r}; process is running "
                        f"but no matching top-level window appeared within {_launch_timeout(spec, 2.0):.1f}s "
                        "(likely cold start / splash)."
                    ),
                }
            last_err = (
                f"exe {exe!r} started but no matching window or process appeared within "
                f"{_launch_timeout(spec, 2.0):.1f}s"
            )
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
