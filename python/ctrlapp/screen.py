"""Screen Sensor — 三级金字塔截图（design.md §4.5.3）。

L1 全屏 / L2 活动窗口 / L3 鼠标周边局部。
对外 API 同时返回：
- PIL.Image（送给 LLM 的可能已下采样版本）
- 缩放比例（用于把 LLM 回的坐标反算回物理坐标）
- 截图原点在虚拟屏幕坐标系中的偏移（用于 L2/L3）
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from enum import Enum

import imagehash
import mss
from PIL import Image

from .config import ScreenshotConfig
from .window import active_window, cursor_pos


class ScreenLevel(str, Enum):
    L1 = "fullscreen"
    L2 = "active_window"
    L3 = "cursor_local"


@dataclass
class Capture:
    level: ScreenLevel
    image: Image.Image           # 已可能下采样
    raw_size: tuple[int, int]    # 原始（物理）宽高
    sent_size: tuple[int, int]   # 实际发给 LLM 的宽高
    offset: tuple[int, int]      # 在虚拟屏幕坐标系中的左上角原点
    phash: str                   # 感知哈希，用于变化检测

    @property
    def scale_x(self) -> float:
        return self.raw_size[0] / self.sent_size[0] if self.sent_size[0] else 1.0

    @property
    def scale_y(self) -> float:
        return self.raw_size[1] / self.sent_size[1] if self.sent_size[1] else 1.0

    def model_to_screen(self, x: int, y: int) -> tuple[int, int]:
        """LLM 给出的截图内坐标 → 虚拟屏幕物理坐标。"""
        sx = int(round(x * self.scale_x)) + self.offset[0]
        sy = int(round(y * self.scale_y)) + self.offset[1]
        return sx, sy

    def png_bytes(self) -> bytes:
        buf = io.BytesIO()
        self.image.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    def jpeg_bytes(self, quality: int = 80) -> bytes:
        """JPEG-encoded version (RGB), used to keep request body small for L1/L2."""
        buf = io.BytesIO()
        img = self.image
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=int(quality), optimize=True, progressive=False)
        return buf.getvalue()

    def encoded_for_send(self, prefer_jpeg: bool, jpeg_quality: int = 80) -> tuple[bytes, str]:
        """Return (bytes, mime) suitable for an image_url data URL.
        prefer_jpeg=True → JPEG (good for big L1/L2 screenshots);
        prefer_jpeg=False → PNG (lossless, used for tiny L3 tiles & icon atlas)."""
        if prefer_jpeg:
            return self.jpeg_bytes(jpeg_quality), "image/jpeg"
        return self.png_bytes(), "image/png"


def _shrink(img: Image.Image, max_long_edge: int) -> Image.Image:
    if max_long_edge <= 0:
        return img
    w, h = img.size
    long_edge = max(w, h)
    if long_edge <= max_long_edge:
        return img
    scale = max_long_edge / long_edge
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def _phash(img: Image.Image) -> str:
    return str(imagehash.dhash(img, hash_size=16))


class ScreenSensor:
    def __init__(self, cfg: ScreenshotConfig) -> None:
        self.cfg = cfg

    # ---------- 三级捕获 ----------
    def capture(self, level: ScreenLevel) -> Capture:
        if level is ScreenLevel.L1:
            return self._capture_full()
        if level is ScreenLevel.L2:
            return self._capture_active_window()
        if level is ScreenLevel.L3:
            return self._capture_cursor_local()
        raise ValueError(level)

    # ---------- 实现 ----------
    def _grab(self, region: dict) -> Image.Image:
        with mss.mss() as sct:
            shot = sct.grab(region)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def _capture_full(self) -> Capture:
        with mss.mss() as sct:
            mon = sct.monitors[0]  # 虚拟屏幕（all monitors）
            region = {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}
        img = self._grab(region)
        raw = img.size
        sent = _shrink(img, self.cfg.l1_max_long_edge)
        return Capture(
            level=ScreenLevel.L1, image=sent, raw_size=raw, sent_size=sent.size,
            offset=(region["left"], region["top"]), phash=_phash(sent),
        )

    def _capture_active_window(self) -> Capture:
        win = active_window()
        if win is None or win.width <= 0 or win.height <= 0:
            # 退化为全屏
            return self._capture_full()
        region = {"left": win.left, "top": win.top, "width": win.width, "height": win.height}
        img = self._grab(region)
        raw = img.size
        sent = _shrink(img, self.cfg.l2_max_long_edge)
        return Capture(
            level=ScreenLevel.L2, image=sent, raw_size=raw, sent_size=sent.size,
            offset=(win.left, win.top), phash=_phash(sent),
        )

    def _capture_cursor_local(self) -> Capture:
        cx, cy = cursor_pos()
        r = self.cfg.l3_radius_px
        region = {"left": cx - r, "top": cy - r, "width": r * 2, "height": r * 2}
        img = self._grab(region)
        raw = img.size
        sent = _shrink(img, self.cfg.l3_max_long_edge)
        return Capture(
            level=ScreenLevel.L3, image=sent, raw_size=raw, sent_size=sent.size,
            offset=(region["left"], region["top"]), phash=_phash(sent),
        )

    def capture_around(self, sx: int, sy: int, radius: int) -> Capture:
        """Grab a square tile around an arbitrary virtual-screen coordinate.
        Used for pre-click target verification (no LLM); returns an L3-style
        Capture that is independent of the cursor position."""
        r = max(8, int(radius))
        region = {"left": sx - r, "top": sy - r, "width": r * 2, "height": r * 2}
        img = self._grab(region)
        raw = img.size
        sent = _shrink(img, self.cfg.l3_max_long_edge)
        return Capture(
            level=ScreenLevel.L3, image=sent, raw_size=raw, sent_size=sent.size,
            offset=(region["left"], region["top"]), phash=_phash(sent),
        )

    # ---------- 变化检测 ----------
    @staticmethod
    def similarity(a: str, b: str) -> float:
        """两张 dHash 相似度，1.0 = 完全相同。"""
        ha = imagehash.hex_to_hash(a)
        hb = imagehash.hex_to_hash(b)
        bits = ha.hash.size
        return 1.0 - (ha - hb) / bits
