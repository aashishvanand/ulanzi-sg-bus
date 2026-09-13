#!/usr/bin/env python3
"""Convert icon.jpeg (the Singapore Public Transport symbol) into pixel-art
icons for both clocks.

NOT committed to git, and neither are its outputs — this logo is a
trademarked/non-free government mark (see the source's own Wikimedia
Commons licensing), used here only for a personal display. See .gitignore.

Approach for 16x16: threshold the source image into foreground (the
yellow mark) vs. background, crop tightly to the mark's bounding box
(emblem-focus, same idea as convert_tiles.py in the sibling repo), fit it
into the square preserving aspect ratio (letterboxed — the source is
~1.86:1, not square), then hard-threshold back to two flat colors.

Approach for 8x8: hand-authored (see ICON_8_GRID) — at that resolution,
every algorithmic downscale of this specific mark (squish or letterbox)
reads as an unrecognizable scribble or collapses into a plain cross.

Outputs:
  icons/transport_icon_8.gif   - 8x8, for AWTRIX NG
  icons/transport_icon_16.gif  - 16x16, for TC002
"""
import os
from PIL import Image

SRC = os.path.join(os.path.dirname(__file__), "icon.jpeg")
OUT_DIR = os.path.join(os.path.dirname(__file__), "icons")
os.makedirs(OUT_DIR, exist_ok=True)

MARK_COLOR = (255, 209, 77)  # flattened yellow used for the mark itself
BG = (0, 0, 0)  # black, to blend with the clock panel's background

# At 8x8, an algorithmic downscale of this specific mark (wide, ~1.86:1,
# with fine curve detail) reads as an unrecognizable scribble no matter
# which fit strategy is used — squish distorts it into a blob, letterbox
# leaves too few rows to keep any curve. So 8x8 is hand-authored instead:
# a simple zigzag wave motif in the same spirit/color as the real mark,
# chosen over the algorithmic attempts for legibility at that resolution.
ICON_8_GRID = [
    "........",
    ".##..##.",
    "##..##..",
    "#..##..#",
    "..##..##",
    ".##..##.",
    "........",
    "........",
]


def load_mask():
    img = Image.open(SRC).convert("RGB")
    w, h = img.size
    bg_sample = img.getpixel((2, 2))  # corner pixel = background teal

    def is_foreground(px):
        dist = sum((a - b) ** 2 for a, b in zip(px, bg_sample)) ** 0.5
        return dist > 60

    mask = Image.new("L", (w, h), 0)
    px_in = img.load()
    px_out = mask.load()
    for y in range(h):
        for x in range(w):
            if is_foreground(px_in[x, y]):
                px_out[x, y] = 255
    return mask


def bbox_with_padding(mask, pad_frac=0.04):
    bbox = mask.getbbox()
    if not bbox:
        return (0, 0, mask.width, mask.height)
    x0, y0, x1, y1 = bbox
    pad_x = round((x1 - x0) * pad_frac)
    pad_y = round((y1 - y0) * pad_frac)
    return (
        max(0, x0 - pad_x),
        max(0, y0 - pad_y),
        min(mask.width, x1 + pad_x),
        min(mask.height, y1 + pad_y),
    )


def render(size, preserve_aspect=True):
    mask = load_mask()
    box = bbox_with_padding(mask)
    cropped = mask.crop(box)
    cw, ch = cropped.size

    # Flat two-color source image (crisp edges before downscale)
    flat = Image.new("RGB", cropped.size, BG)
    flat.paste(Image.new("RGB", cropped.size, MARK_COLOR), mask=cropped)

    # The source mark is ~1.86:1 (wide), not square — resizing straight to
    # size x size squishes it. At 16x16 that squish is mild and preserving
    # the aspect ratio (letterboxed) reads far more like the real mark. At
    # 8x8, though, letterboxing leaves only ~4px of vertical detail and
    # collapses the wave into a plain cross — worse than a mild squish,
    # since a cross reads as a different, unrelated symbol. So: letterbox
    # at 16x16, fill (squish) at 8x8.
    if preserve_aspect:
        scale = min(size / cw, size / ch)
        fit_w = max(1, round(cw * scale))
        fit_h = max(1, round(ch * scale))
        resized = flat.resize((fit_w, fit_h), Image.LANCZOS)
        canvas = Image.new("RGB", (size, size), BG)
        canvas.paste(resized, ((size - fit_w) // 2, (size - fit_h) // 2))
    else:
        canvas = flat.resize((size, size), Image.LANCZOS)

    # Hard-threshold back to exactly two colors: LANCZOS softens edges into
    # intermediate tones, and letting quantize() pick from those produces
    # muddy in-between colors instead of a crisp mark.
    binary = Image.new("RGB", (size, size), BG)
    px = canvas.load()
    out = binary.load()
    for y in range(size):
        for x in range(size):
            r, g, b = px[x, y]
            dist_mark = sum((v - m) ** 2 for v, m in zip((r, g, b), MARK_COLOR))
            dist_bg = sum((v - m) ** 2 for v, m in zip((r, g, b), BG))
            if dist_mark < dist_bg:
                out[x, y] = MARK_COLOR
    return binary.quantize(colors=2, dither=Image.Dither.NONE)


def render_hand_grid(grid):
    img = Image.new("RGB", (8, 8), BG)
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            if ch == "#":
                img.putpixel((x, y), MARK_COLOR)
    return img.quantize(colors=2, dither=Image.Dither.NONE)


if __name__ == "__main__":
    render_hand_grid(ICON_8_GRID).save(os.path.join(OUT_DIR, "transport_icon_8.gif"), format="GIF")
    render(16, preserve_aspect=True).save(os.path.join(OUT_DIR, "transport_icon_16.gif"), format="GIF")
    print("wrote icons/transport_icon_8.gif and icons/transport_icon_16.gif")
