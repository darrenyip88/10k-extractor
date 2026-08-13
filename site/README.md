# site — the Filed front end

The chosen direction is **`v4-icarus.html`**, and it runs the extractor for
real: type a ticker, get the filing back on the page.

```bash
cd "/Users/darren/Claude/Claude Code/10K AI" && python3 serve.py
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
| `GET /` | serves `v4-icarus.html` |
| `POST /api/extract` | `{"ticker":"AAPL"}` → runs `scripts/extract_10k.py`, returns the parsed filing as JSON |
| `GET /api/cached` | tickers already sitting in `filings/` |
| any 404 | a `#222222` error page, not stdlib's white one |
| `GET /filings/...` | the extracted files themselves, so the file list on the page is clickable |

`/api/extract` shells out to the same script `./run_10k.sh` calls, so the page
and the CLI can't drift apart. Cached tickers come back in about 0.3s; a cold
one is ~17 SEC requests plus one keyless price call, and about 5 seconds.

Two guards worth knowing about, since neither is obvious from the outside:

- **One extraction at a time.** SEC asks for ≤10 requests/second. A page
  refresh mid-run would otherwise start a second extractor writing into the
  same `filings/` directory. A second request while one is running gets a 429.
- **`/filings/` is path-checked and served as `text/plain`.** The raw `10-K.htm`
  is untrusted third-party HTML; handing it over as text means the browser
  never executes anything inside it. `..` traversal returns 403.

## The three sections worth reading

Under the metrics, the page opens Item 1 (Business), Item 1A (Risk Factors) and
Item 7 (MD&A) inline, each with a line on what it's for. They're `<details>`
drawers that fetch their file from `/filings/` on first open — Item 1A alone
runs to 70 KB and most visits never open it. No server change: the hrefs come
from the file list `/api/extract` already returns, so a filer missing an Item
simply gets no drawer.

**Item 8 used to be the fourth and isn't any more.** It's the statements and
their notes — which this page already renders as tables and links as files — so
the drawer was a second copy of them as 200 KB of prose, and it was the least
opened thing on the page. NVIDIA and JPMorgan made that worse by answering Item
8 with one sentence pointing at Part IV, so their drawer opened on 0.4 KB.

**They open condensed, not raw.** The drawer loads `summaries/<item>.md` — the
extractor's own condensed version, about a third the length, every sentence the
company's own — and a control at the top swaps to the full section and back.

Both views are set as prose: `<h4>` for headings, `<p>` at a 66ch measure. They
used to be a `<pre>` with `white-space: pre-wrap`, which meant a 56rem line of
10-K prose and no paragraph structure at all. The full section needs one more
step, because section text comes out of the filing one line per inline element —
a single sentence arrives split around every superscript ®. The page rejoins
them with the same rule the extractor uses: a line opening lowercase or with
punctuation belongs to the line above.

## Deep links

`/?t=NVDA` pulls that filing on load, so a result is a shareable link.

## The pick list is the cache

`/api/cached` was built and nothing called it. The hint line under the CTA
hardcoded four tickers as suggestions — a guess at what would be fast, printed
next to a claim that cached ones are instant. It now asks the route and prints
what's actually on disk, which is the honest version of the same sentence and
the page's only empty state. The four stay in the markup as the fallback, so
`python3 -m http.server` still shows something.

`BRK.B` lands on disk as `BRK-B` and the list prints the directory name. That
submits fine — `sec_client.normalize_ticker` takes either.

The pick buttons are wired by one delegated listener on `document`, not by
`querySelectorAll` at load. The list is replaced once the fetch answers, and
per-node listeners would all be attached to the discarded nodes.

## 404

Stdlib's default error page is black-on-white Courier. In a site that is
`#222222` edge to edge, that reads as the browser breaking rather than the path
being wrong. `serve.py` sets `error_message_format` — the handler's own hook, so
no new route — to a page on the same ground with the same two faces. Every
literal `%` in that string is doubled; it goes through `%`-formatting, and one
bare `100%` in the CSS raises instead of rendering.

## Honest blanks

A value the filer never tagged renders as `—`, never as a substituted
near-equivalent. This is visible on purpose: pull up `JPM` and gross margin,
operating margin and free cash flow are all blank, because a bank reports no
gross profit line, no operating income line and no capex. `BRK.B` has no diluted
EPS undimensioned. The list of what was tried and not found prints under the
metrics — twelve concepts for JPM.

---

# The design

The plate is **classical-glitch**: a filings tool borrowing its authority from a
classical figure that is coming apart. Mythic, atmospheric, decayed, luminous.
`v3`'s letterpress broadside is still on disk and is now the anti-reference —
nothing carried over from it except the two typefaces and the promise that a
blank stays blank.

## The ground

Flat `#222222`, edge to edge, on every page including `index.html`. Not `#000` —
pure black kills the artwork and the warm tones go muddy inside it. `#222222`
still reads as black on screen and gives them somewhere to sit.

Four inks, and only four:

| | | on `#222222` | carries |
|---|---|---|---|
| `--cream` | `#EFD5C8` | 11.4:1 | display type, values, the button |
| `--dim` | `#B79C8D` | 6.2:1 | body text |
| `--faint` | `#A98D7D` | 5.2:1 | tracked micro labels, placeholders |
| `--umber` | `#5F4F45` | 2.0:1 | rules and the one bar. **Never a word.** |

Umber is the only value under 4.5:1 and it is never allowed to carry text. If it
looks right on a label, the label wants `--faint`.

## Attribution — unfinished

The three source plates in `assets/` — `src_flyer.jpg`, `src_icarus_cover.jpg`
and `src_fallen.jpg` — were **supplied, not sourced**. They look like someone
else's work (the ICARUS cover carries a "Noiiir" signature) and their licence is
unknown, so the footer prints no art credit at all rather than a guessed one.

**Settle this before the site is public.** Either establish the rights and print
the credit, or swap in artwork whose licence is known. `src_genius.jpg` — Onorio
Marinari's *Winged genius*, public domain via Wikimedia Commons — is still in
`assets/` and was the first build's hero for exactly that reason.

## The figure

`assets/icarus.webp`, cut from `src_flyer.jpg` by `assets/make_icarus.py`. It
bleeds off three edges and is the only thing on the page allowed to.

**The join is the effect** — but only at the joins. The figure itself is left
whole; what dissolves is every edge where the plate is *cut*: the left frame,
where the outstretched hand runs off, and the diagonal along the bottom. A hard
cutout looks pasted and a soft feather looks cheap; a dot matrix looks decided.

Four things that each cost a pass:

- **Luminance *is* the matte.** This source is line work on near-black, so
  there is no threshold to tune and no flood fill to fight: alpha ramps
  straight off luminance, the ground falls away, and the figure's own shadows
  stay as honestly translucent as they were drawn.
- **No dissolve on the silhouette.** There was one, and it withered the figure.
  A forearm is about 30px across, so a 16px inward bite consumed the whole
  limb and the calves and feet came out as loose confetti. It was also the
  wrong edge to work: the artist drew this figure on near-black already, so the
  silhouette is not a seam and needs no help. The frame is the only real join.
- **The frame bands are narrow — 55px and 90px, not 300px and 190px.** The wide
  versions reached the elbow and the knee. An arm eaten to the elbow is not a
  dissolving edge, it is a withered arm. Same lesson on the background plates,
  where the band went from a fifth of the width to a tenth.
- **The cream wedge is fitted, not eyeballed.** The source sits on a cream
  triangle in the lower right. `WEDGE_B/WEDGE_M` come from a least-squares fit
  over the last run of cream in each column, and that diagonal is the second
  edge dissolved.

Everything else in that frame — around 400 connected runs of captions, barcodes
and a pair of dice — is dropped. The page sets its own type, in two sizes.

The scatter is seeded (`SEED = 7`), so a rebuild is stable and the composition
doesn't re-roll under you.

```bash
python3 assets/make_icarus.py    # all five assets, ~510 KB total
```

## The cosmos

One `.cosmos` layer, absolute inside `body`, as tall as the whole document.

- **Eleven planets**, all cut from the hero's own frame (`planet.webp` — the
  ringed body was the second-largest run in that image). Cropped to the body
  and padded square: the run's own bounding box is mostly empty, because the
  ring streaks a long way right and a sliver of the figure's leg sits in the
  same box, so a sprite boxed that way rendered the disc at half the width CSS
  asked for and read as a dot with a dash. The crop now comes off the body,
  eroded until the few-pixel streak is gone, and the alpha is masked by the run
  itself so nothing else leaks in. Sizes 10–48px, drifting on 22–47s cycles.
- **Three ghosted plates** from the other two covers, cropped to windows that
  miss every line of the covers' own type, so nothing had to be painted out.
- **Six glitch streaks** on `steps(1)`.

Three things worth knowing:

- **Absolute, not fixed.** Fixed planets sit still while the page moves under
  them and read as dirt on the screen. These belong to the document.
- **No `overflow:hidden` on the layer.** It's several thousand pixels tall once
  a filing renders, and clipping it makes one paint area that size for every
  drifting planet to invalidate. Overflow-x on the root already clips the
  plates that hang off the sides. Every animated child gets `will-change:
  transform` so it composites instead of repainting its parent.
- **The plates' opacity ceiling is measured.** Flat 7% is the most either plate
  can carry directly behind `--dim` body text without dropping it under 4.5:1.
  They run at 13% with a mask that dies before the reading column, which is
  what makes the higher number legal — where a plate can reach text it's back
  near 3%. The mask is stepped in six hard stops, not smooth: a gradient
  falloff is the soft feather this whole page refuses. Below `52rem` there is
  no outer margin left to hide in, so the whole layer drops to 55%.

Planets are kept past ~18% and before ~78% of the width on wide screens. One
crossing a hairline rule stops reading as a distant body and starts reading as
dirt.

**That rule was written for the wrong column.** 18–78% is the centred 56rem
reading column, but the hero copy isn't in it — the headline, the deck and the
CTA are flush left at `--pad`, which is exactly the outer margin the planets
were sent to. Five of the eleven sit under 12%, and on a laptop one of them
drifts straight through the ticker field: a sphere inside the only control on
the site. `.cta` is now the one opaque box on the page (`background:var(--void)`
— same colour as the ground, so it shows as nothing except the field staying
empty). The planets themselves were left alone; over 4.5rem cream serif they
read fine.

`index.html` had the same bug and needed the opposite fix. That page is flush
left with a 62rem column and *no* left margin at all, so two planets at 4% and
6% were sitting on the "Four · Icarus" and "Two · Editorial grid" headings.
All four moved to the right margin, where the ghosted plate already lives.

## The motif at hairline scale

`.rule` is the same move: a 1px rule with a band of 5px squares above it,
thinning at both ends. Two `mask-image` layers — a horizontal ramp over the
dotted band, a solid strip over the hairline — so only the dots dissolve and the
rule stays full width.

It's a `<div>`, not an `<hr>`. Generated content on `<hr>` silently fails to
paint in this engine despite computing a non-none value; `v3` hit the same trap.

The favicon is the edge again at 64px: a cream block whose left side breaks into
the matrix. A figure is grey mush at that size, so the mark is the effect.

## Motion

- **One authored moment:** a scan sweeps down the figure once on load and is
  then gone for good. A real `mask-position` animation, not an overlay fade.
- **Quiet and permanent:** the planets drift; the streaks jump on `steps(1)`.
  Stepped on purpose — a glitch doesn't ease, and an eased streak reads as a
  loading bar.
- `prefers-reduced-motion` kills all of it.

Every control now answers a press with `translateY(1px)` — the submit button,
the ticker picks, the condensed/full swap. A hairline rectangle with no fill has
nothing to squash, so a push is the only physical move it has.

## Keyboard

A skip link is the first focusable thing on the page, styled as the same
hairline rectangle as the CTA and off-canvas until it's focused. It points at
`#ticker`, not at the form — an input is focusable, so the native anchor jump
puts the cursor in the field.

The smooth-scroll handler is scoped to `.top nav a[href^="#"]` rather than every
in-page anchor. It calls `preventDefault`, which on the skip link would scroll
the field into view without ever focusing it — the one thing a skip link is for.

The result section carries `aria-label`, not `aria-live`. It fills with a whole
filing — eight metrics, three tables, the risk diff — and a polite live region
announces all of it. The status line above already says the run finished.

## The CTA

One small hairline rectangle, no fill, no second button. The ticker field lives
*inside* it, so the whole page has exactly one control. Hover inverts the submit
half to cream — the single inversion in the world.

No rounded corners anywhere, no shadows, no gradient on any ground, no cards.

## Narrow screens

Below `52rem` the hero figure stops being a layer behind the copy and becomes a
real grid row above it. Overlapping display type onto the artwork fails contrast
at any opacity, and dimming the artwork to rescue the text ruins the one thing
the page is built around. It still bleeds off the top and the right, and its own
bottom edge is a pixel matrix, so the join is still the effect.

## Type

Two faces from `assets/fonts`, self-hosted from Google Fonts. No network calls
at runtime.

- **Libre Caslon Display** — everything that speaks: headline, values, entry
  titles, section prose.
- **Libre Franklin** — only at 10px, 600, tracked `.2em`, uppercase.

Nothing in between those two. System monospace appears for file paths, KB
figures, the second counter and the shell command — data and measurement, not a
costume.

The other four woff2 files in `assets/fonts` belong to `v1` and `v2`.

## The other three directions

`index.html` lists all four. `v1-neoclassical.html`, `v2-editorial.html` and
`v3-letterpress.html` are the design only — `serve.py`'s `HOME` now points at
`v4`, so their ticker boxes have no route behind them. Kept for reference.

`v3` owns `assets/hero.jpg` (the Acropolis stipple engraving) and
`assets/bg-mosaic.png` (Paestum as a colour ASCII mosaic, `make_mosaic.py`, with
`src_canvas.jpg` and `src_temple.jpg` as inputs). All still on disk, all still
used by `v3`, none referenced by `v4`.

## The app icon

`assets/make_icon.py` draws `app.icns` and `../make_app.sh` installs it. It's
the flyer's head — haloed, in profile — on the same `#222222` ground as the
page, its bottom cut bleeding off the tile and dissolving into the same
square-pixel matrix that cuts every plate.

It crops `icarus.webp`, not `src_flyer.jpg`, so it inherits the matte and the
chroma bleed `make_icarus.py` already pulled from the plate. Re-deriving those
here would be a second copy of that work, free to drift.

Three settings are load-bearing, all of them arrived at by rendering a contact
sheet and looking at it:

- `HEAD_W = 0.84`. Wider and the head bleeds off both side edges and reads as
  one pale mass; the closed silhouette is the last cue still working at 32px.
- `HEAD_TOP = 1 - HEAD_W`, so the crop's hard bottom edge lands exactly on the
  tile's and genuinely bleeds. Higher and that cut sits *inside* the tile as a
  flat pale rectangle.
- The matrix is dropped below 128px, where a cell is worth less than a pixel
  and prints as a chewed edge rather than an effect.

Pushing the levels harder at small sizes was tried and reverted — PIL's
contrast pivots on the mean, so it blew the jaw and neck into one white wedge.

**16px is where this mark gives out.** A photographic head has more information
than 256 pixels hold, so in the menu bar and list views it's a warm blob. The
Dock draws it far larger, which is what it's for. `--check` asserts the crop
hasn't drifted off the head and that the 16px tile still has tonal range — it
does *not* claim the face reads there. `iconutil` exiting 0 proves nothing
either way; render the sheet and look.

`assets/favicon.png` is the leftover ink-on-paper mark. Nothing produces it any
more — `make_icon.py` used to, and doesn't — but `v3-letterpress.html:8` still
links it, so it stays on disk. `v4`, `index.html` and the server's error page
all use `assets/favicon-icarus.png`, from `make_icarus.py`.
