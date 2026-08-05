# site — the 10K AI front end

The chosen direction is **`v3-letterpress.html`**, and it now runs the extractor
for real: type a ticker, get the filing back on the page.

```bash
python3 serve.py
```

Then open <http://localhost:4321>. `serve.py` serves the site *and* the one
dynamic route, so this is the only command you need.

`python3 -m http.server` still works for looking at the design, but the ticker
box will report that it can't reach the extractor — there's no API behind a
plain static server.

## What the server does

Stdlib only. No Flask, no new dependencies.

| Route | Does |
|---|---|
| `GET /` | serves `v3-letterpress.html` |
| `POST /api/extract` | `{"ticker":"AAPL"}` → runs `scripts/extract_10k.py`, returns the parsed filing as JSON |
| `GET /api/cached` | tickers already sitting in `filings/` |
| `GET /filings/...` | the extracted files themselves, so the file list on the page is clickable |

`/api/extract` shells out to the same script `./run_10k.sh` calls, so the page
and the CLI can't drift apart. Cached tickers come back in about 0.3s; a cold
one is ~13 SEC requests and about 5 seconds.

Two guards worth knowing about, since neither is obvious from the outside:

- **One extraction at a time.** SEC asks for ≤10 requests/second. A page
  refresh mid-run would otherwise start a second extractor writing into the
  same `filings/` directory. A second request while one is running gets a 429.
- **`/filings/` is path-checked and served as `text/plain`.** The raw `10-K.htm`
  is untrusted third-party HTML; handing it over as text means the browser
  never executes anything inside it. `..` traversal returns 403.

## The four sections worth reading

Under the metrics, the page opens Item 1 (Business), Item 1A (Risk Factors),
Item 7 (MD&A) and Item 8 (Financial Statements & Notes) inline, each with a
line on what it's for. They're `<details>` drawers that fetch their section
file from `/filings/` on first open — Item 8 alone runs to 230 KB for KO and
most visits never open it. No server change: the hrefs come from the file list
`/api/extract` already returns, so a filer missing an Item simply gets no
drawer.

NVIDIA and JPMorgan answer Item 8 with one sentence pointing at Part IV, so
their section file is ~0.4 KB. Anything under 2 KB prints a note saying the
tables are in `statements/` instead.

## Deep links

`/?t=NVDA` pulls that filing on load, so a result is a shareable link.

## Honest blanks

A value the filer never tagged renders as `—`, never as a substituted
near-equivalent. This is visible on purpose: pull up `JPM` and gross margin,
operating margin, free cash flow and total debt are all blank, because a bank
doesn't report a gross profit line. `BRK.B` has no diluted EPS undimensioned.
The list of what was tried and not found prints under the metrics.

## The hero plate

`assets/hero.jpg` — a 19th-century stipple engraving of the Acropolis. Provenance was not recorded when the file was added; it is almost certainly public domain by age, but if you fork this and care, verify or swap it.

The bottom third is blurred rather than covered with a dark scrim, so the image
stays an image. Three copies of the engraving sit over the original, each with
a heavier `blur()` and a `mask-image` gradient starting lower down, so the blur
deepens toward the bottom.

Blur radii are deliberately modest (4 / 11 / 24px). An earlier pass ran
9 / 26 / 54 and the architecture dissolved into fog — the darkening, not the
blur, is what buys the text its contrast.

Each blurred copy is `transform: scale()`d slightly. `filter: blur()` fades an
element's own edges to transparent, and the scale pushes those soft edges
outside the clip so no sharp strip shows at the bottom of the plate.

## The ground

`assets/bg-mosaic.png` — the background of the whole page below the landing.
The landing (plate, headline, ticker box) is the one sheet of white; everything
past it sits straight on the mosaic, separated by a 1px printed edge rather
than a drop shadow, so the brief's flat world stays flat.

It's Jules Coignet's *Der Poseidontempel in Paestum* (1844, public domain via
Wikimedia Commons) rebuilt as a colour ASCII character mosaic: 150 cells
across, each cell keeping the painting's own colour with one monospace glyph
stamped into it, chosen by that cell's brightness. Digits and punctuation only
— letters would let the eye start reading words instead of looking at the
picture.

Regenerate with `python3 assets/make_mosaic.py`. `assets/src_canvas.jpg` is its
input; `assets/src_temple.jpg` is the untrimmed gallery photo and can be
deleted if you want the ~750KB back.

It's `background-size: cover` on the ground block itself — one continuous image
scaled to the whole lower page, never tiled, so there's no seam to find. Not a
fixed backdrop: `position: fixed` behind a page this tall means the browser
scales a 1200px plate across ~3000px of document and the glyphs turn to mush,
and it janks on iOS Safari at this size besides.

**Measure the plate's brightness per pixel, not per cell.** `make_mosaic.py`
caps every pixel at `MAX_L = 0.095` relative luminance, which is what puts cream
`#efe7d8` at 5.9:1 and the quieter `#d8cfbd` at 4.7:1 on the worst pixel in the
image. An earlier version measured the 150×150 cell average instead and reported
5.3:1 on a ground that was really **2.6:1** — at the size the browser draws this,
one glyph stroke is several pixels wide, and a cell average smooths away exactly
the bright spots type has to sit on.

## The app icon

`assets/app.icns`, drawn by `assets/make_icon.py` and installed by
`../make_app.sh`. A Doric temple front in ink on paper with the site's double
rule under it — flat shapes, not a crop of the engraving, because stipple at
32px is grey mush. The rule is dropped below 48px, where three hairlines land
inside one pixel. Rounded tile rather than full bleed: macOS doesn't mask icons
the way iOS does, so a white square would sit in the Dock as a white square.

## Type

Six variable woff2 files in `assets/fonts`, ~130KB total, self-hosted from
Google Fonts. No network calls at runtime.

`v3` sets Libre Caslon Display against Libre Franklin, which carries only the
tracked uppercase labels.

## The other two directions

`v1-neoclassical.html` and `v2-editorial.html` are the directions that weren't
chosen. They're the design only — no ticker box, no extractor behind them.
Kept for reference; nothing else points at them.
