---
name: 10k
description: Pull a company's 10-K from SEC EDGAR and analyze it. Use when the user names a ticker and wants the annual report, financial statements, risk factors, business description, MD&A, segment detail, or a read on what changed year over year. Triggers on "/10k", "pull the 10-K for X", "what are X's risk factors", "read X's annual report", "what did X say about Y in their filing".
---

# 10-K analysis

## Run the extractor first

```bash
./run_10k.sh <TICKER>
```

Free, no API key, no quota — it's all SEC EDGAR. Takes ~15 seconds cold, instant
when cached. Output lands in `filings/<TICKER>/<fiscal-year-end>/`.

Useful flags: `--year 2019` for an older filing, `--all-tables` to also pull every
note table (segment revenue, geographic split, debt maturities, leases),
`--refresh` to bypass the cache, `--cik N` for tickers SEC's map doesn't list.

## Then read, in this order

1. **`SUMMARY.md`** — always start here. Company identifiers, latest-year numbers,
   revenue/EPS history, risk-diff headline, and an index of every other file.
2. Whichever file answers the actual question:

| Question | File |
|---|---|
| What does the company do? Segments, customers? | `sections/item1_business.md` |
| What are the risks? | `risk_headings.md` first (the index), then `sections/item1a_risk_factors.md` |
| What changed in the risks this year? | `risk_diff.md` |
| Why did results move? | `sections/item7_mdna.md` |
| Exact statement line items, as filed | `statements/income_statement.md`, `balance_sheet.md`, `cash_flow.md` |
| Multi-year trend, margins, FCF, ROE | `trends.md` |
| Lawsuits | `sections/item3_legal_proceedings.md` |
| Accounting notes, segment detail | `sections/item8_financial_statements.md` |
| Rates/FX/commodity exposure | `sections/item7a_market_risk.md` |
| Auditor, CIK, SIC, source URL | `metadata.json` |

Sections run large (Item 1A is often 70k+ characters, Item 8 can top 150k). Read
the specific section file, not `full_text.txt`.

## Analysis rules

Ground rules for reading the output:

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
- Watch for split-driven breaks in the EPS and share-count rows of `trends.md`;
  the file explains why they're there.
