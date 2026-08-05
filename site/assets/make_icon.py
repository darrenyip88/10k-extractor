#!/usr/bin/env python3
"""Draw the app icon: a Doric temple front, printed in ink on paper.

Drawn rather than cropped from the engraving. The engraving is stipple — at
32px every dot collapses into grey mush and the architecture stops resolving,
which is the trap the hero brief warned about. Flat shapes hold their edges all
the way down to 16px, and flat is what the rest of the page is.

The double rule under the temple is the site's one ornament, so the icon and
the page read as the same object. Below 48px the rule is dropped: three
hairlines 40px apart land inside one pixel and print as grey smear.

    python3 make_icon.py            # writes app.icns

Needs iconutil (ships with macOS).
"""

import os
import shutil
import subprocess

from PIL import Image, ImageDraw

INK = (0x14, 0x14, 0x14, 0xFF)
PAPER = (0xFF, 0xFF, 0xFF, 0xFF)
EDGE = (0x14, 0x14, 0x14, 0x38)     # the printed edge of the sheet, not a shadow
OUT = "app.icns"
ICONSET = "app.iconset"

SIZES = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
         (256, 1), (256, 2), (512, 1), (512, 2)]

B = 2048            # drawn here, downsampled to every size: cheap antialiasing
COLUMNS = 6
RULE_ABOVE = 48     # px at which the double rule survives the downsample

# macOS draws app icons on a rounded tile inset from the canvas, not full
# bleed. A white square would sit in the Dock as a white *square*, which is
# the one thing that would mark it as homemade.
TILE_INSET = 100 / 1024.0
TILE_SPAN = 824 / 1024.0


def draw_plate(size, rules=True):
    im = Image.new("RGBA", (B, B), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    x0, x1 = TILE_INSET * B, (1 - TILE_INSET) * B
    d.rounded_rectangle([x0, x0, x1, x1], radius=0.225 * (x1 - x0),
                        fill=PAPER, outline=EDGE, width=max(2, int(B / 512)))

    # everything below is laid out in 1024ths of the tile, not of the canvas
    def t(v):
        return (TILE_INSET + v / 1024.0 * TILE_SPAN) * B

    left, right = t(148), t(876)
    width = right - left
    mid = B / 2

    # --- stylobate: three steps, each wider than the one above -------------
    step_h = t(38) - t(0)
    base_y = t(806)
    for i in range(3):
        grow = (t(34) - t(0)) * i
        d.rectangle([left - grow, base_y - step_h * (i + 1),
                     right + grow, base_y - step_h * i], fill=INK)

    # --- columns -----------------------------------------------------------
    col_top = t(462)
    col_bot = base_y - step_h * 3
    # Doric intercolumniation is roughly 1.2 column diameters of daylight
    # between shafts. Tighter than that and the colonnade prints as one black
    # mass instead of columns. Solve for a width that fits COLUMNS of them.
    inset = t(38) - t(0)
    span = width - 2 * inset
    col_w = span / (COLUMNS + (COLUMNS - 1) * 1.2)
    gap = col_w * 1.2
    cap_h = t(20) - t(0)
    flare = t(9) - t(0)
    for i in range(COLUMNS):
        x = left + inset + i * (col_w + gap)
        d.rectangle([x, col_top, x + col_w, col_bot], fill=INK)
        # capital: the flare at the head of a Doric column
        d.rectangle([x - flare, col_top - cap_h, x + col_w + flare, col_top],
                    fill=INK)

    # --- architrave --------------------------------------------------------
    arch_bot = col_top - cap_h
    arch_top = arch_bot - (t(60) - t(0))
    d.rectangle([left, arch_top, right, arch_bot], fill=INK)

    # --- pediment: solid, at the shallow Greek pitch, eaves overhanging -----
    # A steeper roof reads as a house. Doric sits near 16 degrees, and the
    # paper gap under it is what keeps the two masses apart at 32px.
    ped_bot = arch_top - (t(24) - t(0))
    over = t(24) - t(0)
    apex = ped_bot - 0.30 * (width / 2 + over)
    d.polygon([(left - over, ped_bot), (right + over, ped_bot), (mid, apex)],
              fill=INK)

    # --- the double rule ---------------------------------------------------
    if rules:
        y = t(882)
        d.rectangle([left, y, right, y + (t(15) - t(0))], fill=INK)
        d.rectangle([left, y + (t(42) - t(0)), right, y + (t(50) - t(0))],
                    fill=INK)

    return im.resize((size, size), Image.LANCZOS)


def main():
    if os.path.isdir(ICONSET):
        shutil.rmtree(ICONSET)
    os.mkdir(ICONSET)

    for px, scale in SIZES:
        n = px * scale
        name = "icon_{0}x{0}{1}.png".format(px, "@2x" if scale == 2 else "")
        draw_plate(n, rules=n >= RULE_ABOVE).save(os.path.join(ICONSET, name))

    subprocess.run(["iconutil", "-c", "icns", ICONSET, "-o", OUT], check=True)
    shutil.rmtree(ICONSET)
    print("wrote", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
