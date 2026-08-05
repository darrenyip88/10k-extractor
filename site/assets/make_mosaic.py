#!/usr/bin/env python3
"""Rebuild a painting as a colour ASCII character mosaic.

Every cell keeps the colour of the painting underneath it and gets one
monospace glyph stamped into it, chosen by that cell's brightness. Dense
glyphs land in the sunlit passages, near-empty cells in the deep shadow.
The glyph is drawn in a contrasting tone derived from the cell's own colour,
so the source palette survives — no monochrome conversion, no green-on-black.

Digits and punctuation only. Letters would let the eye start reading words
instead of looking at the picture.

    python3 make_mosaic.py

Source: Jules Coignet, "Der Poseidontempel in Paestum", 1844. Public domain
(artist died 1860), via Wikimedia Commons.
"""

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

SRC = "src_canvas.jpg"
OUT = "bg-mosaic.png"

CELLS_ACROSS = 150      # the brief's grid
CELL = 8                # 150 cells across at 8px = 1200px; still a 6px cell
                        # once the browser scales it, at half the file weight
FONT_PATH = "/System/Library/Fonts/Menlo.ttc"

# This plate is the page's ground, with cream type sitting directly on it, so
# it is printed at a low key. Hue is untouched — every cell still carries the
# painting's own colour, just darker — which is what keeps the type legible
# without a scrim laid over the top.
KEY = 0.26

# The ceiling every pixel is held under, in WCAG relative luminance. Cream
# (#efe7d8, L=0.80) over a background at 0.095 is 5.9:1; the quieter body
# colour is 4.7:1. Measure this per PIXEL, not per cell: at the size the
# browser scales this plate to, one glyph stroke is several pixels wide, so a
# cell average hides exactly the bright spots that type has to sit on. That
# mistake read 5.3:1 on a ground that was really 2.6:1.
MAX_L = 0.095

# Ordered light -> heavy by how much ink each one puts in a cell.
# No letters: a readable word stops the eye.
RAMP = " ..,,:;--~++**//77??33556699440088%%##@@"


def pick_square(im):
    """Square crop centred on the temple, rocks held in the lower third."""
    w, h = im.size
    side = h
    # temple sits right of centre in this painting
    left = int(w * 0.31)
    left = max(0, min(left, w - side))
    return im.crop((left, 0, left + side, side))


def lift_shadows(im, gamma=0.62, sat=1.18):
    """The painting's foreground sits in deep shadow. Straight conversion buries
    the rocks and stone blocks in near-black cells, so pull the low end up
    before the grid is applied — otherwise a third of the frame renders empty."""
    lut = [min(255, int(((v / 255.0) ** gamma) * 255)) for v in range(256)]
    im = im.point(lut * 3)
    return ImageEnhance.Color(im).enhance(sat)


def lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lum(p):
    return 0.2126 * lin(p[0]) + 0.7152 * lin(p[1]) + 0.0722 * lin(p[2])


_capped = {}


def cap(rgb):
    """Pull a colour down until it clears MAX_L, keeping its hue.

    All three channels scale by the same factor, so the painting's colour
    survives — it just prints darker. Only the handful of bright glyph strokes
    ever hit this; the cells themselves are already under the ceiling."""
    if rgb in _capped:
        return _capped[rgb]
    out = rgb
    if lum(rgb) > MAX_L:
        lo, hi = 0.0, 1.0
        for _ in range(18):
            mid = (lo + hi) / 2
            if lum(tuple(v * mid for v in rgb)) > MAX_L:
                hi = mid
            else:
                lo = mid
        out = tuple(int(v * lo) for v in rgb)
    _capped[rgb] = out
    return out


def main():
    im = Image.open(SRC).convert("RGB")
    im = pick_square(im)
    im = lift_shadows(im)
    # Print the plate down to the page's key BEFORE the glyphs are chosen, so
    # each glyph's colour is derived from the darkened cell and the character
    # texture survives. Darkening the finished canvas instead flattens the
    # glyphs into the cell and the mosaic stops reading as characters at all.
    im = im.point([int(v * KEY) for v in range(256)] * 3)

    size = CELLS_ACROSS * CELL
    # one source pixel per cell gives us the cell's average colour
    small = im.resize((CELLS_ACROSS, CELLS_ACROSS), Image.LANCZOS)
    px = small.load()

    canvas = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(FONT_PATH, CELL)

    for cy in range(CELLS_ACROSS):
        for cx in range(CELLS_ACROSS):
            r, g, b = px[cx, cy]
            lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0

            x0, y0 = cx * CELL, cy * CELL
            # the cell keeps the painting's colour
            draw.rectangle([x0, y0, x0 + CELL, y0 + CELL], fill=cap((r, g, b)))

            # bright cell -> dense glyph, deep shadow -> near empty
            ch = RAMP[min(int(lum * len(RAMP)), len(RAMP) - 1)]
            if ch == " ":
                continue

            # darker character over sunlit marble, warm cream over the dark
            # fields. The lift is generous so the glyphs still read as
            # characters at this key; cap() then holds them under the ceiling.
            if lum > 0.5:
                gc = (int(r * 0.5), int(g * 0.5), int(b * 0.45))
            else:
                gc = (min(255, int(r * 1.7 + 30)),
                      min(255, int(g * 1.65 + 26)),
                      min(255, int(b * 1.45 + 18)))

            draw.text((x0, y0 - 1), ch, font=font, fill=cap(gc))

    canvas.save(OUT, optimize=True)
    print("wrote {} {}".format(OUT, canvas.size))
    report(canvas)


def report(im, inks=(("cream", (0xEF, 0xE7, 0xD8)), ("quiet", (0xD8, 0xCF, 0xBD)))):
    """Worst-case contrast for type sitting anywhere on this ground.

    Every pixel, antialiasing included — a glyph stroke is several pixels wide
    once the browser scales the plate up, so any bright pixel is a bright
    pixel some line of type can land on."""
    brightest = max(im.getdata(), key=lum)
    lb = lum(brightest)
    print("brightest pixel {}  L={:.4f}".format(brightest, lb))
    for name, ink in inks:
        print("  {:<6} on it: {:.2f}:1  (body text needs 4.5)".format(
            name, (lum(ink) + 0.05) / (lb + 0.05)))


if __name__ == "__main__":
    main()
