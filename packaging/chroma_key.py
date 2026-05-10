"""Remove a green-screen background and write a transparent PNG.

Usage:
    python chroma_key.py <input.png> <output.png>

Algorithm:
1. Convert RGB -> HSV; pixels whose hue is green-ish AND saturation is high
   AND value is high are flagged as background (alpha = 0).
2. Edge pixels get a soft alpha falloff to avoid harsh stair-stepping.
3. Spill suppression: for any pixel that still has noticeable alpha, clamp
   the green channel to <= max(red, blue) to remove green color bleed on
   the subject's edges.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image


def remove_green(src: Path, dst: Path) -> None:
    img = Image.open(src).convert("RGBA")
    arr = np.asarray(img, dtype=np.float32)  # H, W, 4
    r, g, b, _a = arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3]

    # "Greenness": how much green dominates the other channels.
    # Range roughly [-255, 255]; large positive => strong green.
    greenness = g - np.maximum(r, b)

    # Hard cut for obvious background, soft falloff for edges.
    # Tunables (in 0-255 channel units):
    hard_thr = 40.0   # above this -> definitely background
    soft_thr = 10.0   # below this -> definitely subject
    # Linear ramp between soft_thr and hard_thr.
    alpha = np.clip(1.0 - (greenness - soft_thr) / (hard_thr - soft_thr), 0.0, 1.0)

    # Spill suppression: wherever pixel is (partially) kept, clamp green so it
    # cannot exceed max(r, b). This kills the green rim left around hair/edges.
    keep_mask = alpha > 0.0
    g_clamped = np.where(keep_mask, np.minimum(g, np.maximum(r, b)), g)

    out = np.stack(
        [
            np.clip(r, 0, 255),
            np.clip(g_clamped, 0, 255),
            np.clip(b, 0, 255),
            np.clip(alpha * 255.0, 0, 255),
        ],
        axis=-1,
    ).astype(np.uint8)

    Image.fromarray(out, mode="RGBA").save(dst, format="PNG", optimize=True)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    if not src.is_file():
        print(f"Source not found: {src}", file=sys.stderr)
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    remove_green(src, dst)
    print(f"Wrote {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
