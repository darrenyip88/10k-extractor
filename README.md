# 10-K extractor

Give it a ticker, get back the 10-K and everything useful inside it: each Item as
its own file, the as-filed financial statements, ten years of financials, an index
of every risk factor, and a diff of this year's risks against last year's.

```bash
./run_10k.sh AAPL
```

See `examples/KO-2025-12-31/` for what comes out.

## Setup

Two things: Python deps, and one environment variable.

```bash
pip install requests pandas lxml beautifulsoup4
export SEC_USER_AGENT="Your Name you@example.com"
```

SEC EDGAR requires a User-Agent naming a real person and contact email — a
generic one gets a 403 on every request. Put the export in your shell profile so
it persists. The tool exits with instructions if it's missing rather than failing
halfway through an extraction.

That's the whole setup. No API key, no account, no paid tier.

Or in a browser — same extractor, ticker box instead of a terminal:

```bash
python3 serve.py
```

Then <http://localhost:4321>. See `site/README.md` for what the server exposes.

Or from the Dock. `./make_app.sh` builds **10K AI.app** into `/Applications`: it
starts the server if it isn't already up and opens the site. It's a launcher,
not a copy — it runs `serve.py` out of this directory, so edits here land in the
app immediately. Re-run `make_app.sh` if you move the project, because the
launcher holds absolute paths. Log: `~/Library/Logs/10K AI.log`. To stop the
server, `pkill -f serve.py`.

All of it comes from SEC EDGAR. **No API key, no quota, no cost.** This matters —
FMP's free tier is 250 calls/day and doesn't serve 10-K text at all, so nothing
here touches your FMP budget. A full extraction is ~12 SEC requests and about 15
seconds cold, instant once cached.

No LLM calls either. The tool extracts and structures; ask Claude to read the
output (see `SKILL.md`, which wires up `/10k <TICKER>`).

## Usage

```bash
./run_10k.sh AAPL                 # latest 10-K
./run_10k.sh BRK.B                # dots and dashes both work
./run_10k.sh AAPL --year 2019     # an older filing (history is capped at that year too)
./run_10k.sh TSLA --all-tables    # + every note table: segments, geography, debt, leases
./run_10k.sh JPM --refresh        # bypass the cache
./run_10k.sh SOMECO --cik 12345   # ticker SEC's map doesn't list
```

Output goes to `filings/<TICKER>/<fiscal-year-end>/`. Start with `SUMMARY.md`.

```
SUMMARY.md          key numbers, revenue/EPS history, risk-diff headline, file index
metadata.json       CIK, SIC industry, auditor + PCAOB ID, exchange, source URLs
sections/           item1_business, item1a_risk_factors, item7_mdna, item8_..., 10 in all
statements/         income_statement, balance_sheet, cash_flow, equity (.md + .json)
trends.md/.json     10 years of financials + margins, FCF, ROE, CAGR, share count
risk_headings.md    every risk factor headline — the Item 1A index
risk_diff.md        risks added, removed, and reworded vs last year
full_text.txt       the whole filing as plain text
10-K.htm            raw, exactly as filed
```

Re-running is a no-op unless you pass `--refresh`. Responses are cached to
`.cache/` forever, which is safe because filings are immutable once accepted.

## Tests

```bash
cd scripts && python3 -m pytest tests/ -q
```

Each module is also its own self-check — `python3 scripts/trends.py` runs the
assertions for that module. Only `sec_client.py` touches the network
(`pytest -m "not network"` to skip).

## Learned the hard way

Every one of these cost real debugging time. Read before changing anything.

**SEC 403s a generic User-Agent.** The header must contain a real contact email.
That's what `SEC_USER_AGENT` is for, and why the client refuses to start without
it — a 403 forty requests into an extraction is a much worse error than one at
launch.

**SEC throws occasional 503s.** One transient 503 silently swallowed Tesla's
balance sheet on an early run. `sec_client` now retries 429/5xx with backoff.

**Every "Item 1A. Risk Factors" string appears at least twice** — once in the
table of contents, once as the real heading. Naive extraction hands you the
24-character TOC entry instead of the 68,000-character section. Fix: for each
Item, keep the match with the most text before the next Item heading. TOC entries
sit next to each other so their spans are tiny.

**`filings.recent` is not a filing history.** It holds the most recent filings of
*any* type, so a heavy filer buries its own 10-Ks: JPMorgan's recent block has
25,613 filings and exactly one 10-K, the rest prospectuses. Older ones live in the
paginated `filings.files` archives, which `list_10ks` walks when it needs more.

**R#.htm files contain XBRL element-definition pop-ups** that `read_html` returns
as tables. "Take the biggest table" looks right and passes on the income
statement, then returns tagging metadata for Auditor Information, whose pop-up is
larger than the report itself. The statement is always rendered first — take
table 0.

**Don't use the XBRL `frame` field.** Duration facts get frames like `CY2024`, but
instant facts (Assets, equity, cash) get `CY2024Q3I`, keyed to *calendar*
quarters. A September-year-end filer therefore has no frame on its fiscal-year-end
balance sheet, and filtering on frames silently returns an empty series for every
balance sheet line. Instead: filter to 10-K rows, take durations of 340–400 days,
key by period end date, then pull instant facts at exactly those same dates. Works
on any fiscal calendar.

**Merge XBRL concepts, don't first-match them.** Apple tags revenue as
`RevenueFromContractWithCustomerExcludingAssessedTax` only from fiscal 2018 (ASC
606) and `SalesRevenueNet` before that. Taking the first concept with any data
left every pre-2018 year blank.

**Restatements mean the same period appears multiple times** in companyfacts. The
row with the latest `filed` date wins. A side effect: old years that no later
filing restated sit on the pre-split basis, so a stock split shows up as a step
change in the EPS row. That's real data, not a bug — `trends.md` says so.

**`difflib.SequenceMatcher` has an autojunk heuristic** that ignores any character
appearing in over 1% of a sequence longer than 200 elements. Risk headlines run
250+ characters, so comparing them as characters scored two near-identical Apple
risk factors at 0.41 instead of 0.95 and reported them as one added plus one
removed. Compare word lists instead — ~35 elements, so autojunk never fires.

**Match risks best-score-first, not in document order.** Taking each heading's
best match as you walk the list lets an early heading claim a prior risk that a
later one matches far better.

**No similarity threshold cleanly separates "new risk" from "same risk, rewritten
from scratch."** JPMorgan rewrote every headline in its 2025 10-K without changing
what any of them meant. So the tool doesn't pretend to decide: each "added" risk
is printed with its closest dropped counterpart and a similarity score, and a
wholesale rewrite gets flagged at the top of `risk_diff.md`.

**companyfacts can lag the filing it came from.** NVIDIA's FY2026 balance sheet
reports $51,951M of marketable securities, but companyfacts has no row for
`MarketableSecuritiesCurrent` at that date — its latest is the prior quarter. So
`trends.md` can have a hole that `statements/balance_sheet.md` fills. This is the
reason the tool pulls both sources instead of just XBRL, and why `SUMMARY.md`
spells out the components of net cash rather than printing one number.

**Missing metrics are reported, never substituted.** Banks have no gross profit or
capex line; Berkshire doesn't tag diluted EPS undimensioned. `trends.md` lists
which concepts fed each row and which were tried and not found.

**Paths in this project contain spaces.** Quote everything in shell.

**LaunchServices starts a script-based .app under Rosetta.** The bundle's
server then ran as x86_64 and couldn't `dlopen` numpy's arm64 `.so`, so every
extraction from the app died on an import error whose text is about numpy
source trees and never mentions architecture. Worse, `uname -m` inside that
translated process answers `x86_64`, so the launcher can't detect its own way
out — `make_app.sh` bakes the real arch in at build time and starts python
through `arch -arm64`. The same code from the terminal was always fine, which
is exactly what made it look like a Python install problem.

## Not built

Only 10-Ks — no 10-Qs, 8-Ks, or proxies. No Exhibit 21 subsidiary parsing. Note
tables are behind `--all-tables` rather than on by default (free, but ~70 extra
requests and a lot of files).

## Credits and licence

Code is MIT (see `LICENSE`). The things that aren't mine:

- **Filing content** comes from [SEC EDGAR](https://www.sec.gov/edgar). US
  government works and public company disclosures — not covered by this licence.
- **Fonts** in `site/assets/fonts/` are Archivo, Bodoni Moda, Libre Caslon
  Display, Libre Franklin, Public Sans and Source Serif, all under the SIL Open
  Font Licence 1.1. See `site/assets/fonts/README.md`.
- **`site/assets/src_canvas.jpg` / `src_temple.jpg`** — Jules Coignet, *Der
  Poseidontempel in Paestum* (1844), public domain via Wikimedia Commons. The
  ASCII mosaic in `bg-mosaic.png` is generated from it by `make_mosaic.py`.
- **`site/assets/hero.jpg`** — a 19th-century stipple engraving of the Acropolis.
  Provenance wasn't recorded when it was added. Public domain by age in all
  likelihood, but it's unverified — swap it if you're reusing this.

Not investment advice. The tool extracts and structures public filings; every
judgment about what the numbers mean is yours.
