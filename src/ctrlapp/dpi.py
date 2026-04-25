"""Windows DPI / 多屏处理。

设计要点（design.md §4.5.1）：
- 进程声明 Per-Monitor V2 DPI Aware，避免系统自动缩放截图。
- 截图保留物理像素；缩放给模型时记录比例，模型回坐标后反算回物理坐标。
- 多显示器使用虚拟屏幕坐标系。
"""
from __future__ import annotations

import ctypes
import sys


def set_dpi_aware() -> None:
    """声明本进程为 Per-Monitor V2 DPI Aware。"""
    if sys.platform != "win32":
        return
    try:
        # Windows 10 1703+
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)  # PER_MONITOR_AWARE_V2
        return
    except (AttributeError, OSError):
        pass
    try:
        # Windows 8.1+
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """返回虚拟屏幕矩形 (left, top, width, height)。"""
    if sys.platform != "win32":
        return (0, 0, 1920, 1080)
    user32 = ctypes.windll.user32
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )
