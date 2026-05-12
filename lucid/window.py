"""Window utilities — 仅查询前台窗口位置/标题，不读 UIA 树（保持纯视觉精神）。"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass


@dataclass
class WindowInfo:
    title: str
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        # Exclusive right edge, matches Win32 RECT semantics: rect.right = left + width.
        return self.left + self.width

    @property
    def bottom(self) -> int:
        # Exclusive bottom edge, matches Win32 RECT semantics: rect.bottom = top + height.
        return self.top + self.height


def active_window() -> WindowInfo | None:
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return WindowInfo(
        title=buf.value,
        left=rect.left,
        top=rect.top,
        width=rect.right - rect.left,
        height=rect.bottom - rect.top,
    )


def cursor_pos() -> tuple[int, int]:
    if sys.platform != "win32":
        return (0, 0)
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)
