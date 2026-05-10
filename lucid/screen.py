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
from typing import Any

import imagehash
import mss
from PIL import Image

from .config import ScreenshotConfig
from .window import active_window, cursor_pos


class ScreenLevel(str, Enum):
    L0 = "icon_atlas"
    L1 = "fullscreen"
    L2 = "active_window"
    L3 = "cursor_local"


class NoForegroundWindowError(RuntimeError):
    """Raised by L2 capture when there is no usable foreground window
    (e.g. lock screen, secure desktop). Caller decides whether to fall back
    to L1 or just surface a text-only error."""


@dataclass
class Capture:
    level: ScreenLevel
    image: Image.Image           # 已可能下采样
    raw_size: tuple[int, int]    # 原始（物理）宽高
    sent_size: tuple[int, int]   # 实际发给 LLM 的宽高
    # Origin of the captured rect in virtual-screen coords. ``None`` for L0
    # (icon_atlas) which is a synthetic image, not a screen region — the loop
    # treats ``offset is None`` as the signal "do NOT install this capture as
    # the active coordinate frame".
    offset: tuple[int, int] | None
    phash: str                   # 感知哈希，用于变化检测

    @property
    def scale_x(self) -> float:
        return self.raw_size[0] / self.sent_size[0] if self.sent_size[0] else 1.0

    @property
    def scale_y(self) -> float:
        return self.raw_size[1] / self.sent_size[1] if self.sent_size[1] else 1.0

    def model_to_screen(self, x: int, y: int) -> tuple[int, int]:
        """LLM 给出的截图内坐标 → 虚拟屏幕物理坐标。

        For synthetic images without a screen origin (e.g. L0 icon_atlas,
        ``offset is None``) this raises ``ValueError`` — those images do not
        define a clickable coordinate frame.
        """
        if self.offset is None:
            raise ValueError(
                f"capture level={self.level.value} has no screen offset; "
                "its coordinates are NOT mappable to the screen."
            )
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
        if level is ScreenLevel.L0:
            return self._capture_icon_atlas()
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
            # No usable foreground window (lock screen, secure desktop, …).
            # Per Docs/screenshot.md v2 §2.1 we no longer silently downgrade
            # to L1 here — callers (loop.py / tools.py) decide whether to
            # fall back or surface a text-only error.
            raise NoForegroundWindowError(
                "no usable foreground window for L2 capture"
            )
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
        # Snipaste-style smart sizing: ask UIA for the smallest UI element
        # under the cursor and crop to its bounding rect. If UIA can't give
        # us a usable rect (no element / element too big / too tiny), per
        # Docs/screenshot.md v2 §2.2 we **fall back to L2 (the whole active
        # window)**, not a fixed-radius square — a 200×200 patch of nothing
        # is useless to the model, while a full-window L2 is at least usable.
        rect = self._smart_l3_rect(cx, cy)
        if rect is None:
            try:
                return self._capture_active_window()
            except NoForegroundWindowError:
                # No foreground window either → last resort: full screen.
                return self._capture_full()
        left, top, right, bottom = rect
        region = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        img = self._grab(region)
        raw = img.size
        sent = _shrink(img, self.cfg.l3_max_long_edge)
        return Capture(
            level=ScreenLevel.L3, image=sent, raw_size=raw, sent_size=sent.size,
            offset=(region["left"], region["top"]), phash=_phash(sent),
        )

    def _capture_icon_atlas(self) -> Capture:
        """L0 — launcher icons collage. Synthetic image (not from mss); has no
        screen offset, so the loop will NOT install it as the active
        coordinate frame.
        """
        # Local import to avoid a launcher_icons ↔ screen import cycle.
        from . import launcher_icons as _li
        cfg_root = getattr(self.cfg, "_root", None) or getattr(self, "_cfg_root", None)
        # We don't have the full Config here; build_atlas needs it. Caller
        # (ComputerTool / loop.py) must inject it via ``set_root_config``.
        if cfg_root is None:
            raise RuntimeError(
                "icon_atlas capture requires the root Config; "
                "call ScreenSensor.set_root_config(cfg) once at startup."
            )
        atlas = _li.build_atlas(cfg_root)
        if atlas is None:
            raise RuntimeError(
                "icon atlas is empty (no apps scanned yet); "
                "the launcher_icons scheduler hasn't run."
            )
        try:
            img = Image.open(io.BytesIO(atlas.png_bytes)).convert("RGB")
        except Exception as e:
            raise RuntimeError(f"failed to decode atlas PNG: {e}")
        size = img.size
        return Capture(
            level=ScreenLevel.L0, image=img, raw_size=size, sent_size=size,
            offset=None, phash=_phash(img),
        )

    def set_root_config(self, cfg_root: Any) -> None:
        """Inject the root :class:`Config` so :meth:`_capture_icon_atlas` can
        call :func:`launcher_icons.build_atlas`. Called once at agent startup.
        """
        self._cfg_root = cfg_root

    def _smart_l3_rect(self, cx: int, cy: int) -> tuple[int, int, int, int] | None:
        """Snipaste-style smart L3 crop: ask UIA for the bounding rect of the
        smallest UI element under (cx, cy), then sanity-clamp.

        Returns ``(left, top, right, bottom)`` in virtual-screen coordinates,
        or ``None`` to signal "fallback to fixed radius".

        Strategy:
        - Element rect too big (covers >max_ratio of the screen)? Useless —
          probably hit a top-level container. Fallback.
        - Element rect tiny (e.g. a 1px border)? Auto-grow to (min_w, min_h)
          centred on the cursor.
        - Otherwise outset by ``smart_padding_px`` and clamp to the screen.
        """
        try:
            from . import uia as uia_mod
        except Exception:
            return None
        try:
            r = uia_mod.element_rect_at(cx, cy)
        except Exception:
            r = None
        if r is None:
            return None
        left, top, right, bottom = r
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            return None

        # Virtual-screen bounds for clamping below.
        try:
            with mss.mss() as sct:
                mon = sct.monitors[0]
                screen_w, screen_h = int(mon["width"]), int(mon["height"])
                screen_left, screen_top = int(mon["left"]), int(mon["top"])
        except Exception:
            screen_w, screen_h, screen_left, screen_top = 1920, 1080, 0, 0

        # Auto-grow tiny rects so the model has visual context.
        min_w = int(getattr(self.cfg, "l3_smart_min_w", 160))
        min_h = int(getattr(self.cfg, "l3_smart_min_h", 80))
        pad = int(getattr(self.cfg, "l3_smart_padding_px", 16))
        if w < min_w:
            grow = (min_w - w + 1) // 2
            left -= grow
            right = left + max(w + 2 * grow, min_w)
        if h < min_h:
            grow = (min_h - h + 1) // 2
            top -= grow
            bottom = top + max(h + 2 * grow, min_h)
        # Outset by padding.
        left -= pad
        top -= pad
        right += pad
        bottom += pad
        # Clamp to virtual-screen bounds.
        left = max(left, screen_left)
        top = max(top, screen_top)
        right = min(right, screen_left + screen_w)
        bottom = min(bottom, screen_top + screen_h)
        if right <= left or bottom <= top:
            return None
        return (left, top, right, bottom)

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

