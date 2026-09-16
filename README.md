# Filed — a 10-K extractor

Give it a ticker, get back the 10-K and everything useful inside it: each Item as
its own file plus a condensed version of it, the as-filed financial statements,
ten years of financials, an index of every risk factor, a diff of this year's
risks against last year's, and the valuation — real share count, market cap, the
EV bridge and the multiples. Then the 10-Qs filed since roll that audited year
forward to the most recent quarter.

**Every number comes out of a filing — the 10-K, or a 10-Q for the trailing
twelve months — with exactly one exception, the share price.** That one is a
live quote, because a market cap is the one figure here with no business being
nine months stale. `--filing-price` turns it off and prices the company off its
own cover page instead; see "The price" below for what each costs.

```bash
./run_10k.sh AAPL
```

Or in a browser — same extractor, ticker box instead of a terminal:

```bash
python3 serve.py
```

Then <http://localhost:4321>. See `site/README.md` for what the server exposes.
Live test: <https://filed-br1e.onrender.com>

Or from the Dock. `./make_app.sh` builds **Filed.app** into `/Applications`: it
starts the server if it isn't already up and opens the site. It's a launcher,
not a copy — it runs `serve.py` out of this directory, so edits here land in the
app immediately. Re-run `make_app.sh` if you move the project, because the
launcher holds absolute paths. Log: `~/Library/Logs/Filed.log`. To stop the
server, `pkill -f serve.py`.

All of it comes from SEC EDGAR, plus one keyless quote call. **No API key, no
quota, no cost.** This matters — FMP's free tier is 250 calls/day and doesn't
serve 10-K text at all, so nothing here touches your FMP budget. A full
extraction is ~17 SEC requests (two more for a filer that reports membership
fees) plus that one quote, and about 15 seconds cold, instant once cached.
`--no-price` skips the price and everything downstream of it.

The quote is deliberately never cached — a cached quote is a stale number
wearing a live timestamp — but the rest of the run is, so re-running a ticker
short-circuits before the price is fetched. `--refresh` re-prices.

No LLM calls either. The tool extracts and structures; ask Claude to read the
output (see `SKILL.md`, which wires up `/10k <TICKER>`).

## Usage

```bash
./run_10k.sh AAPL                 # latest 10-K
./run_10k.sh BRK.B                # dots and dashes both work
./run_10k.sh AAPL --year 2019     # an older filing (history capped, and priced off its own cover page)
./run_10k.sh TSLA --all-tables    # + every note table: segments, geography, debt, leases
./run_10k.sh JPM --refresh        # bypass the cache
./run_10k.sh SOMECO --cik 12345   # ticker SEC's map doesn't list
./run_10k.sh AAPL --price 250     # your own price instead of the live quote
./run_10k.sh BRK.B --shares 2.16e9  # your own share count — needed for multi-class filers
./run_10k.sh COST --filing-price  # no quote call — price off the cover page instead
./run_10k.sh COST --no-price      # no price, so no market cap, EV or multiples
./run_10k.sh AAPL --rebuild       # regenerate every output from the cache, re-price, no download
```

`--refresh` is for "the data might have changed" and re-downloads. `--rebuild` is
for "the code changed": it reads the cached SEC responses, takes one fresh quote,
and rewrites every file. Nothing hits EDGAR.

Output goes to `filings/<TICKER>/<fiscal-year-end>/`. Start with `SUMMARY.md`.

```
SUMMARY.md          key numbers, valuation, LTM + snapshot block, revenue/EPS history, risk diff, file index
metadata.json       CIK, SIC industry, auditor + PCAOB ID, exchange, source URLs
sections/           item1_business, item1a_risk_factors, item7_mdna, item8_..., 10 in all
summaries/          the same Items condensed to about a third, every sentence the filer's own
statements/         income_statement, balance_sheet, cash_flow, equity (.md + .json)
trends.md/.json     10 years of financials + margins, FCF, ROE, ROIC, the working
                    capital cycle, CAGR, share count
                    (.json also carries ttm, mrq and the balance-sheet snapshot)
model.csv           the same history as one spreadsheet — years across, raw dollars,
                    blanks left blank, LTM as the last column
xbrl_by_year.json   every concept this filer tagged, valued at each year end
valuation.md/.json  share count three ways, price, market cap, EV bridge, EBIT/EBITDA,
                    multiples, and what every earlier year's filing implied
risk_headings.md    every risk factor headline — the Item 1A index
risk_diff.md        risks added, removed, and reworded vs last year
full_text.txt       the whole filing as plain text
10-K.htm            raw, exactly as filed
```

Re-running is a no-op unless you pass `--refresh`. Responses are cached to
`.cache/` forever, which is safe because filings are immutable once accepted.

## The price

Live by default: one call to Yahoo's chart endpoint, no key, no quota, and
never cached. It's the only number in the whole tool that isn't tagged data out
of EDGAR, and it earns the exception — every other figure is a fact about a
period that has already closed, while a market cap is a fact about right now.
An EV/EBITDA built on a fresh LTM numerator and a nine-month-old price is
wrong in a way that's hard to see.

If the call fails, the run falls back to the filing's own price below and says
so, rather than dying or printing nothing. `--filing-price` skips the call
outright; `--price` takes your own number; `--no-price` drops the price and
everything downstream of it.

`--year` skips it too, automatically. Everything else in a historical run is
stamped to that fiscal year, and today's quote over a 2019 share count is not a
2019 market cap — it's two eras multiplied together. The cover page carries a
price contemporary with the filing, which is the point of asking for an old one.

### The filing's own price, and why it's a floor

A 10-K states no closing price anywhere. The quarterly high/low table stopped
being required in 2018, and the performance graph is indexed to $100. What every
cover page does state, and tags in XBRL, is two numbers: the **aggregate market
value of common equity held by non-affiliates**, measured on the last business
day of the second fiscal quarter, and the **share count**, taken a few weeks
after year end. Their quotient is a share price — the one this tool used before
the live quote existed, and the one it falls back to.

It's a floor, and the output says so every time it prints. Two reasons:

- **Affiliate-held shares are excluded.** Apple's affiliates hold a fraction of
  a percent and the implied price lands within about 1% of that day's close
  ($220.18 against ~$217 on 2025-03-28). Tesla's insiders hold roughly 13%, so
  its implied $237.96 sits about a fifth under the ~$317 close on the same date.
  The gap *is* the insider stake, and a 10-K doesn't disclose it — beneficial
  ownership is incorporated by reference from the proxy.
- **The two numbers are months apart**, so a filer that bought back stock in
  between divides by too few shares.

Market cap on this basis therefore restates the public float. A two-class filer
gets no implied price at all: one float figure divided by BRK.A plus BRK.B
prices the B shares at $649 against a real ~$485, so it refuses, the same way
the market cap does. (The live quote has no such problem — it prices whichever
class you asked for — but the market cap still refuses, because summing A and B
shares and applying the B price is wrong by orders of magnitude either way.
`--shares` is the answer there.)

## Flows, stocks, and the price walk into a spreadsheet

`SUMMARY.md` has a **Latest data** block that is the one table to copy into a
model. It splits every figure by vintage, because that's the thing that goes
wrong when you assemble a valuation by hand:

| Kind | What it means | Where it comes from |
|---|---|---|
| Flow — LTM | twelve months of trading ending at the latest quarter | 10-K + this year's YTD 10-Q − last year's same YTD |
| Stock — snapshot | one date, one filing, no arithmetic | the latest 10-Q's balance sheet (the 10-K's, if no 10-Q has been filed since) |
| Price | right now | live quote |

Flow items: revenue, operating income, D&A, net income, cash from ops, capex,
and membership fee income for the warehouse clubs. Stock items: cash + ST
investments, total debt, total assets, inventory, and the share count.

Two things there are worth knowing about.

**Membership fee income doesn't exist in companyfacts.** Costco and BJ's tag it
along a dimension, and the XBRL company-facts API carries undimensioned facts
only, so the line is simply absent — no amount of concept-hunting finds it. Both
LTM legs come off the 10-Q's *rendered* income statement instead, which prints
the current and prior-year periods side by side, so one table supplies both and
they can't be a concept apart. Costs two extra requests, and only for a filer
that reports the line at all.

**MRQ revenue is a discrete quarter, and getting it is fiddlier than it looks.**
Q2 and Q3 tag a cumulative period ending the same day as the discrete one — pick
wrong and you report eight months as a quarter. Filers that tag no discrete
quarter get it differenced out of two year-to-date figures sharing a fiscal-year
start. And when the 10-K is the newest filing, the most recent quarter *is* Q4,
which is the year less the last 10-Q inside it (Costco FY2025: 275,235 − 189,079
= 86,156). `mrq.basis` in `trends.json` states which route was used.

The snapshot is deliberately independent of the TTM roll-forward. The roll needs
a comparable prior-year period and returns nothing without one — but a balance
sheet needs no comparable, so a filer that can't be rolled forward still gets
its cash, debt, assets, inventory and share count.

## The model block

The ten-year table is not just the income statement. Everything a forecast is
built from comes out of the same free companyfacts call, so it is all there:

| | |
|---|---|
| Income | SG&A, R&D, selling & marketing, stock comp, income tax expense, interest income |
| Balance sheet | receivables, payables, current assets and liabilities, PP&E, goodwill, intangibles, deferred revenue, retained earnings |
| Cash flow | acquisitions, buybacks, dividends |
| Computed | effective tax rate, NOPAT, invested capital, ROIC, asset turnover, current ratio, working capital, net debt / EBITDA, interest coverage, receivable / inventory / payable days, the cash conversion cycle, capex and R&D and SG&A and stock comp as a share of revenue, FCF conversion, FCF and book value and revenue per share, dividend payout, dividends + buybacks against free cash flow |

Two of those are worth spelling out, because the definition is the number:

- **Effective tax rate** is income tax expense over pretax income, computed only
  when pretax income is positive. A tax rate over a loss is a number with no
  forecasting meaning and printing one invites it into a model.
- **Invested capital** is total debt + total equity − cash and short-term
  investments, and ROIC is NOPAT over that. Other definitions exist; this one is
  printed beside the figure so it can be recomputed rather than guessed at.

Receivable days run on revenue and inventory and payable days on cost of
revenue, so a filer with no cost line — a bank — gets blanks rather than days
computed against the wrong denominator.

`model.csv` is all of it in one file: metrics down, fiscal years across, the
trailing twelve months as the last column, values in whole dollars with no
symbols or separators, and an empty cell wherever the filer tagged nothing. The
blank matters — writing 0 there would be a number the company never reported,
and every average taken over that column afterwards would be wrong.

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

**Only one free quote feed still answers a script.** Stooq's CSV went behind a
JavaScript proof-of-work wall in 2026 and returns an HTML challenge page.
Yahoo's chart endpoint (`query1.finance.yahoo.com/v8/finance/chart/<TICKER>`)
works, keyless, with the same contact-email User-Agent SEC wants, and takes
SEC's dashed class tickers unchanged (`BRK-B`). It's undocumented, so it is
wrapped in a bare `except` and falls back to the cover-page price — a valuation
that loses its market cap because a quote host had a bad minute is worse than
one priced off the filing. Don't route it through `SECClient.get()`: that cache
never expires by design, and a cached quote is a stale number wearing a live
timestamp.

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

**A filer can change the scale it tags a share count at, mid-history.**
McDonald's tags `WeightedAverageNumberOfDilutedSharesOutstanding` as
750,100,000 through fiscal 2020 and as 716.4 from 2021 — the same figure, in
millions. Untouched it rendered as 0.0M shares, a −100% share-count change in
2021 and a +100,000% one in 2022, and every per-share figure over it was out by
a factor of a million. The cross-check is the filer's own arithmetic: diluted
EPS is net income over the diluted count, so net income ÷ EPS is what the count
has to be. That is also what makes the fix safe against a reverse split, where
the count really does fall hard — EPS moves with it, so the implied count moves
too and nothing is rescaled. Only a units error puts a clean power of ten
between them.

**A 10-K restates the two years before it, so "this year's filing" is the
*earliest*-filed row, not the latest.** Everywhere else in this tool the newest
restatement wins, because the question is "what is this figure now". For pricing
a historical year the question is "which document is this", and the newest row
for FY2019 belongs to the FY2021 10-K. The public float and share count on a
cover page belong to the filing that printed them; pairing FY2019's numbers with
FY2021's cover page prices the wrong year.

**A stock split silently breaks any per-share ratio built across it.** Apple's
FY2019 cover page states 4,443,265,000 shares and an implied $196.86, while the
FY2019 EPS in the ten-year series is $2.97 — restated for the 4-for-1 split of
August 2020. Dividing one by the other gives a P/E of 66 against a real 16.6,
and nothing about 66 looks wrong. Dollar figures are immune, so the historical
P/E is built out of market cap over net income instead, and every EV multiple
was already dollars over dollars. Two further traps under that one: the split
factor is detected by comparing a year's own filed share count against the
current series, which only fires on the years a later filing actually restated —
Apple's FY2018 and FY2019, not its FY2016 — so the factor has to be carried
backwards, because a split after 2018 is also a split after 2016. And in the
page's own tables, the change in a margin is a difference in points, never a
percentage change: "gross margin grew 2%" is a sentence with no meaning.

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
