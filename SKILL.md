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
multi-class filers like BRK), `--no-price` to skip the price entirely.

Every figure comes out of the 10-K, the share price included — there is no quote
feed anywhere in this tool.

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
- **The price is the cover page's, and it's a floor.** There is no quote feed.
  `valuation.md` divides the cover page's aggregate market value of non-affiliate
  common equity by shares outstanding. That excludes insider-held shares, so a
  filer with a large insider stake prices low by roughly that percentage (Apple
  lands within ~1% of the real close, Tesla about a fifth under), and the two
  numbers are stamped months apart. Market cap is therefore the public float
  restated, and every multiple inherits the same floor. Say which date the price
  is from — it's printed — and never call it the current price.
- **A condensed section is not a paraphrase.** `summaries/` keeps whole
  sentences the company wrote and drops others; it never rewords. But it *does*
  drop sentences, so don't claim a filing is silent on something from a summary
  alone — check `sections/` before saying a 10-K doesn't mention X.
- **EV comes with its own caveats.** The file states how total debt was assembled
  and refuses to print an EV at all when debt or cash isn't tagged. Don't fill
  that gap with an estimate.
- Watch for split-driven breaks in the EPS and share-count rows of `trends.md`;
  the file explains why they're there.
