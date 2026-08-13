---
name: 10k
description: Pull a company's 10-K from SEC EDGAR and analyze it. Use when Darren names a ticker and wants the annual report, financial statements, risk factors, business description, MD&A, segment detail, or a read on what changed year over year. Triggers on "/10k", "pull the 10-K for X", "what are X's risk factors", "read X's annual report", "what did X say about Y in their filing".
---

# 10-K analysis

## Run the extractor first

```bash
cd "/Users/darren/Claude/Claude Code/10K AI" && ./run_10k.sh <TICKER>
```

Free, no API key, no quota — it's all SEC EDGAR. Takes ~15 seconds cold, instant
when cached. Output lands in `filings/<TICKER>/<fiscal-year-end>/`.

Useful flags: `--year 2019` for an older filing, `--all-tables` to also pull every
note table (segment revenue, geographic split, debt maturities, leases),
`--refresh` to bypass the cache, `--cik N` for tickers SEC's map doesn't list,
`--price 250` / `--shares N` to override the price or the share count (needed for
multi-class filers like BRK), `--filing-price` to price off the cover page
instead of a live quote, `--no-price` to skip the price entirely.

Every figure comes out of a filing — the 10-K, or a 10-Q for anything trailing —
with one exception: the share price is a live quote. It is never cached, so a
re-run that short-circuits on the cache carries the price from when it was
written; `--refresh` re-prices.

## Then read, in this order

1. **`SUMMARY.md`** — always start here. Company identifiers, latest-year numbers,
   revenue/EPS history, risk-diff headline, and an index of every other file.
2. Whichever file answers the actual question:

| Question | File |
|---|---|
| A fast read on any Item | `summaries/<same name>.md` — the section condensed to about a third, every sentence the filer's own |
| What does the company do? Segments, customers? | `sections/item1_business.md` |
| What are the risks? | `risk_headings.md` first (the index), then `sections/item1a_risk_factors.md` |
| What changed in the risks this year? | `risk_diff.md` |
| Why did results move? | `sections/item7_mdna.md` |
| Exact statement line items, as filed | `statements/income_statement.md`, `balance_sheet.md`, `cash_flow.md` |
| Multi-year trend, margins, FCF, ROE | `trends.md` |
| Share count, market cap, EV, EBIT/EBITDA, multiples | `valuation.md` |
| Current run-rate, not the audited year | the **Trailing twelve months** section of `SUMMARY.md` and `valuation.md` (`ttm` in the JSON twins) |
| One block of model-ready inputs: LTM flows, latest balance sheet, live price | the **Latest data** section of `SUMMARY.md` (`ttm` / `mrq` / `snapshot` in `trends.json`) |
| Last quarter's revenue and the same quarter a year ago | `mrq` in `trends.json` — the discrete quarter, not the year to date |
| Lawsuits | `sections/item3_legal_proceedings.md` |
| Accounting notes, segment detail | `sections/item8_financial_statements.md` |
| Rates/FX/commodity exposure | `sections/item7a_market_risk.md` |
| Auditor, CIK, SIC, source URL | `metadata.json` |

Sections run large (Item 1A is often 70k+ characters, Item 8 can top 150k). Read
the specific section file, not `full_text.txt`. When the question is "what does
this section say", `summaries/` answers it at a third the length; when the
question turns on exact wording, read the full section in `sections/`.

## Analysis rules

Follow the house rules in `~/Claude/Claude Code/CLAUDE.md`:

- **Separate facts, assumptions, and opinions.** As-filed numbers are facts.
  Ratios in `trends.md` are computed by the tool — say so. Anything about what
  it means for the stock is opinion, labeled as such.
- **Use real numbers.** They're all in the output. Never estimate a figure that
  sits in `trends.json` or a statement file.
- **Never invent.** If a metric shows `—`, the company didn't tag it — say that.
  `trends.md` lists exactly which XBRL concepts fed each row and which were
  missing. Banks and insurers legitimately have no gross profit or capex line.
- **Read `risk_diff.md` critically.** Added risks are the highest-signal part of
  a 10-K. But when the file flags a wholesale rewrite, "added" means "reworded",
  not "new" — check each one against its closest dropped counterpart before
  claiming a company started worrying about something.
- **Three share counts, three meanings** (`valuation.md`): the cover-page count is
  actual shares outstanding weeks after year end, the weighted-average diluted
  count is a full-year average, and fully diluted adds options and unvested RSUs
  from the footnote. Say which one a per-share figure uses.
- **The price is live; everything it multiplies is not.** The quote carries its
  own timestamp in `valuation.json` — say which one, and pair it with LTM
  figures rather than the audited year when the question is about the multiple
  today. If the feed was unreachable the run falls back to the cover page's
  aggregate market value of non-affiliate common equity divided by shares
  outstanding, which is a **floor**: it excludes insider-held shares, so a filer
  with a large insider stake prices low by roughly that percentage (Apple lands
  within ~1% of the real close, Tesla about a fifth under), and the two numbers
  are stamped months apart. The output flags which of the two it used
  (`is_live` vs `is_floor`). Never call a floor price the current price.
- **A condensed section is not a paraphrase.** `summaries/` keeps whole
  sentences the company wrote and drops others; it never rewords. But it *does*
  drop sentences, so don't claim a filing is silent on something from a summary
  alone — check `sections/` before saying a 10-K doesn't mention X.
- **EV comes with its own caveats.** The file states how total debt was assembled
  and refuses to print an EV at all when debt or cash isn't tagged. Don't fill
  that gap with an estimate.
- **The annual column is the audited one; the TTM column is the current one.**
  TTM = fiscal year + this year's year-to-date − last year's year-to-date at the
  same quarter, from the 10-Qs, with the balance sheet moved to the quarter end.
  Use TTM when the question is about the run-rate now and say which quarter it
  runs to; use the fiscal year when the question is about what was audited, filed
  or discussed in the MD&A. Never mix them inside one multiple. Lines a filer
  doesn't tag quarterly are listed in `not_tagged` and are blank in the TTM
  column only — banks typically have no quarterly revenue there. When the section
  says there is no TTM, the 10-K is the newest data and that is the answer.
- **Flow items are LTM; stock items are a snapshot.** The **Latest data**
  section is the block to quote when someone is building a model. Income and
  cash-flow lines there are twelve months of trading ending at the latest
  quarter; balance-sheet lines are one date off one filing, with no arithmetic
  at all. Don't average a balance sheet or annualise a quarter to make them
  match — they aren't supposed to.
- **MRQ revenue is one quarter, not a year.** `mrq` in `trends.json` is the
  discrete three months, taken from the 10-Q, with the same quarter a year
  earlier beside it. `basis` says how it was arrived at: tagged directly, or
  differenced out of two year-to-date figures, or — when the 10-K is the newest
  filing — the year less its last 10-Q, which is the fourth quarter. Never
  multiply it by four and call it a run-rate.
- **Two share counts in the snapshot, and they are not interchangeable.** The
  cover-page count is shares actually outstanding weeks after the quarter
  closed (use it for market cap); the weighted-average diluted count spans the
  quarter and includes award dilution (use it for per-share figures).
- Watch for split-driven breaks in the EPS and share-count rows of `trends.md`;
  the file explains why they're there.
