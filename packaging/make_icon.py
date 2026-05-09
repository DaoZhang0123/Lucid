"""Generate OtterScope app icon source (1024x1024 PNG).

A white paw print on a teal rounded-square background. Pipe the result through
`pnpm tauri icon icon-source.png` to regenerate all platform sizes + .ico/.icns.
"""
from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
BG = (38, 138, 168, 255)        # teal — "scope" / water
FG = (255, 255, 255, 255)       # white paw

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Rounded-square background
d.rounded_rectangle([0, 0, SIZE, SIZE], radius=200, fill=BG)

# Paw geometry (centered, slightly low)
cx, cy = SIZE // 2, SIZE // 2 + 50

# Main pad (pear-shaped: ellipse)
pad_w, pad_h = 520, 460
d.ellipse(
    [cx - pad_w // 2, cy - pad_h // 2 + 40,
     cx + pad_w // 2, cy + pad_h // 2 + 40],
    fill=FG,
)

# Four toe pads
toe_w, toe_h = 170, 230
toes = [
    (-300, -300),  # outer left
    (-110, -410),  # inner left
    ( 110, -410),  # inner right
    ( 300, -300),  # outer right
]
for dx, dy in toes:
    d.ellipse(
        [cx + dx - toe_w // 2, cy + dy - toe_h // 2,
         cx + dx + toe_w // 2, cy + dy + toe_h // 2],
        fill=FG,
    )

out = "icon-source.png"
img.save(out)
print(f"wrote {out} ({SIZE}x{SIZE})")
