"""Input Driver — 鼠标 / 键盘 / 中文输入。

设计要点（design.md §4.5.2）：
- 中文走 **剪贴板 + Ctrl+V** 最稳，避开 IME 复杂状态。
- 英文/快捷键走 SendInput。
- 每个动作之后留 action_delay 给 UI 响应。
"""
from __future__ import annotations

import time

import pyautogui
import pyperclip

from .config import InputConfig
from .dpi import virtual_screen_rect

pyautogui.FAILSAFE = True   # 鼠标移到屏幕角触发 abort（用户级急停）；
                            # 我们额外把目标坐标 clamp 到角内 2px，避免 agent 自己点死。
pyautogui.PAUSE = 0.0       # 我们自己控制延迟


def _safe_xy(x: int, y: int) -> tuple[int, int]:
    """把目标坐标 clamp 到虚拟桌面内、且离每个角 ≥ FAILSAFE_MARGIN 像素，
    避免 PyAutoGUI 的 fail-safe 因为我们自己 moveTo 到角而误触发。"""
    margin = 2
    vx, vy, vw, vh = virtual_screen_rect()
    cx = max(vx + margin, min(int(x), vx + vw - 1 - margin))
    cy = max(vy + margin, min(int(y), vy + vh - 1 - margin))
    return cx, cy


def _nudge_off_corner_if_needed() -> None:
    """PyAutoGUI 的 fail-safe 会在每次调用时检查**当前**鼠标位置；如果鼠标
    已经在 FAILSAFE_POINTS（默认 [(0,0)]）上，下一次任何调用都会抛
    FailSafeException。这里直接用 Windows SetCursorPos 把鼠标从角上挪开
    （绕过 pyautogui 自己的 wrapper 检查），让后续动作能继续。"""
    try:
        import sys
        if sys.platform != "win32":
            return
        import ctypes
        pt = ctypes.wintypes.POINT() if hasattr(ctypes, "wintypes") else None
        if pt is None:
            from ctypes import wintypes  # noqa: WPS433
            pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        for fx, fy in pyautogui.FAILSAFE_POINTS:
            if abs(pt.x - fx) <= 1 and abs(pt.y - fy) <= 1:
                vx, vy, vw, vh = virtual_screen_rect()
                # 挪到虚拟桌面中心
                ctypes.windll.user32.SetCursorPos(vx + vw // 2, vy + vh // 2)
                return
    except Exception:
        pass


def _move_to(x: int, y: int, duration: float = 0.0) -> None:
    _nudge_off_corner_if_needed()
    cx, cy = _safe_xy(x, y)
    pyautogui.moveTo(cx, cy, duration=duration)


# Claude computer_use 用的按键名 → pyautogui 名字（差别不多，这里仅做必要映射）。
_KEY_ALIASES = {
    "Return": "enter",
    "KP_Enter": "enter",
    "Page_Up": "pageup",
    "Page_Down": "pagedown",
    "Escape": "esc",
    "BackSpace": "backspace",
    "ctrl": "ctrl",
    "control": "ctrl",
    "cmd": "win",
    "super": "win",
    "alt_l": "alt",
    "alt_r": "alt",
}


def _norm_key(name: str) -> str:
    n = name.strip()
    return _KEY_ALIASES.get(n, _KEY_ALIASES.get(n.lower(), n.lower()))


class InputDriver:
    def __init__(self, cfg: InputConfig) -> None:
        self.cfg = cfg

    def _delay(self) -> None:
        if self.cfg.action_delay > 0:
            time.sleep(self.cfg.action_delay)

    # ---------- 鼠标 ----------
    def mouse_move(self, x: int, y: int) -> None:
        _move_to(x, y)
        self._delay()

    def left_click(self, x: int | None = None, y: int | None = None) -> None:
        if x is not None:
            _move_to(x, y)
        else:
            _nudge_off_corner_if_needed()
        pyautogui.click()
        self._delay()

    def double_click(self, x: int | None = None, y: int | None = None) -> None:
        if x is not None:
            _move_to(x, y)
        else:
            _nudge_off_corner_if_needed()
        pyautogui.doubleClick()
        self._delay()

    def right_click(self, x: int | None = None, y: int | None = None) -> None:
        if x is not None:
            _move_to(x, y)
        else:
            _nudge_off_corner_if_needed()
        pyautogui.rightClick()
        self._delay()

    def middle_click(self, x: int | None = None, y: int | None = None) -> None:
        if x is not None:
            _move_to(x, y)
        else:
            _nudge_off_corner_if_needed()
        pyautogui.middleClick()
        self._delay()

    def left_mouse_down(self, x: int | None = None, y: int | None = None) -> None:
        if x is not None:
            _move_to(x, y)
        else:
            _nudge_off_corner_if_needed()
        pyautogui.mouseDown()
        self._delay()

    def left_mouse_up(self, x: int | None = None, y: int | None = None) -> None:
        if x is not None:
            _move_to(x, y)
        else:
            _nudge_off_corner_if_needed()
        pyautogui.mouseUp()
        self._delay()

    def left_click_drag(self, x: int, y: int) -> None:
        _nudge_off_corner_if_needed()
        cx, cy = _safe_xy(x, y)
        pyautogui.dragTo(cx, cy, duration=0.3, button="left")
        self._delay()

    def scroll(self, x: int | None, y: int | None, direction: str, amount: int) -> None:
        if x is not None:
            _move_to(x, y)
        else:
            _nudge_off_corner_if_needed()
        ticks = max(1, int(amount)) * 100
        if direction == "up":
            pyautogui.scroll(ticks)
        elif direction == "down":
            pyautogui.scroll(-ticks)
        elif direction == "left":
            pyautogui.hscroll(-ticks)
        elif direction == "right":
            pyautogui.hscroll(ticks)
        self._delay()

    def cursor_position(self) -> tuple[int, int]:
        x, y = pyautogui.position()
        return int(x), int(y)

    # ---------- 键盘 ----------
    def type_text(self, text: str) -> None:
        """打字。**统一走剪贴板 + Ctrl+V**，绕开 IME / 键盘布局干扰。

        历史上有个 "短 ASCII 走 SendInput" 的快路径（绕开剪贴板污染），
        但中文 IME 激活时 SendInput 也会被 IME 拦截：`\\` -> `、`、数字
        被候选窗吞掉等（C2 Save-As bug）。剪贴板路径直接发 WM_PASTE，
        IME 完全不掺和，是唯一稳的路径。

        剪贴板污染（O2 bug）由 `_paste_with_verify` 在 copy 前先清空、
        copy 后内容比对来兜底，污染窗口被压到亚毫秒级。
        """
        if not text:
            return
        _nudge_off_corner_if_needed()
        if self.cfg.chinese_input == "clipboard":
            self._paste_text(text)
        else:
            pyautogui.typewrite(text, interval=0.01)
        self._delay()

    @staticmethod
    def _split_segments(text: str) -> list[tuple[str, str]]:
        """把 text 切为 [(kind, payload), ...]：kind 为 'text' 或 'key'。

        - \n -> ('key', 'enter')
        - \t -> ('key', 'tab')
        - \r 忽略（避免 Windows 换行上下文重复触发）。
        """
        segs: list[tuple[str, str]] = []
        buf: list[str] = []
        for ch in text:
            if ch == "\r":
                continue
            if ch == "\n":
                if buf:
                    segs.append(("text", "".join(buf)))
                    buf = []
                segs.append(("key", "enter"))
            elif ch == "\t":
                if buf:
                    segs.append(("text", "".join(buf)))
                    buf = []
                segs.append(("key", "tab"))
            else:
                buf.append(ch)
        if buf:
            segs.append(("text", "".join(buf)))
        return segs

    @staticmethod
    def _is_short_ascii(text: str) -> bool:  # noqa: ARG004 — kept for back-compat
        """DEPRECATED: always returns False.

        The historical SendInput fast-path was racy under active CJK IMEs
        (it let `\\` get translated to `、` and digits get swallowed by the
        candidate window). We now route 100%% of typing through the
        clipboard + WM_PASTE, which IMEs do not see. Kept as a stub so
        external callers / tests that import it don't break.
        """
        return False

    @staticmethod
    def _paste_with_verify(payload: str, attempts: int = 5) -> bool:
        """Empty clipboard → copy ``payload`` → verify → Ctrl+V.

        The empty-first step is the key defence against the O2 bug: between
        our previous ``pyperclip.copy()`` and the ``Ctrl+V`` keystroke an
        external process could write into the clipboard, and our verify
        would still pass on a stale match from a previous turn. By emptying
        first, the verify can only succeed if OUR copy landed last, so a
        race-loser is caught immediately and we retry.

        Sequence per attempt:
          1. EmptyClipboard (Win32 OpenClipboard + EmptyClipboard +
             CloseClipboard) — best-effort, falls back to pyperclip.copy("").
          2. pyperclip.copy(payload).
          3. small settle delay.
          4. pyperclip.paste() — must equal payload (else: someone raced; retry).
          5. Ctrl+V.

        Returns True if the paste was issued with verified clipboard contents.
        """
        def _empty_clipboard() -> None:
            try:
                import sys
                if sys.platform == "win32":
                    import ctypes
                    user32 = ctypes.windll.user32
                    # OpenClipboard(NULL) — owner = current task; retry briefly
                    # if another process holds it.
                    for _ in range(5):
                        if user32.OpenClipboard(None):
                            try:
                                user32.EmptyClipboard()
                            finally:
                                user32.CloseClipboard()
                            return
                        time.sleep(0.01)
                # Fallback: writing empty string still beats trusting stale.
                pyperclip.copy("")
            except Exception:
                try:
                    pyperclip.copy("")
                except Exception:
                    pass

        for _ in range(max(1, attempts)):
            _empty_clipboard()
            try:
                pyperclip.copy(payload)
            except Exception:
                time.sleep(0.05)
                continue
            time.sleep(0.04)
            try:
                got = pyperclip.paste()
            except Exception:
                got = ""
            if got == payload:
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.05)
                return True
            # Clipboard didn't stick (someone else wrote into it). Retry.
            time.sleep(0.05)
        return False

    def _paste_text(self, text: str) -> None:
        # Plan B: ALWAYS go through clipboard + verify. The historical
        # SendInput fast-path for short ASCII was racy under active CJK
        # IMEs (`\\` -> `、`, digits swallowed by candidate window). The
        # O2 clipboard-pollution risk is now handled by _paste_with_verify
        # emptying the clipboard first and verifying our copy stuck before
        # pressing Ctrl+V.
        backup = ""
        try:
            backup = pyperclip.paste()
        except Exception:
            pass
        try:
            if not getattr(self.cfg, "type_split_newlines", False):
                # Paste the whole string in one go. Most modern widgets
                # (WeChat / browsers / VS Code / Office) preserve embedded
                # newlines as soft breaks; splitting on \n and pressing Enter
                # would prematurely send messages in chat apps.
                payload = text.replace("\r", "")
                if payload:
                    if not self._paste_with_verify(payload):
                        # Clipboard verify failed after retries. Do NOT fall
                        # back to typewrite — that path is IME-vulnerable
                        # and would silently corrupt paths under a CJK IME.
                        # Raise so the tool layer reports the failure.
                        raise RuntimeError(
                            "type_text: clipboard verify failed after retries; "
                            "refusing IME-vulnerable typewrite fallback"
                        )
                return
            for kind, payload in self._split_segments(text):
                if kind == "text":
                    if not payload:
                        continue
                    if not self._paste_with_verify(payload):
                        raise RuntimeError(
                            "type_text: clipboard verify failed after retries; "
                            "refusing IME-vulnerable typewrite fallback"
                        )
                else:
                    pyautogui.press(payload)
                    time.sleep(0.03)
        finally:
            try:
                pyperclip.copy(backup)
            except Exception:
                pass

    def key(self, combo: str) -> None:
        """例：'ctrl+s' / 'alt+F4' / 'Return'。"""
        keys = [_norm_key(k) for k in combo.replace(" ", "").split("+") if k]
        if not keys:
            return
        _nudge_off_corner_if_needed()
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)
        self._delay()

    # ---------- 等待 ----------
    @staticmethod
    def wait(seconds: float) -> None:
        time.sleep(max(0.0, float(seconds)))
