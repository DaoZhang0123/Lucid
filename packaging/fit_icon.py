"""Crop a transparent PNG to its content bbox and re-pad to a centered
square canvas with a small fixed margin.

Usage: python fit_icon.py <input.png> <output.png> [margin_pct]

margin_pct defaults to 0.06 (6% padding around content).
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image


def fit(src: Path, dst: Path, margin_pct: float = 0.06, target: int = 1024) -> None:
    img = Image.open(src).convert("RGBA")
    bbox = img.getbbox()
    if bbox is None:
        raise RuntimeError("source image is fully transparent")
    content = img.crop(bbox)
    cw, ch = content.size
    side = max(cw, ch)
    canvas_inner = side  # content fits in this square
    # Add margin: final canvas = canvas_inner / (1 - 2*margin_pct)
    final = int(round(canvas_inner / (1.0 - 2.0 * margin_pct)))
    canvas = Image.new("RGBA", (final, final), (0, 0, 0, 0))
    ox = (final - cw) // 2
    oy = (final - ch) // 2
    canvas.paste(content, (ox, oy), content)
    # Resample to target square
    canvas = canvas.resize((target, target), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dst, format="PNG", optimize=True)
    print(f"Wrote {dst} ({target}x{target}, content was {cw}x{ch})")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    margin = float(sys.argv[3]) if len(sys.argv) > 3 else 0.06
    fit(src, dst, margin_pct=margin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
