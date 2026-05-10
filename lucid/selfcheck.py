"""Phase 1.5 适配自检：多屏 / HiDPI / win+r alias。

提供函数式 API；CLI 入口：

    python -m lucid.selfcheck            # 跑所有自检
    python -m lucid.selfcheck monitors   # 仅多屏 + DPI 报告
    python -m lucid.selfcheck winr       # 仅 Win+R alias 自检（会真按键！）
    python -m lucid.selfcheck click      # 点击坐标偏差自检（HiDPI 标度差异回退）

设计要点见 design.md §3.2 / §4.5.3 / §4.6.3：
* 多屏布局变化时需要重新探测 `[screenshot].l1_max_long_edge` 上限——
  这里只是 *报告* 当前虚拟屏几何，让上层（loop.py）决定是否要调小长边。
* HiDPI：从 ctypes 读每屏 DPI，与第一屏的 96/120/144/168 不同则提示。
* Win+R：发送热键后 1.2s 截 L2 活动窗口，简单判断标题是否包含
  「Run」/「运行」/「执行」；失败时给出可能的国际化键名。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from typing import List, Optional


@dataclass
class MonitorInfo:
    index: int
    left: int
    top: int
    width: int
    height: int
    dpi_x: int
    dpi_y: int
    scale_pct: int  # round(dpi_x / 96 * 100)


def list_monitors() -> List[MonitorInfo]:
    """枚举所有显示器，附带 per-monitor DPI（Windows 8.1+）。"""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    shcore = None
    try:
        shcore = ctypes.windll.shcore  # GetDpiForMonitor
    except OSError:
        pass

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    monitors: list[tuple[int, int, int, int, int]] = []  # hmonitor + rect

    def _cb(hmon, _hdc, lprc, _data):
        r = lprc.contents
        monitors.append((int(hmon), r.left, r.top, r.right, r.bottom))
        return 1

    user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(_cb), 0)

    out: List[MonitorInfo] = []
    for i, (hmon, l, t, r, b) in enumerate(monitors):
        dx = dy = 96
        if shcore is not None:
            dpix = ctypes.c_uint()
            dpiy = ctypes.c_uint()
            try:
                # MDT_EFFECTIVE_DPI = 0
                if shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dpix), ctypes.byref(dpiy)) == 0:
                    dx, dy = int(dpix.value), int(dpiy.value)
            except OSError:
                pass
        out.append(
            MonitorInfo(
                index=i,
                left=l,
                top=t,
                width=r - l,
                height=b - t,
                dpi_x=dx,
                dpi_y=dy,
                scale_pct=round(dx / 96.0 * 100),
            )
        )
    return out


def report_monitors() -> dict:
    mons = list_monitors()
    virt_w = max((m.left + m.width for m in mons), default=0) - min((m.left for m in mons), default=0)
    virt_h = max((m.top + m.height for m in mons), default=0) - min((m.top for m in mons), default=0)
    scales = sorted({m.scale_pct for m in mons})
    long_edge = max(virt_w, virt_h)
    suggestions = []
    if len(scales) > 1:
        suggestions.append(
            f"检测到混合 DPI（{scales}%），点击坐标会按 per-monitor 标度，建议保留 verify_click_with_l3 = true。"
        )
    if long_edge > 3840:
        suggestions.append(
            f"虚拟屏长边 {long_edge}px 偏大，建议把 [screenshot].l1_max_long_edge 调到 1568 或更小。"
        )
    return {
        "monitors": [asdict(m) for m in mons],
        "virtual_width": virt_w,
        "virtual_height": virt_h,
        "long_edge": long_edge,
        "distinct_scales_pct": scales,
        "suggestions": suggestions,
    }


def winr_alias_check(timeout_s: float = 1.5) -> dict:
    """按 Win+R，等窗口出现，读取活动窗口标题判断本地化别名是否生效。"""
    if sys.platform != "win32":
        return {"ok": False, "reason": "non-windows"}
    try:
        import pyautogui  # noqa: WPS433
    except Exception as e:  # pragma: no cover
        return {"ok": False, "reason": f"pyautogui import failed: {e}"}

    pyautogui.hotkey("win", "r")
    time.sleep(timeout_s)

    # 读活动窗口标题
    import ctypes
    from ctypes import wintypes

    u32 = ctypes.windll.user32
    hwnd = u32.GetForegroundWindow()
    length = u32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    u32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value or ""

    keywords = ("Run", "运行", "执行", "Ausführen", "Exécuter", "ファイル名を指定")
    matched = any(k.lower() in title.lower() for k in keywords)

    # 关闭对话框
    pyautogui.press("escape")

    return {
        "ok": matched,
        "active_title": title,
        "tried_keywords": list(keywords),
        "hint": (
            "Win+R 似乎未弹出预期对话框；请确认本地化键名，"
            "或在 prompt 中改用「按 Win 键 → type 程序名 → 回车」流程。"
            if not matched else
            "Win+R 别名工作正常。"
        ),
    }


def click_dpi_drift_check(point: Optional[tuple[int, int]] = None) -> dict:
    """点击给定屏幕坐标后，比较点击前/后 L3 鼠标周边截图的 dHash 距离，
    判断是否真的发生了 UI 变化；用来在 HiDPI 标度差异下做粗略坐标自检。

    若不传 point，则取虚拟屏中点（不会造成任何破坏性副作用，仅移动鼠标点击空白处）。
    """
    if sys.platform != "win32":
        return {"ok": False, "reason": "non-windows"}
    try:
        import pyautogui  # noqa: WPS433
        from PIL import Image  # noqa: WPS433
        import mss  # noqa: WPS433
    except Exception as e:  # pragma: no cover
        return {"ok": False, "reason": f"deps missing: {e}"}

    mons = list_monitors()
    if point is None:
        # 默认取主屏幕中心，避开屏幕边角
        m = mons[0] if mons else None
        if m is None:
            return {"ok": False, "reason": "no monitors"}
        point = (m.left + m.width // 2, m.top + m.height // 2)

    x, y = point
    radius = 100
    region = {"left": x - radius, "top": y - radius, "width": 2 * radius, "height": 2 * radius}

    def _grab() -> Image.Image:
        with mss.mss() as sct:
            shot = sct.grab(region)
            return Image.frombytes("RGB", shot.size, shot.rgb)

    def _dhash(img: Image.Image) -> int:
        small = img.convert("L").resize((9, 8))
        bits = 0
        idx = 0
        for row in range(8):
            for col in range(8):
                if small.getpixel((col, row)) < small.getpixel((col + 1, row)):
                    bits |= (1 << idx)
                idx += 1
        return bits

    before = _dhash(_grab())
    pyautogui.moveTo(x, y, duration=0.05)
    pyautogui.click()
    time.sleep(0.3)
    after = _dhash(_grab())

    distance = bin(before ^ after).count("1")
    return {
        "ok": True,
        "point": [x, y],
        "dhash_distance": distance,
        "interpretation": (
            "变化明显（点击触达了某个 UI 元素）"
            if distance >= 4
            else "几乎无变化（可能点中空白；HiDPI 下需检查 DPI awareness）"
        ),
    }


# ----------------- CLI -----------------

def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="lucid.selfcheck")
    parser.add_argument(
        "what",
        nargs="?",
        choices=["monitors", "winr", "click", "all"],
        default="all",
    )
    args = parser.parse_args(argv)
    result = {}
    if args.what in ("monitors", "all"):
        result["monitors"] = report_monitors()
    if args.what in ("winr", "all"):
        result["winr"] = winr_alias_check()
    if args.what in ("click", "all"):
        result["click"] = click_dpi_drift_check()
    _print_json(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
