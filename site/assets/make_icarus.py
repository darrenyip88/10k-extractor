#!/usr/bin/env python3
"""Cut the plates the site is built from, and let every edge break apart into
the same matrix of square pixels.

The point of the plate is that the seam is the effect. A hard cutout looks
pasted and a soft feather looks cheap, so every edge is dissolved instead: the
squares are densest exactly where the figure meets the ground and thin out in
both directions, into solid paint on one side and into nothing on the other.

    python3 make_icarus.py

Writes, all on transparency so the page's own near-black is always the ground:

    icarus.webp         the hero figure, its wing streaking into chroma
    planet.webp         one ringed planet, lifted out of the same frame
    plate-icarus.webp   background figure, the falling angel
    plate-fallen.webp   background figure, the winged figure reaching up
    favicon-icarus.png  the signature edge at 64px

Sources are the three plates Darren supplied: `src_flyer.jpg`,
`src_icarus_cover.jpg` and `src_fallen.jpg`. Titles, captions, barcodes and
stray data fragments are all cut — the page sets its own type, in two sizes.

`src_genius.jpg` is the Marinari painting the first build used and nothing
references any more. Kept in case the direction comes back.
"""

import numpy as np
from PIL import Image
from scipy import ndimage

GROUND = np.array([0x22, 0x22, 0x22], np.float32)
CREAM = np.array([0xEF, 0xD5, 0xC8], np.float32)
LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)

CELL = 6                    # square pixel of the dissolve matrix
SEED = 7                    # the scatter is authored, not re-rolled per build


def luma(a):
    return a @ LUMA


# --------------------------------------------------------------------------
# the matrix

def cell_mask(p, rng, cell=None):
    """Keep/drop decided once per CELL square, not once per pixel.

    One draw per cell is the whole trick — a per-pixel draw is just noise, and
    noise is what a feather looks like once it is dark enough.
    """
    c = cell or CELL
    h, w = p.shape
    gh, gw = -(-h // c), -(-w // c)
    draw = np.kron(rng.random((gh, gw)), np.ones((c, c)))[:h, :w]
    cy = np.clip(np.arange(gh) * c + c // 2, 0, h - 1)
    cx = np.clip(np.arange(gw) * c + c // 2, 0, w - 1)
    pc = np.kron(p[np.ix_(cy, cx)], np.ones((c, c)))[:h, :w]
    return draw < pc


def dissolve_edges(a, p, rng):
    """Break every frame edge described by `p` into the matrix.

    `p` is 0 at the edge and 1 where the plate should stay whole. Masking an
    edge in CSS instead only trades a straight cut for a soft feather, which is
    the one finish this plate rules out: the join has to look decided.
    """
    return a * np.where(p >= 1, 1.0, cell_mask(p, rng).astype(np.float32))


def save(rgb, alpha, path, quality=86):
    out = np.dstack([np.clip(rgb, 0, 255).astype(np.uint8),
                     (np.clip(alpha, 0, 1) * 255).astype(np.uint8)])
    Image.fromarray(out).save(path, quality=quality, method=6)
    print(f"  {path:22} {Image.open(path).size}")


# --------------------------------------------------------------------------
# the hero, and the planet that shares its frame

FLYER = "src_flyer.jpg"
# The plate sits on a cream wedge in the lower right. Fitted from the image
# rather than eyeballed: per column, the last run of cream from the bottom.
WEDGE_B, WEDGE_M = 1078.9, -0.2718


def flyer():
    im = Image.open(FLYER).convert("RGB")
    W, H = im.size
    a = np.asarray(im).astype(np.float32)
    lum = luma(a)

    yy = np.arange(H)[:, None]
    xx = np.arange(W)[None, :]
    wedge_d = (WEDGE_B + WEDGE_M * xx) - yy          # >0 above the cream wedge

    # This source is line work on near-black, so luminance *is* the matte. No
    # threshold on the paint, no flood fill: the ground simply falls away and
    # the figure's own shadows stay as honestly translucent as they were drawn.
    solid = ndimage.binary_closing((lum > 72) & (wedge_d > 0), np.ones((5, 5)))
    lab, n = ndimage.label(solid)
    sizes = ndimage.sum(solid, lab, range(1, n + 1))
    order = np.argsort(sizes)[::-1]

    # The largest run is the figure with its wing and the chroma streaking off
    # it. Everything else in this frame — 400-odd runs — is a caption, a
    # barcode or a die, and none of it survives.
    body = lab == order[0] + 1
    keep = ndimage.binary_dilation(body, np.ones((9, 9)))

    # No silhouette dissolve on this figure. It was withering it: a forearm is
    # about 30px across, so a 16px inward bite consumed the whole limb, and the
    # calves and feet came out as loose confetti rather than a dissolving edge.
    #
    # It was also the wrong edge to work on. The silhouette here is not a seam —
    # the artist drew this figure on near-black already, so the matte edge is
    # the artwork's own and needs no help. The only real join is the frame,
    # where the plate meets the page, and that is the one still dissolved below.
    alpha = np.clip((lum - 30) / 48, 0, 1) * keep

    # The left frame edge and the wedge's own diagonal, into the matrix.
    #
    # Narrow bands, and a curve that reaches whole almost at once. The figure's
    # outstretched hand really does run off the left edge and its calves really
    # do run into the wedge, so both cuts are real and both have to dissolve —
    # but at 300px and 190px the bands reached the elbow and the knee, and an
    # arm eaten to the elbow is not a dissolving edge, it is a withered arm.
    # 55px and 90px touch only the last stretch before each cut.
    p = np.minimum(np.clip(xx / 55.0, 0, 1) ** 0.45,
                   np.clip(wedge_d / 90.0, 0, 1) ** 0.45)
    alpha = dissolve_edges(alpha, np.broadcast_to(p, (H, W)).copy(),
                           np.random.default_rng(SEED))

    rows = np.where(alpha.max(1) > 0.02)[0]
    save(a[: rows.max() + 1], alpha[: rows.max() + 1], "icarus.webp")
    planet(a, lab == order[1] + 1)


def planet(a, run, out="planet.webp"):
    """The ringed body, cropped to itself.

    Its bounding box is mostly empty: the ring streaks a long way right, and a
    sliver of the figure's leg sits inside the same box. Boxed like that the
    sprite renders the disc at half the width CSS asks for and reads as a dot
    with a dash, so the crop comes off the *body* — the run eroded until the
    few-pixel-thick streak is gone — and the alpha is masked by the run itself
    so nothing from the rest of the frame can leak in.

    Padded square, so the page can size it on one axis and forget the ratio.
    """
    core = ndimage.binary_erosion(run, np.ones((7, 7)))
    clab, cn = ndimage.label(core)
    if cn:
        sizes = ndimage.sum(core, clab, range(1, cn + 1))
        core = clab == 1 + int(np.argmax(sizes))

    ys, xs = np.where(core)
    pad = 6
    y0, y1 = ys.min() - pad, ys.max() + pad + 1
    x0, x1 = xs.min() - pad, xs.max() + pad + 1
    side = max(y1 - y0, x1 - x0)
    cy, cx = (y0 + y1) // 2, (x0 + x1) // 2
    y0, x0 = cy - side // 2, cx - side // 2

    sl = (slice(max(0, y0), y0 + side), slice(max(0, x0), x0 + side))
    px = a[sl]
    mask = ndimage.binary_dilation(run, np.ones((5, 5)))[sl]
    save(px, np.clip((luma(px) - 30) / 46, 0, 1) * mask, out, quality=92)


# --------------------------------------------------------------------------
# the two background figures

def plate(src, box, out, width, warm_lo, warm_hi, lum_lo, lum_hi, floor=0.0):
    """Lift a figure out of a magazine plate for use behind the page.

    Cropped, not inpainted: each box is a window on the artwork that misses
    every line of the cover's own type, so nothing has to be painted out and no
    title survives to compete with the page's own.

    The matte is temperature, not value. On both plates the figure and its
    wings are warm and everything behind them — sky, cloud, the cold mottled
    ground — is not, so warmth carries the cut where a luminance threshold
    would take the clouds with it.
    """
    im = Image.open(src).convert("RGB").crop(box)
    h = int(width * im.size[1] / im.size[0])
    a = np.asarray(im.resize((width, h), Image.LANCZOS)).astype(np.float32)

    warm = np.clip((a[..., 0] - a[..., 2] - warm_lo) / (warm_hi - warm_lo), 0, 1)
    lit = np.clip((luma(a) - lum_lo) / (lum_hi - lum_lo), 0, 1)
    alpha = np.maximum(warm, floor) * lit

    # A tenth of the width, not a fifth, and a curve that reaches whole quickly.
    # The wide soft band was eating well into the wings before it finished, so
    # the figures read as withered rather than as figures with a dissolving
    # border. The matrix belongs at the frame, not over the artwork.
    xx = np.arange(width, dtype=np.float32)[None, :]
    yy = np.arange(h, dtype=np.float32)[:, None]
    band = width * 0.10
    px = np.minimum(np.clip(xx / band, 0, 1), np.clip((width - 1 - xx) / band, 0, 1))
    py = np.minimum(np.clip(yy / band, 0, 1), np.clip((h - 1 - yy) / band, 0, 1))
    p = np.minimum(px, py) ** 0.45
    alpha = dissolve_edges(alpha, np.ascontiguousarray(p),
                           np.random.default_rng(SEED + 3))
    save(a, alpha, out, quality=58)


# --------------------------------------------------------------------------

def favicon(path="favicon-icarus.png", n=64):
    """The signature edge at 64px: a cream block whose left side is a matrix.

    A figure is grey mush at this size, so the mark is the effect instead.
    """
    a = np.zeros((n, n), np.float32)
    a[5:n - 5, 6:n - 6] = 1.0
    p = np.clip((np.arange(n, dtype=np.float32) - 4) / 38, 0, 1) ** 1.05
    a = dissolve_edges(a, np.broadcast_to(p, (n, n)).copy(),
                       np.random.default_rng(SEED))

    out = np.zeros((n, n, 3), np.float32) + GROUND
    out = out + a[..., None] * (CREAM - out)
    Image.fromarray(out.astype(np.uint8)).save(path)
    print(f"  {path:22} {n}x{n}")


def main():
    flyer()
    # Boxes chosen to miss every line of type on each cover: the title crosses
    # the upper wings, and the two side labels sit hard against the edges.
    # 520px and quality 58: these render at 13% opacity behind the page, so a
    # 300 KB plate is 300 KB nobody can see. Compression noise is invisible at
    # that strength; the file size is not.
    plate("src_icarus_cover.jpg", (150, 225, 620, 800), "plate-icarus.webp",
          520, warm_lo=6, warm_hi=42, lum_lo=42, lum_hi=150, floor=0.22)
    plate("src_fallen.jpg", (95, 320, 585, 910), "plate-fallen.webp",
          520, warm_lo=10, warm_hi=46, lum_lo=40, lum_hi=165)
    favicon()


if __name__ == "__main__":
    main()
