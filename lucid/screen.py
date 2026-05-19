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
from PIL import Image, ImageDraw, ImageFont

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
    # Per-capture grid colour→coordinate legend (multi-line CJK text), built
    # by ``_draw_grid``. Callers (loop.py, tools.py) append this to the
    # human-readable text part of the message that carries the image, so the
    # model knows which line colour means which screen coordinate. ``None``
    # for synthetic images (L0 icon_atlas) that have no grid.
    grid_legend: str | None = None

    @property
    def scale_x(self) -> float:
        return self.raw_size[0] / self.sent_size[0] if self.sent_size[0] else 1.0

    @property
    def scale_y(self) -> float:
        return self.raw_size[1] / self.sent_size[1] if self.sent_size[1] else 1.0

    def model_to_screen(self, x: int, y: int) -> tuple[int, int]:
        """Map a model-supplied ``coordinate`` (image-local raw pixels, 0-based
        from the top-left of this capture) to virtual-screen pixels.

        Since v2.3 the on-image gridlines in :func:`screen._draw_grid` are
        labelled with **image-local** pixel positions (0..raw_w, 0..raw_h)
        instead of absolute screen coordinates. The model reads those labels
        and sends ``coordinate`` in the same image-local frame; the framework
        adds this capture's screen offset here. This removes the dual-frame
        confusion that bit thread 20260517-185939 (model read "1400" off a
        label inside an 880-px-wide L2 image and tried to place the click in
        a non-existent image column, ending up in the WeChat side panel).

        For L1 the offset is ``(0, 0)`` so image-local == screen and behaviour
        is identical to the pre-v2.3 prompt.

        For synthetic images without a screen origin (e.g. L0 icon_atlas,
        ``offset is None``) this raises ``ValueError`` — those images do not
        define a clickable coordinate frame.
        """
        if self.offset is None:
            raise ValueError(
                f"capture level={self.level.value} has no screen offset; "
                "its coordinates are NOT mappable to the screen."
            )
        ox, oy = self.offset
        return int(x) + ox, int(y) + oy

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


# ---------------------------------------------------------------------------
# Gridline overlay (colour-coded, NO on-image text)
# ---------------------------------------------------------------------------
# Drawn on L1/L2/L3 captures so the model can ground screen coordinates
# without UIA. Each 100-px gridline gets a distinct colour from a fixed
# palette; the colour→coordinate mapping is emitted as a text legend that
# the caller (loop.py / tools.py) appends to the image's user-message text
# part. This avoids putting digits on the image — which were getting badly
# mauled by ViT patch tokenizers and JPEG quantization — and instead lets
# the model use full attention over the legend text + image-side colour
# identification, two tasks LLMs do well.
#
# X (vertical) lines and Y (horizontal) lines share the same palette but
# the legend tags axis explicitly. Lines are 2 px wide with a 1-px dark
# halo so they remain visible over any background colour.
_GRID_SPACING_PX = 100

# 12-colour palette of highly-distinct hues. (English name, RGB.) English
# names so they survive JPEG / Unicode encoding everywhere and match the
# model's colour vocabulary directly. The palette wraps if a capture is
# wider than 12 * 100 px (only L1 ever does) — the legend disambiguates by
# listing every coordinate.
#
# Order is chosen so **adjacent gridlines** are maximally hue-distant
# (≥80° apart on the colour wheel for every consecutive pair). A naive
# rainbow order (red→orange→yellow→…) puts three warm colours back-to-back
# at the leftmost lines and the model confuses them at JPEG/ViT scale,
# which caused the WeChat 20260517-160917 emoji miss where the model
# picked x=1370 (near "red" x=1400) when the emoji actually sat on the
# 4th vertical line ("lime" x=1700). Alternating hues stops that class of
# adjacent-line confusion. Hue values (HSL) shown in comments.
_GRID_PALETTE: list[tuple[str, tuple[int, int, int]]] = [
    ("red",     (255,  40,  40)),   # 0°
    ("blue",    ( 40,  80, 255)),   # 220°
    ("yellow",  (240, 220,   0)),   # 60°
    ("purple",  (160,  60, 220)),   # 280°
    ("lime",    ( 80, 220,  40)),   # 95°
    ("magenta", (255,  60, 200)),   # 310°
    ("orange",  (255, 140,   0)),   # 30°
    ("cyan",    ( 60, 200, 255)),   # 180°
    ("pink",    (255, 150, 180)),   # 340°
    ("teal",    (  0, 180, 180)),   # 170°
    ("brown",   (170, 100,  40)),   # 20°
    ("green",   (  0, 150,  60)),   # 140°
]
# Thin (1-px) semi-transparent gridlines so the background stays readable.
# We deliberately drop the dark halo and the 2-px width that the previous
# revision used — on dense UIs (e.g. WeChat chat list) the thick + haloed
# stripes were obscuring small icons and text. At ~50% alpha a 1-px line
# is still clearly identifiable as colour-X but lets the underlying pixels
# show through.
_GRID_LINE_ALPHA = 110


def _palette_entry(idx: int) -> tuple[str, tuple[int, int, int, int]]:
    """Return (name, RGBA) for the idx-th colour, wrapping around the palette."""
    name, rgb = _GRID_PALETTE[idx % len(_GRID_PALETTE)]
    return name, (rgb[0], rgb[1], rgb[2], _GRID_LINE_ALPHA)


def palette_legend(raw_w: int, raw_h: int,
                   spacing: int = _GRID_SPACING_PX) -> str:
    """Return a one-line legend mapping each labelled gridline's colour to its
    image-local coordinate, for the current ``_draw_grid`` layout.

    Used by ``loop.py`` to inject a colour↔number cheat-sheet next to a
    pre-click preview so the model can cross-check colour identification
    against the labels on the macro frame — vision models routinely
    misname adjacent gridline colours (red/blue/yellow off-by-one), and
    the numbers are the ground truth.
    """
    if raw_w <= 0 or raw_h <= 0:
        return ""
    x_parts: list[str] = []
    for idx, ix in enumerate(range(spacing, raw_w, spacing)):
        name, _ = _GRID_PALETTE[idx % len(_GRID_PALETTE)]
        x_parts.append(f"{name}={ix}")
    y_parts: list[str] = []
    for idx, iy in enumerate(range(spacing, raw_h, spacing)):
        name, _ = _GRID_PALETTE[idx % len(_GRID_PALETTE)]
        y_parts.append(f"{name}={iy}")
    bits: list[str] = []
    if x_parts:
        bits.append("verticals " + ", ".join(x_parts))
    if y_parts:
        bits.append("horizontals " + ", ".join(y_parts))
    return "; ".join(bits)


def _draw_grid(img: Image.Image, raw_size: tuple[int, int],
               offset: tuple[int, int],
               spacing: int = _GRID_SPACING_PX) -> tuple[Image.Image, str | None]:
    """Overlay colour-coded image-local gridlines with on-image labels.

    Each 100-px gridline gets a distinct palette colour and the **image-local**
    pixel coordinate (0-based from the top-left of the raw capture) is drawn
    **on the image itself**, at both endpoints of the line, in the same colour
    as the line (with a thin black stroke for legibility on any background).

    Image-local labels (since v2.3) avoid the dual-frame confusion bug where
    an L2 of a 880-px-wide WeChat window had labels like "1400, 1500, 1600"
    (screen coords) that didn't match any actual image-pixel column. Now the
    same L2 reads "100, 200, ..., 800" — numbers that directly correspond to
    visible pixel positions in the image. The framework adds this capture's
    screen ``offset`` automatically in :meth:`Capture.model_to_screen` when the
    click fires, so the model never needs to know the window's screen origin.

    Label placement:
      * Vertical line (image x = ix): two `ix` labels, each just to the
        RIGHT of the line — one near the top edge, one near the bottom.
      * Horizontal line (image y = iy): two `iy` labels, each just BELOW
        the line — one near the left edge, one near the right.
    Gridline crossings inside the image are NOT labelled (avoids clutter
    and keeps the underlying UI visible).

    The ``offset`` argument is retained for API compatibility (callers pass it
    in case we later want a hybrid mode) but is no longer used to derive label
    values — they are pure image-local raw pixels.

    Returns ``(image, None)`` — the second tuple element is retained for
    API compatibility with callers expecting an optional legend string.
    """
    sent_w, sent_h = img.size
    raw_w, raw_h = raw_size
    if sent_w <= 0 or sent_h <= 0 or raw_w <= 0 or raw_h <= 0:
        return img, None
    _ = offset  # currently unused under v2.3 image-local labels
    scale_x = sent_w / raw_w
    scale_y = sent_h / raw_h
    base = img.convert("RGBA") if img.mode != "RGBA" else img.copy()
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    # Image-local 100-px grid lines, distinct colour per line. Start at
    # ``spacing`` (skip x=0 / y=0 to keep the labels off the image edge).
    x_vals = list(range(spacing, raw_w, spacing))
    y_vals = list(range(spacing, raw_h, spacing))
    # Lazy-load a small font; we want it readable but not space-hungry.
    from .launcher_icons import _try_load_font as _load_font
    font = _load_font(13)
    drew_anything = False
    # Pre-compute pixel positions so we can skip lines that fall off-image.
    x_pixels: list[tuple[int, int, str, tuple[int, int, int, int]]] = []
    for idx, ix_raw in enumerate(x_vals):
        ix = int(round(ix_raw * scale_x))
        if 0 <= ix < sent_w:
            _, rgba = _palette_entry(idx)
            x_pixels.append((ix, ix_raw, str(ix_raw), rgba))
    y_pixels: list[tuple[int, int, str, tuple[int, int, int, int]]] = []
    for idx, iy_raw in enumerate(y_vals):
        iy = int(round(iy_raw * scale_y))
        if 0 <= iy < sent_h:
            _, rgba = _palette_entry(idx)
            y_pixels.append((iy, iy_raw, str(iy_raw), rgba))
    # Draw the lines first (1-px, semi-transparent).
    for ix, _sx, _label, rgba in x_pixels:
        draw.line([(ix, 0), (ix, sent_h - 1)], fill=rgba, width=1)
        drew_anything = True
    for iy, _sy, _label, rgba in y_pixels:
        draw.line([(0, iy), (sent_w - 1, iy)], fill=rgba, width=1)
        drew_anything = True
    # Now draw the on-image coordinate labels in solid colour (full alpha)
    # with a thin black outline so they remain readable against any UI bg.
    # Labels live on the overlay too, so they composite cleanly.
    margin = 3
    for ix, _sx, label, rgba in x_pixels:
        solid = (rgba[0], rgba[1], rgba[2], 255)
        bbox = draw.textbbox((0, 0), label, font=font, stroke_width=1)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = min(ix + margin, sent_w - tw - 1)
        # Top endpoint label.
        draw.text((tx, margin), label, font=font, fill=solid,
                  stroke_width=1, stroke_fill=(0, 0, 0, 255))
        # Bottom endpoint label.
        draw.text((tx, sent_h - th - margin - 2), label, font=font,
                  fill=solid, stroke_width=1, stroke_fill=(0, 0, 0, 255))
    for iy, _sy, label, rgba in y_pixels:
        solid = (rgba[0], rgba[1], rgba[2], 255)
        bbox = draw.textbbox((0, 0), label, font=font, stroke_width=1)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        ty = min(iy + margin, sent_h - th - 1)
        # Left endpoint label.
        draw.text((margin, ty), label, font=font, fill=solid,
                  stroke_width=1, stroke_fill=(0, 0, 0, 255))
        # Right endpoint label.
        draw.text((sent_w - tw - margin - 2, ty), label, font=font,
                  fill=solid, stroke_width=1, stroke_fill=(0, 0, 0, 255))
    out = Image.alpha_composite(base, overlay).convert("RGB")
    if not drew_anything:
        return out, None
    return out, None


# ---------------------------------------------------------------------------
# Crosshair overlay for L3 tiles (NO gridlines)
# ---------------------------------------------------------------------------
# L3's purpose is to confirm **what element sits under the planned click**,
# NOT to read coordinates (use L1/L2 for that — they carry the colour grid).
# At the 200×200 scale, the 100-px gridlines obscure the small target icon
# and trick the model into thinking the click is mis-aimed even when the
# coordinate is correct.
#
# Initial design: lime crosshair with 28-px arms + 6-px centre gap + small
# ring. Problem (WeChat emoji-panel thread 20260517-170209): each emoji in
# the panel is ~30 px wide, so 28-px crosshair arms reach RIGHT into the
# neighbouring cells and visually mangle the emojis next to the target.
# When the target itself is a hand-gesture emoji (👍/👎/🤝/✌️/🙏…) the
# model also needs the NEIGHBOURS visible to disambiguate — covering them
# with green lines defeats the purpose of an L3 zoom.
#
# Current design: tiny centre crosshair (8-px arms, 5-px gap → only 3-px
# strokes near centre) + a small open ring + four short tick marks at the
# tile edges pointing inward to mark the centre row/column. The icon under
# the click is barely touched, but the centreline is still findable via the
# edge ticks.
_CROSSHAIR_COLOUR = (60, 255, 60, 150)   # lime, semi-transparent so it doesn't obscure the icon underneath
_CROSSHAIR_HALO = (0, 0, 0, 110)         # 1-px dark halo for contrast (also semi-transparent)
_CROSSHAIR_REACH_PX = 8                   # short arms — don't cover neighbours
_CROSSHAIR_GAP_PX = 5                     # leave centre pixel visible
_CROSSHAIR_RING_R = 4                     # small open ring at centre
_CROSSHAIR_EDGE_TICK_PX = 12              # length of each edge tick mark
_CROSSHAIR_EDGE_MARGIN_PX = 1             # gap between tick and tile edge


def _draw_crosshair(img: Image.Image, raw_size: tuple[int, int],
                    offset: tuple[int, int],
                    target_xy: tuple[int, int]) -> Image.Image:
    """Overlay a centred crosshair at the screen-coord ``target_xy``.

    The crosshair is positioned at the target's location within the captured
    tile (after rescaling for ``_shrink``). No gridlines, no legend — the
    only purpose is "this is the pixel the click will hit; confirm the
    element beneath it is the right one".
    """
    sent_w, sent_h = img.size
    raw_w, raw_h = raw_size
    if sent_w <= 0 or sent_h <= 0 or raw_w <= 0 or raw_h <= 0:
        return img
    sx0, sy0 = offset
    tx, ty = int(target_xy[0]), int(target_xy[1])
    scale_x = sent_w / raw_w
    scale_y = sent_h / raw_h
    cx = int(round((tx - sx0) * scale_x))
    cy = int(round((ty - sy0) * scale_y))
    # Clamp the centre to the tile bounds — should normally be exactly the
    # tile centre, but defensive in case the target landed at the edge.
    cx = max(0, min(sent_w - 1, cx))
    cy = max(0, min(sent_h - 1, cy))
    base = img.convert("RGBA") if img.mode != "RGBA" else img.copy()
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    reach = _CROSSHAIR_REACH_PX
    gap = _CROSSHAIR_GAP_PX
    # Helper to stroke a line with a 1-px dark halo on both sides.
    def _stroke(x0: int, y0: int, x1: int, y1: int) -> None:
        if y0 == y1:  # horizontal
            draw.line([(x0, y0 - 1), (x1, y1 - 1)], fill=_CROSSHAIR_HALO, width=1)
            draw.line([(x0, y0 + 1), (x1, y1 + 1)], fill=_CROSSHAIR_HALO, width=1)
        else:          # vertical
            draw.line([(x0 - 1, y0), (x1 - 1, y1)], fill=_CROSSHAIR_HALO, width=1)
            draw.line([(x0 + 1, y0), (x1 + 1, y1)], fill=_CROSSHAIR_HALO, width=1)
        draw.line([(x0, y0), (x1, y1)], fill=_CROSSHAIR_COLOUR, width=1)
    # Four short centre crosshair arms.
    for (x0, y0), (x1, y1) in [
        ((cx - reach, cy), (cx - gap, cy)),
        ((cx + gap, cy), (cx + reach, cy)),
        ((cx, cy - reach), (cx, cy - gap)),
        ((cx, cy + gap), (cx, cy + reach)),
    ]:
        _stroke(x0, y0, x1, y1)
    # Small open ring at the centre.
    r = _CROSSHAIR_RING_R
    draw.ellipse([(cx - r - 1, cy - r - 1), (cx + r + 1, cy + r + 1)],
                 outline=_CROSSHAIR_HALO, width=1)
    draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)],
                 outline=_CROSSHAIR_COLOUR, width=1)
    # Four edge tick marks — short stripes at each tile edge aligned with
    # the centre row/column. Lets the model locate the centreline without
    # covering the icon under the cursor with long crosshair arms.
    tick = _CROSSHAIR_EDGE_TICK_PX
    m = _CROSSHAIR_EDGE_MARGIN_PX
    # Top + bottom: vertical ticks aligned with cx.
    _stroke(cx, m, cx, m + tick)
    _stroke(cx, sent_h - 1 - m - tick, cx, sent_h - 1 - m)
    # Left + right: horizontal ticks aligned with cy.
    _stroke(m, cy, m + tick, cy)
    _stroke(sent_w - 1 - m - tick, cy, sent_w - 1 - m, cy)
    return Image.alpha_composite(base, overlay).convert("RGB")


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
        # L1 = overview only; intentionally NO gridlines. On a 2560×1440
        # virtual desktop the 100-px grid would paint ~25 vertical + ~14
        # horizontal labelled lines, cluttering the panoramic view that L1
        # is meant for. Precise clicking happens via L2 / L3 which keep
        # the colour-coded grid.
        return Capture(
            level=ScreenLevel.L1, image=sent, raw_size=raw, sent_size=sent.size,
            offset=(region["left"], region["top"]), phash=_phash(sent),
            grid_legend=None,
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
        sent, legend = _draw_grid(sent, raw, (win.left, win.top))
        return Capture(
            level=ScreenLevel.L2, image=sent, raw_size=raw, sent_size=sent.size,
            offset=(win.left, win.top), phash=_phash(sent),
            grid_legend=legend,
        )

    def _capture_cursor_local(self) -> Capture:
        # L3 = a fixed-size tile (`l3_tile_size_px`, default 200×200) centred
        # on the cursor. A single lime crosshair marks the cursor pixel so
        # the model can confirm WHICH element is under it. Coordinate reading
        # is reserved for L1/L2 (which carry the colour-coded gridlines);
        # the L3 grid was removed because at 200 px scale the gridlines
        # obscured the target icon and made correct clicks look mis-aimed.
        cx, cy = cursor_pos()
        return self._capture_tile(cx, cy)

    def _capture_tile(self, sx: int, sy: int) -> Capture:
        """Grab a fixed-size square tile centred on (sx, sy) in screen coords.

        Tile size comes from ``cfg.l3_tile_size_px`` (default 200). The tile
        is clamped to the virtual desktop bounds. A single thin lime
        crosshair (with a 6-px gap so the centre pixel stays visible) is
        drawn on the target — L3's job is "is the right element under the
        cursor?", not coordinate reading. Use L1/L2's colour gridlines when
        you need to read coords.
        """
        size = max(32, int(getattr(self.cfg, "l3_tile_size_px", 200)))
        half = size // 2
        with mss.mss() as sct:
            mon = sct.monitors[0]
            s_left, s_top = int(mon["left"]), int(mon["top"])
            s_w, s_h = int(mon["width"]), int(mon["height"])
        # First propose the centred rect, then slide it back into the screen
        # if either edge falls off — so the tile keeps full ``size`` whenever
        # the screen is large enough.
        left = sx - half
        top = sy - half
        right = left + size
        bottom = top + size
        if left < s_left:
            left, right = s_left, s_left + size
        if top < s_top:
            top, bottom = s_top, s_top + size
        if right > s_left + s_w:
            right = s_left + s_w
            left = max(s_left, right - size)
        if bottom > s_top + s_h:
            bottom = s_top + s_h
            top = max(s_top, bottom - size)
        region = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        img = self._grab(region)
        raw = img.size
        sent = _shrink(img, self.cfg.l3_max_long_edge)
        sent = _draw_crosshair(sent, raw, (left, top), (sx, sy))
        return Capture(
            level=ScreenLevel.L3, image=sent, raw_size=raw, sent_size=sent.size,
            offset=(left, top), phash=_phash(sent),
            grid_legend=None,
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

    def capture_around(self, sx: int, sy: int, radius: int = 0) -> Capture:
        """Grab a fixed-size L3 tile around an arbitrary screen coordinate.

        The ``radius`` argument is kept for backwards compatibility but is
        ignored — L3 size is now controlled exclusively by
        ``cfg.l3_tile_size_px`` (default 200, matching the model's expected
        click-target context window). Used by the pre-click preview path in
        ``loop._maybe_preview_click``.
        """
        return self._capture_tile(int(sx), int(sy))

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
        sent, legend = _draw_grid(sent, raw, (int(left), int(top)))
        return Capture(
            level=ScreenLevel.L2, image=sent, raw_size=raw, sent_size=sent.size,
            offset=(int(left), int(top)), phash=_phash(sent),
            grid_legend=legend,
        )

    # ---------- 变化检测 ----------
    @staticmethod
    def similarity(a: str, b: str) -> float:
        """两张 dHash 相似度，1.0 = 完全相同。"""
        ha = imagehash.hex_to_hash(a)
        hb = imagehash.hex_to_hash(b)
        bits = ha.hash.size
        return 1.0 - (ha - hb) / bits

