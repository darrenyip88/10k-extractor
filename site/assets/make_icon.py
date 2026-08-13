#!/usr/bin/env python3
"""Draw the app icon: the flyer's head, cut out of the hero and set on the
site's #222222 ground.

Cropped from `icarus.webp` rather than `src_flyer.jpg`, so the icon inherits
the treatment the hero already carries — the chroma bleed, the line work, the
matte that `make_icarus.py` pulled from the plate's own luminance. Re-deriving
it here would be a second, drifting copy of that work.

The head bleeds off the bottom of the tile the way the figure bleeds off the
page, and the last stretch before the cut dissolves into the same matrix of
square pixels as every other edge on the site. What bleeds is the neck, not
the jaw — see `CROP`.

The engraving is the risk here: stipple and fine line work collapse into grey
mush the moment the icon is small, which is why the icon this replaced was a
drawn letterform. Three things carry it as far as it goes, all of them measured
on a contact sheet rather than assumed:

- The crop is *tight*. A head fills the tile; a whole figure would be confetti
  by 32px. `CROP` is the head, its halo, and the neck the cut needs.
- The head stops short of the side edges. Bled to full width it reads as one
  pale mass — the closed silhouette is the last cue still working at 32px.
- The face is lifted off the ground with a levels curve, and the matte's
  translucent tail is cut, because at icon size it prints as haze.

**Where it gives out: 16px.** At the size the menu bar and list views use, the
profile is a warm blob and no amount of levels work brings it back — a
photographic head has more information than 256 pixels can hold. Verified, not
assumed; `--check` asserts only that the tile still has range there, not that
the face reads. The Dock, which is what this icon is for, draws it far larger.

    python3 make_icon.py            # writes app.icns
    python3 make_icon.py --check    # the head survives the crop and 16px

Needs iconutil (ships with macOS). The browser tab is *not* this mark —
`favicon-icarus.png` comes from `make_icarus.py` and is left alone.
"""

import os
import random
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageEnhance

SRC = "icarus.webp"
GROUND = (0x22, 0x22, 0x22, 0xFF)
HAIR = (0xEF, 0xD5, 0xC8, 0x26)      # --hair: the tile's own edge, not a shadow
OUT = "app.icns"
ICONSET = "app.iconset"

SIZES = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
         (256, 1), (256, 2), (512, 1), (512, 2)]

B = 1024             # drawn here, downsampled to every size. The source is
                     # 928px wide, so there is nothing above this to sample.
MATRIX_ABOVE = 128   # px at which a matrix cell is still worth more than a pixel
CELL = B // 64       # the dissolve's square pixel
SEED = 7             # same scatter on every rebuild, so the icon doesn't churn

# The head in `icarus.webp`: halo, hair, face, and enough neck below the chin
# to have something to cut. Square, so the crop can't stretch.
#
# The neck is the point. Cropped to the jaw the dissolve had nowhere to land
# but the face, and the bottom band ate the chin — the one edge of a profile
# that has to survive for it to read as a profile at all.
CROP = (290, 94, 522, 326)
# Width is the crop's, not the head's: the head sits about 94% across it, so at
# 0.99 the hair still stops ~7% of the tile short of both edges and the
# silhouette closes. That closure is the one cue still working at 32px — bleed
# the *head* to full width and it prints as a single pale mass.
HEAD_W = 0.99        # how much of the tile's width the crop spans
HEAD_TOP = 0.04      # set independently: leaves the halo air without dropping
                     # the face, and the surplus bleeds off the bottom
FADE = 0.08          # the bottom band that dissolves, in tile heights

# macOS draws app icons on a rounded tile inset from the canvas, not full
# bleed. A #222222 square would sit in the Dock as a dark *square*, which is
# the one thing that would mark it as homemade.
TILE_INSET = 100 / 1024.0
TILE_SPAN = 824 / 1024.0


def head(matrix=True):
    """The cut-out head, sized and placed on a transparent canvas."""
    im = Image.open(SRC).convert("RGBA").crop(CROP)
    span = int(TILE_SPAN * B)
    n = int(span * HEAD_W)
    im = im.resize((n, n), Image.LANCZOS)

    # Lift the face off the ground. The plate is drawn to be read at 900px
    # against near-black; shrunk it goes flat and the profile stops resolving,
    # so contrast and brightness both come up.
    #
    # Pushing these harder at the small sizes was tried and reverted: PIL's
    # contrast pivots on the mean, so the extra push blew the jaw and neck into
    # one white wedge and cost more legibility at 16px than it bought.
    rgb = ImageEnhance.Brightness(ImageEnhance.Contrast(
        im.convert("RGB")).enhance(1.22)).enhance(1.14)
    a = im.getchannel("A")
    # The matte's long translucent tail is the plate's own shadow. At icon size
    # it reads as haze around the head, so anything under a quarter is dropped
    # and the rest is pushed toward solid.
    a = a.point(lambda v: 0 if v < 64 else min(255, int((v - 64) * 1.9)))

    canvas = Image.new("RGBA", (B, B), (0, 0, 0, 0))
    mask = Image.new("L", (B, B), 0)
    x = (B - n) // 2
    y = int(TILE_INSET * B + HEAD_TOP * span)
    canvas.paste(rgb, (x, y))
    mask.paste(a, (x, y))

    if matrix:
        dissolve(mask)
    canvas.putalpha(mask)
    return canvas


def dissolve(mask):
    """Break the bottom cut into the plates' matrix of square pixels.

    One draw per cell, not per pixel — the same trick as `make_icarus.py`, and
    the whole reason the edge reads as decided rather than feathered. A
    per-pixel draw is noise, and noise is what a soft feather looks like once
    it is this dark.
    """
    rng = random.Random(SEED)
    d = ImageDraw.Draw(mask)
    bottom = TILE_INSET * B + TILE_SPAN * B
    start = bottom - FADE * TILE_SPAN * B

    for y in range(int(start), int(bottom) + CELL, CELL):
        # 1 where the plate stays whole, 0 at the cut
        p = max(0.0, min(1.0, (bottom - y) / (bottom - start))) ** 0.45
        for x in range(0, B, CELL):
            if rng.random() >= p:
                d.rectangle([x, y, x + CELL, y + CELL], fill=0)


def draw_tile(size, matrix=True):
    im = Image.new("RGBA", (B, B), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    x0, x1 = TILE_INSET * B, (1 - TILE_INSET) * B
    d.rounded_rectangle([x0, x0, x1, x1], radius=0.225 * (x1 - x0),
                        fill=GROUND, outline=HAIR, width=max(1, B // 512))

    # Clipped to the tile, so the head can bleed off the bottom without
    # squaring off the rounded corners or spilling onto the desktop.
    tile = Image.new("L", (B, B), 0)
    ImageDraw.Draw(tile).rounded_rectangle([x0, x0, x1, x1],
                                           radius=0.225 * (x1 - x0), fill=255)
    figure = head(matrix)
    figure.putalpha(Image.composite(figure.getchannel("A"),
                                    Image.new("L", (B, B), 0), tile))
    im.alpha_composite(figure)
    return im.resize((size, size), Image.LANCZOS)


def check():
    """The two ways this file goes wrong, both silent: the crop drifts off the
    head, or the engraving goes to grey mush once the icon is small."""
    full = head(matrix=False)
    ink = sum(1 for v in full.getchannel("A").getdata() if v > 128)
    assert ink > 0.16 * B * B, "the head is mostly gone — check CROP and HEAD_W"

    small = draw_tile(16)
    px = [p for p in small.getdata() if p[3] > 0x80]
    lum = sorted(0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b, _ in px)
    # Ground to face. Mush is what it looks like when this collapses.
    assert lum[-3] - lum[2] > 80, "only {:.0f} levels of range at 16px".format(
        lum[-3] - lum[2])
    print("checks pass")


def main():
    if os.path.isdir(ICONSET):
        shutil.rmtree(ICONSET)
    os.mkdir(ICONSET)

    for px, scale in SIZES:
        n = px * scale
        name = "icon_{0}x{0}{1}.png".format(px, "@2x" if scale == 2 else "")
        draw_tile(n, matrix=n >= MATRIX_ABOVE).save(os.path.join(ICONSET, name))

    subprocess.run(["iconutil", "-c", "icns", ICONSET, "-o", OUT], check=True)
    shutil.rmtree(ICONSET)
    print("wrote", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    import sys
    (check if "--check" in sys.argv else main)()
