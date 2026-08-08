# 10-K extractor

Give it a ticker, get back the 10-K and everything useful inside it: each Item as
its own file plus a condensed version of it, the as-filed financial statements,
ten years of financials, an index of every risk factor, a diff of this year's
risks against last year's, and the valuation — real share count, market cap, the
EV bridge and the multiples.

**Every number comes out of a 10-K.** Including the share price: there is no
quote feed anywhere in this tool, and nothing it prints moves after the filing
was accepted. See "The price, from the filing" below for what that costs.

```bash
./run_10k.sh AAPL
```

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

All of it comes from SEC EDGAR. **No API key, no quota, no cost, and no live
market data.** This matters — FMP's free tier is 250 calls/day and doesn't serve
10-K text at all, so nothing here touches your FMP budget. A full extraction is
~17 SEC requests and about 15 seconds cold, instant once cached. `--no-price`
skips the price and everything downstream of it.

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
./run_10k.sh AAPL --price 250     # your own price instead of the cover-page one
./run_10k.sh BRK.B --shares 2.16e9  # your own share count — needed for multi-class filers
./run_10k.sh COST --no-price      # no price, so no market cap, EV or multiples
```

Output goes to `filings/<TICKER>/<fiscal-year-end>/`. Start with `SUMMARY.md`.

```
SUMMARY.md          key numbers, valuation, revenue/EPS history, risk-diff headline, file index
metadata.json       CIK, SIC industry, auditor + PCAOB ID, exchange, source URLs
sections/           item1_business, item1a_risk_factors, item7_mdna, item8_..., 10 in all
summaries/          the same Items condensed to about a third, every sentence the filer's own
statements/         income_statement, balance_sheet, cash_flow, equity (.md + .json)
trends.md/.json     10 years of financials + margins, FCF, ROE, CAGR, share count
valuation.md/.json  share count three ways, price, market cap, EV bridge, EBIT/EBITDA, multiples
risk_headings.md    every risk factor headline — the Item 1A index
risk_diff.md        risks added, removed, and reworded vs last year
full_text.txt       the whole filing as plain text
10-K.htm            raw, exactly as filed
```

Re-running is a no-op unless you pass `--refresh`. Responses are cached to
`.cache/` forever, which is safe because filings are immutable once accepted.

## The price, from the filing

A 10-K states no closing price anywhere. The quarterly high/low table stopped
being required in 2018, and the performance graph is indexed to $100. What every
cover page does state, and tags in XBRL, is two numbers: the **aggregate market
value of common equity held by non-affiliates**, measured on the last business
day of the second fiscal quarter, and the **share count**, taken a few weeks
after year end. Their quotient is the price this tool uses.

It's a floor, and the output says so every time it prints. Two reasons:

- **Affiliate-held shares are excluded.** Apple's affiliates hold a fraction of
  a percent and the implied price lands within about 1% of that day's close
  ($220.18 against ~$217 on 2025-03-28). Tesla's insiders hold roughly 13%, so
  its implied $237.96 sits about a fifth under the ~$317 close on the same date.
  The gap *is* the insider stake, and a 10-K doesn't disclose it — beneficial
  ownership is incorporated by reference from the proxy.
- **The two numbers are months apart**, so a filer that bought back stock in
  between divides by too few shares.

Market cap therefore restates the public float, and `--price` overrides the
whole thing when you want an outside number. A two-class filer gets no implied
price at all: one float figure divided by BRK.A plus BRK.B prices the B shares
at $649 against a real ~$485, so it refuses, the same way the market cap does.

## Condensed sections

`sections/item1a_risk_factors.md` runs 42,000 characters for Costco and 70,000+
for a big filer. `summaries/` holds the same Item at about a third the length.

It's extractive, not generative — no model, no key, no cost, same as everything
else here. Every sentence in a summary is a sentence the company wrote. The rule
is: keep the opening sentence of each paragraph, keep every sentence carrying a
figure, drop cross-references ("see Note 7") and the standard disclaimers, and
drop a heading left with nothing under it. Headings come from the filing's own
bold runs, which is also how the risk-factor index works — guessing instead ("a
lone sentence under 200 characters is a headline") promoted half of Apple's
product paragraphs to headings.

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
`sec_client.UA` has it.

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
which concepts fed each row and which were tried and not found. The same rule is
why `valuation.md` prints no enterprise value at all when debt or cash isn't
tagged: JPMorgan's bridge would otherwise read $989bn instead of $1.15tn, and it
would look like a real number.

**The cover-page share count is dimensioned for multi-class filers**, and the XBRL
companyfacts API drops every dimensioned fact. Berkshire's undimensioned
`EntityCommonStockSharesOutstanding` is therefore its *2011* number — 943,242
shares, still sitting there looking like data. Match the fact on the filing's own
accession number, and when nothing matches, read the rendered cover page, which
lists each class.

**One price across two share classes is not a market cap.** BRK.A is ~1,500x BRK.B.
Summing the classes and applying the B price undercounts by hundreds of billions,
so a class gap over 20x refuses to compute market cap and asks for `--shares`.

**Options and RSU counts are dimensioned too**, so companyfacts is useless for
them — Apple's last undimensioned RSU count is from 2013. They come out of the
rendered footnote instead, which brings its own traps: counts are scaled in the
table's unit header ("shares in Thousands" for Apple, "in Millions" for NVIDIA);
"Outstanding at the end of 2024" is the *opening* balance of the 2025 roll-forward;
and Coca-Cola's "Outstanding on December 31, Weighted-Average Exercise Price" of
$55.74 parses as 55,740,000 options unless price-ish rows are excluded by label.

**`LongTermDebt` means two different things.** Apple tags it including current
maturities (82,300 = 71,340 + 11,007); Tesla tags it excluding them (6,584, with
1,569 of current debt tagged separately). Nothing in the data says which. So the
debt bridge prefers `LongTermDebtNoncurrent` plus a current bucket, then concepts
whose names promise they include current maturities, and only then falls back to
`LongTermDebt` — where it adds the current bucket and says in the output that the
figure needs checking. Same class of trap on the short end: `DebtCurrent` already
contains commercial paper and current maturities, so adding those to it
double-counts.

**McDonald's tags its diluted share count in millions.** The XBRL value is literally
716.4 against a cover-page count of 710,398,642. Nothing real moves a share count
by 1000x in a year, so `valuation.md` rescales by the power of ten that lines the
two up and flags that it did. `trends.md` still shows the raw tagged value.

**Quote feeds were a dead end, and then they turned out to be unnecessary.**
Stooq's free CSV went behind a JavaScript proof-of-work wall in 2026 and answers
a script with an HTML challenge page. Yahoo's chart endpoint worked, but it made
the tool depend on an undocumented endpoint and put one live number next to fifty
as-filed ones. The cover page has the answer — see "The price, from the filing"
above — so there is no quote call at all now, and nothing in the output moves
after the filing date.

**Section text arrives one line per inline element.** `get_text("\n")` splits on
every tag boundary, so Apple's "iPhone ® is the Company's line of smartphones"
comes out as three separate lines. Sentence-level condensing on that produces
confetti, and a short fragment looks exactly like a heading. Both the extractor
and the page reflow first: a line opening lowercase or with punctuation belongs
to the line above.

**Don't merge sentences across a trailing number.** An early sentence splitter
treated "1." as a list marker and joined it to what followed — which meant every
paragraph ending "...in 2025." swallowed its next sentence, and that sentence was
usually the boilerplate the condenser exists to drop.

**Most 10-Ks have no quarterly data at all.** The SEC dropped the
selected-quarterly-financial-data requirement in 2021, so MRQ revenue is blank
for AAPL, NVDA, TSLA, JPM, KO, MCD, COST and BRK alike. Older filings do tag it
(Costco's 2017 10-K has its Q4) and the lookup finds those. The row stays blank
and says why. The quarters filed *after* the year end are a different matter —
see below.

**The 10-K is up to a year stale, and the fix is the 10-Q, not the 8-K.** An
earnings 8-K beats the periodic report by about a week (Costco's median lead over
40 filings since 2016: 7 days for a 10-Q, 13 for a 10-K) and carries no
machine-readable financials at all — Costco has filed dozens and contributes
*zero* facts to the XBRL API from any of them, because only the cover page of an
8-K is tagged. A 10-Q is fully tagged, so `trends.ttm()` rolls the audited year
forward:

    TTM = fiscal year + year-to-date this year − year-to-date a year ago

Year-to-date, never four discrete quarters. A 10-Q's cash flow statement is
always cumulative from the year start, so there is no discrete-quarter operating
cash flow to sum, and the fourth quarter never appears in a 10-Q at all — a
four-quarter sum is always missing a leg. One shape works for every line on every
statement. The balance sheet moves to the quarter end too, so the EV bridge isn't
a fresh numerator over a year-old denominator. It all comes out of the
`companyfacts` call the run already makes, so it costs **zero extra SEC
requests**. `--no-ttm` turns it off.

Three traps found building it:

- **Filers abandon concepts without deleting the history.** Uber tagged
  `RevenueFromContractWithCustomerExcludingAssessedTax` in its 10-Qs only through
  2019 and reports `Revenues` now. "First concept with any rows" locked onto a
  series seven years dead and found no quarter to anchor on, so concept selection
  requires rows *after* the fiscal year end.
- **Banks tag no revenue in their 10-Qs.** JPMorgan tags `Revenues` in the 10-K
  and nothing from the revenue list in the 10-Q. Anchoring on revenue alone threw
  away a net-income roll-forward it can perfectly well support, so the anchor
  falls back to net income and the lines it can't carry come back in
  `not_tagged`.
- **An accounting-standard change inside the window is fatal.** Costco's FY2017
  returns no TTM, correctly: ASC 606 means the current quarter and its prior-year
  comparative sit under different concepts, and differencing them would subtract
  two different definitions of revenue. The two periods must come from one
  concept or the answer is refused.

**A rendered statement can put the quarters before the year.** Costco's 2017
income statement opens with seven "3 Months Ended" columns and doesn't reach the
twelve-month ones until index 9 — and two of its columns are both headed
"Sep. 03, 2017", one the fourth quarter and one the year, with different numbers
under each. Taking the leftmost number returned a quarter's membership fees
($644M) as the year's ($2,853M). `parse_report` now keeps the period level of the
header, which fixes the pick and stops the markdown from printing the same column
header twice with different figures.

**Revenue split by product or segment never reaches companyfacts.** The XBRL API
carries undimensioned facts only, so Costco's membership fees — a $5,323M line
sitting in plain sight on its income statement — are simply absent from it. Those
come off the rendered statement instead, where the dimension label is a row with
no numbers ("Membership fees") and the value sits two rows below it.

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

10-K documents only. The 10-Q contributes numbers (the TTM roll-forward above)
but none of its text is parsed, and no 8-K or proxy is read at all. An 8-K
monitor — Item 5.02 departures, 2.01 closings, 4.02 non-reliance, the events with
no scheduled filing — would be a separate tool, not a bigger version of this one.
No Exhibit 21 subsidiary parsing. Note
tables are behind `--all-tables` rather than on by default (free, but ~70 extra
requests and a lot of files).
