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

pyautogui.FAILSAFE = True   # 鼠标移到左上角触发 abort
pyautogui.PAUSE = 0.0       # 我们自己控制延迟


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
        pyautogui.moveTo(x, y, duration=0.0)
        self._delay()

    def left_click(self, x: int | None = None, y: int | None = None) -> None:
        if x is not None:
            pyautogui.moveTo(x, y, duration=0.0)
        pyautogui.click()
        self._delay()

    def double_click(self, x: int | None = None, y: int | None = None) -> None:
        if x is not None:
            pyautogui.moveTo(x, y, duration=0.0)
        pyautogui.doubleClick()
        self._delay()

    def right_click(self, x: int | None = None, y: int | None = None) -> None:
        if x is not None:
            pyautogui.moveTo(x, y, duration=0.0)
        pyautogui.rightClick()
        self._delay()

    def middle_click(self, x: int | None = None, y: int | None = None) -> None:
        if x is not None:
            pyautogui.moveTo(x, y, duration=0.0)
        pyautogui.middleClick()
        self._delay()

    def left_mouse_down(self, x: int | None = None, y: int | None = None) -> None:
        if x is not None:
            pyautogui.moveTo(x, y, duration=0.0)
        pyautogui.mouseDown()
        self._delay()

    def left_mouse_up(self, x: int | None = None, y: int | None = None) -> None:
        if x is not None:
            pyautogui.moveTo(x, y, duration=0.0)
        pyautogui.mouseUp()
        self._delay()

    def left_click_drag(self, x: int, y: int) -> None:
        pyautogui.dragTo(x, y, duration=0.3, button="left")
        self._delay()

    def scroll(self, x: int | None, y: int | None, direction: str, amount: int) -> None:
        if x is not None:
            pyautogui.moveTo(x, y, duration=0.0)
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
        """打字。默认走剪贴板 + Ctrl+V，避开 IME / 键盘布局干扰。

        策略由 cfg.chinese_input 控制：
          - "clipboard"（默认）：**任何文本**都走剪贴板；带控制符 / 换行的
            会拆为「粘贴可见部分 -> 按 Enter / Tab」以保证换行准确触发。
          - "unicode_sendinput"：全部走 typewrite（只能输 ASCII）。
        """
        if not text:
            return
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

    def _paste_text(self, text: str) -> None:
        backup = ""
        try:
            backup = pyperclip.paste()
        except Exception:
            pass
        try:
            for kind, payload in self._split_segments(text):
                if kind == "text":
                    if not payload:
                        continue
                    pyperclip.copy(payload)
                    # 等剪贴板同步进去再粘贴
                    time.sleep(0.03)
                    pyautogui.hotkey("ctrl", "v")
                    time.sleep(0.05)
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
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)
        self._delay()

    # ---------- 等待 ----------
    @staticmethod
    def wait(seconds: float) -> None:
        time.sleep(max(0.0, float(seconds)))
