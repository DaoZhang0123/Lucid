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

    def virtual_size(self) -> tuple[int, int]:
        """Return (width, height) of the virtual desktop without grabbing
        pixels. Cheap; used to seed the tool schema dimensions when the run
        is configured to skip the initial L1 capture."""
        with mss.mss() as sct:
            mon = sct.monitors[0]
            return int(mon["width"]), int(mon["height"])

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

    def capture_region(self, left: int, top: int, width: int, height: int,
                       max_long_edge: int | None = None) -> Capture:
        """Grab an arbitrary screen rect and return an L2-tagged Capture (so
        history compression treats it like an active-window screenshot).
        Used by R2 launch_app to ship the new App's client area to the model.

        ``max_long_edge`` defaults to the L2 limit; pass 0 to skip downscaling.
        """
        w = max(1, int(width))
        h = max(1, int(height))
        region = {"left": int(left), "top": int(top), "width": w, "height": h}
        img = self._grab(region)
        raw = img.size
        limit = self.cfg.l2_max_long_edge if max_long_edge is None else int(max_long_edge)
        sent = _shrink(img, limit)
        return Capture(
            level=ScreenLevel.L2, image=sent, raw_size=raw, sent_size=sent.size,
            offset=(int(left), int(top)), phash=_phash(sent),
        )

    # ---------- 变化检测 ----------
    @staticmethod
    def similarity(a: str, b: str) -> float:
        """两张 dHash 相似度，1.0 = 完全相同。"""
        ha = imagehash.hex_to_hash(a)
        hb = imagehash.hex_to_hash(b)
        bits = ha.hash.size
        return 1.0 - (ha - hb) / bits

    @staticmethod
    def diff_bbox(pre: Image.Image, post: Image.Image,
                  min_area_ratio: float = 0.05,
                  pixel_threshold: int = 25) -> tuple[int, int, int, int] | None:
        """Find the bounding box of the largest contiguous change between two
        same-size images. Used by R2 launch_app to locate the newly-appeared
        window's bbox in screen coords. Returns (x, y, w, h) in the IMAGE
        coordinate system (caller scales back to screen coords if needed),
        or None if change is too small / too scattered.
        """
        from PIL import ImageChops, ImageFilter
        if pre.size != post.size:
            return None
        a = pre.convert("L")
        b = post.convert("L")
        diff = ImageChops.difference(a, b)
        # Threshold to binary (anything brighter than `pixel_threshold` counts)
        diff = diff.point(lambda v: 255 if v > pixel_threshold else 0)
        # Eat away tiny noise (cursor blink, fan-spinner pixels)
        diff = diff.filter(ImageFilter.MaxFilter(5))
        bbox = diff.getbbox()
        if not bbox:
            return None
        x0, y0, x1, y1 = bbox
        w, h = x1 - x0, y1 - y0
        total = pre.size[0] * pre.size[1]
        if total <= 0:
            return None
        if (w * h) / total < float(min_area_ratio):
            return None
        return (int(x0), int(y0), int(w), int(h))

    @staticmethod
    def pixel_diff_ratio(pre: Image.Image, post: Image.Image,
                         pixel_threshold: int = 25) -> float:
        """Fraction of pixels whose grayscale value differs by more than
        ``pixel_threshold`` between pre and post. 0.0 = identical, 1.0 = all
        pixels changed. More robust than dHash for small UI animations.
        Used by R3 click verification."""
        from PIL import ImageChops
        if pre.size != post.size:
            return 1.0
        a = pre.convert("L")
        b = post.convert("L")
        diff = ImageChops.difference(a, b)
        # Count pixels above threshold (sum of 1s after binarisation / 255)
        bw = diff.point(lambda v: 1 if v > pixel_threshold else 0)
        total = bw.size[0] * bw.size[1]
        if total <= 0:
            return 0.0
        # Sum of pixel values = number of "changed" pixels
        try:
            import numpy as np  # type: ignore[import-not-found]
            arr = np.asarray(bw, dtype=np.uint8)
            changed = int(arr.sum())
        except Exception:
            # Fallback: histogram
            h = bw.histogram()
            changed = h[1] if len(h) > 1 else 0
        return changed / total

