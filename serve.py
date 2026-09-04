#!/usr/bin/env python3
"""Serve the site and run the 10-K extractor behind it.

    python3 serve.py            # http://localhost:4321
    python3 serve.py --port 8080

Stdlib only — no Flask, no new dependencies. The site is static files; the one
dynamic route shells out to the same extractor the CLI uses, so the page and
`./run_10k.sh AAPL` can never drift apart.

    POST /api/extract  {"ticker": "AAPL"}  -> structured JSON for the page
    GET  /api/cached                       -> tickers already on disk
    GET  /filings/...                      -> the extracted files themselves
"""

import argparse
import json
import re
import subprocess
import sys
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
FILINGS = ROOT / "filings"
HOME = "v4-icarus.html"

# trends.py is stdlib-only, so importing it costs the server no dependency and
# saves restating which lines are flows and which are stocks — the one thing
# the latest-quarter column has to get right, since flows come off the TTM roll
# and stocks off the balance sheet, and mixing them is the classic way to build
# a valuation that is internally inconsistent.
sys.path.insert(0, str(ROOT / "scripts"))
import trends  # noqa: E402

FLOW_KEYS = set(trends.DURATION_CONCEPTS)
STOCK_KEYS = set(trends.INSTANT_CONCEPTS)

# A cold extraction is ~12 SEC requests and ~15s. Give it room, but never hang
# a browser connection forever if SEC stalls.
EXTRACT_TIMEOUT = 180

TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")

# One extraction at a time. SEC asks for =<10 req/s and the client already
# paces itself; letting a page-refresh spawn parallel runs would blow past that
# and race two writers into the same filings/ directory.
_extract_lock = threading.Lock()


# --- the metric catalogue -------------------------------------------------
#
# One row per number the page can name, and the only place a label lives. The
# aliases are the point: nobody types "operating_cash_flow", they type "cash
# from operations" or "cfo", and a search box that only matches the key is a
# search box that answers nothing. Each entry also picks up the XBRL concepts
# that actually fed it, so searching "LongTermDebtNoncurrent" finds the row it
# built.
#
#   kind    how the page formats it: money | pct | decimal | shares | ratio
#   source  tagged (the filer's own number) or computed (this tool's arithmetic)
CATALOGUE = [
    # key, label, category, kind, source, aliases
    ("revenue", "Revenue", "income", "money", "tagged",
     "sales net sales total revenue turnover top line"),
    ("cost_of_revenue", "Cost of revenue", "income", "money", "tagged",
     "cogs cost of sales cost of goods sold"),
    ("gross_profit", "Gross profit", "income", "money", "tagged", "gross margin dollars"),
    ("rnd_expense", "R&D expense", "income", "money", "tagged",
     "research and development rnd r&d"),
    ("operating_income", "Operating income", "income", "money", "tagged",
     "operating profit ebit income from operations"),
    ("pretax_income", "Pretax income", "income", "money", "tagged",
     "income before tax ebt profit before tax"),
    ("interest_expense", "Interest expense", "income", "money", "tagged",
     "interest cost of debt"),
    ("net_income", "Net income", "income", "money", "tagged",
     "profit earnings bottom line net earnings"),
    ("mrq_revenue", "Revenue, fourth quarter", "income", "money", "tagged",
     "q4 quarterly revenue mrq most recent quarter"),
    ("mrq_revenue_prior", "Revenue, prior-year Q4", "income", "money", "computed",
     "q4 last year quarterly revenue comparison"),
    ("sga_expense", "SG&A", "income", "money", "tagged",
     "selling general administrative overhead opex operating expense"),
    ("selling_marketing_expense", "Selling & marketing", "income", "money", "tagged",
     "sales and marketing s&m advertising"),
    ("stock_comp", "Stock-based compensation", "income", "money", "tagged",
     "sbc share based comp equity compensation dilution non-cash"),
    ("income_tax_expense", "Income tax expense", "income", "money", "tagged",
     "tax provision for income taxes taxes"),
    ("interest_income", "Interest income", "income", "money", "tagged",
     "investment income interest earned"),

    ("eps_diluted", "EPS, diluted", "per_share", "per_share", "tagged",
     "earnings per share diluted eps"),
    ("shares_diluted", "Diluted shares", "per_share", "shares", "tagged",
     "weighted average share count diluted shares outstanding"),
    ("shares_change_pct", "Share count change", "per_share", "pct", "computed",
     "dilution buyback share count growth"),
    ("eps_basic", "EPS, basic", "per_share", "per_share", "tagged",
     "basic earnings per share"),
    ("shares_basic", "Basic shares", "per_share", "shares", "tagged",
     "weighted average basic share count"),
    ("dividends_per_share", "Dividends per share", "per_share", "per_share", "tagged",
     "dps dividend rate declared per share"),
    ("fcf_per_share", "FCF per share", "per_share", "decimal", "computed",
     "free cash flow per share"),
    ("book_value_per_share", "Book value per share", "per_share", "decimal", "computed",
     "bvps net asset value per share equity per share"),
    ("revenue_per_share", "Revenue per share", "per_share", "decimal", "computed",
     "sales per share"),

    ("operating_cash_flow", "Cash from operations", "cashflow", "money", "tagged",
     "cfo operating cash flow cash generated"),
    ("capex", "Capex", "cashflow", "money", "tagged",
     "capital expenditure purchases of property plant and equipment ppe"),
    ("free_cash_flow", "Free cash flow", "cashflow", "money", "computed",
     "fcf cash after capex"),
    ("d_and_a", "D&A", "cashflow", "money", "tagged",
     "depreciation amortization depreciation and amortisation"),
    ("buybacks", "Buybacks", "cashflow", "money", "tagged",
     "share repurchase repurchases of common stock"),
    ("dividends_paid", "Dividends paid", "cashflow", "money", "tagged",
     "dividend distributions to shareholders"),
    ("acquisitions", "Acquisitions", "cashflow", "money", "tagged",
     "m&a payments to acquire businesses bolt-on deals"),
    ("shareholder_returns", "Dividends + buybacks", "cashflow", "money", "computed",
     "capital returned total shareholder return payout"),

    ("total_assets", "Total assets", "balance", "money", "tagged", "assets balance sheet size"),
    ("total_liabilities", "Total liabilities", "balance", "money", "tagged", "liabilities"),
    ("total_equity", "Total equity", "balance", "money", "tagged",
     "shareholders equity stockholders equity book value net assets"),
    ("cash", "Cash and equivalents", "balance", "money", "tagged", "cash on hand liquidity"),
    ("short_term_investments", "Short-term investments", "balance", "money", "tagged",
     "marketable securities current investments"),
    ("cash_and_st_investments", "Cash + short-term investments", "balance", "money", "computed",
     "liquidity total cash gross cash"),
    ("inventory", "Inventory", "balance", "money", "tagged", "inventories stock on hand"),
    ("accounts_receivable", "Accounts receivable", "balance", "money", "tagged",
     "ar receivables trade debtors owed to the company"),
    ("accounts_payable", "Accounts payable", "balance", "money", "tagged",
     "ap payables trade creditors owed by the company"),
    ("current_assets", "Current assets", "balance", "money", "tagged", "total current assets"),
    ("current_liabilities", "Current liabilities", "balance", "money", "tagged",
     "total current liabilities"),
    ("working_capital", "Working capital", "balance", "money", "computed",
     "nwc net working capital current assets less current liabilities"),
    ("ppe_net", "PP&E, net", "balance", "money", "tagged",
     "property plant and equipment fixed assets net book value"),
    ("goodwill", "Goodwill", "balance", "money", "tagged", "acquisition premium"),
    ("intangibles", "Intangibles", "balance", "money", "tagged",
     "intangible assets patents customer relationships"),
    ("deferred_revenue", "Deferred revenue", "balance", "money", "tagged",
     "unearned revenue contract liability billings backlog"),
    ("retained_earnings", "Retained earnings", "balance", "money", "tagged",
     "accumulated deficit earned surplus"),
    ("long_term_investments", "Long-term investments", "balance", "money", "tagged",
     "noncurrent marketable securities"),
    ("invested_capital", "Invested capital", "balance", "money", "computed",
     "capital employed operating capital roic denominator"),
    ("total_debt", "Total debt", "balance", "money", "computed",
     "debt borrowings total borrowings gross debt indebtedness leverage"),
    ("long_term_debt", "Long-term debt (noncurrent)", "balance", "money", "tagged",
     "term debt bonds notes payable noncurrent debt"),
    ("current_debt", "Current maturities of long-term debt", "balance", "money", "tagged",
     "short term debt current portion"),
    ("debt_current_total", "Debt, current (whole bucket)", "balance", "money", "tagged",
     "debtcurrent short term debt total"),
    ("debt_incl_current", "Long-term debt incl. current maturities", "balance", "money",
     "tagged", "total term debt"),
    ("long_term_debt_ambiguous", "LongTermDebt (basis varies by filer)", "balance", "money",
     "tagged", "long term debt ambiguous"),
    ("short_term_borrowings", "Short-term borrowings", "balance", "money", "tagged",
     "revolver bank borrowings"),
    ("commercial_paper", "Commercial paper", "balance", "money", "tagged", "cp short term paper"),
    ("net_cash", "Net cash / (net debt)", "balance", "money", "computed",
     "net debt cash less debt"),
    ("preferred_stock", "Preferred stock", "balance", "money", "tagged", "preferred equity"),
    ("preferred_liquidation", "Preferred liquidation preference", "balance", "money", "tagged",
     "preferred"),
    ("preferred", "Preferred (for the EV bridge)", "balance", "money", "computed", "preferred"),
    ("minority_interest", "Minority interest", "balance", "money", "tagged",
     "noncontrolling interest nci"),
    ("operating_lease_liability", "Operating lease liabilities", "balance", "money", "tagged",
     "leases rent obligations"),
    ("finance_lease_liability", "Finance lease liabilities", "balance", "money", "tagged",
     "capital leases"),

    ("revenue_growth_pct", "Revenue growth", "ratios", "pct", "computed",
     "sales growth yoy top line growth"),
    ("net_income_growth_pct", "Net income growth", "ratios", "pct", "computed",
     "profit growth earnings growth yoy"),
    ("eps_growth_pct", "EPS growth", "ratios", "pct", "computed", "earnings per share growth"),
    ("gross_margin_pct", "Gross margin", "ratios", "pct", "computed", "gross profit margin"),
    ("operating_margin_pct", "Operating margin", "ratios", "pct", "computed",
     "ebit margin operating profitability"),
    ("net_margin_pct", "Net margin", "ratios", "pct", "computed", "profit margin net profitability"),
    ("fcf_margin_pct", "FCF margin", "ratios", "pct", "computed", "free cash flow margin"),
    ("roe_pct", "Return on equity", "ratios", "pct", "computed", "roe return on equity"),
    ("roa_pct", "Return on assets", "ratios", "pct", "computed", "roa return on assets"),
    ("debt_to_equity", "Debt / equity", "ratios", "decimal", "computed",
     "leverage gearing d/e debt to equity"),
    ("ebit", "EBIT", "ratios", "money", "computed", "operating profit earnings before interest"),
    ("ebitda", "EBITDA", "ratios", "money", "computed",
     "earnings before interest taxes depreciation amortization"),
    ("nopat", "NOPAT", "ratios", "money", "computed",
     "net operating profit after tax unlevered earnings"),
    ("effective_tax_rate_pct", "Effective tax rate", "ratios", "pct", "computed",
     "tax rate cash taxes provision rate"),
    ("roic_pct", "Return on invested capital", "ratios", "pct", "computed",
     "roic return on capital returns on capital employed roce"),
    ("asset_turnover", "Asset turnover", "ratios", "ratio", "computed",
     "sales over assets efficiency"),
    ("current_ratio", "Current ratio", "ratios", "decimal", "computed",
     "liquidity working capital ratio"),
    ("net_debt_to_ebitda", "Net debt / EBITDA", "ratios", "ratio", "computed",
     "leverage turns of debt gearing covenant"),
    ("interest_coverage", "Interest coverage", "ratios", "ratio", "computed",
     "times interest earned ebit over interest solvency"),
    ("receivable_days", "Receivable days", "ratios", "days", "computed",
     "dso days sales outstanding collection period working capital"),
    ("inventory_days", "Inventory days", "ratios", "days", "computed",
     "dio days inventory outstanding stock turn working capital"),
    ("payable_days", "Payable days", "ratios", "days", "computed",
     "dpo days payable outstanding supplier terms working capital"),
    ("cash_conversion_days", "Cash conversion cycle", "ratios", "days", "computed",
     "ccc working capital cycle days"),
    ("capex_pct_revenue", "Capex % of revenue", "ratios", "pct", "computed",
     "capital intensity reinvestment rate"),
    ("rnd_pct_revenue", "R&D % of revenue", "ratios", "pct", "computed",
     "research intensity"),
    ("sga_pct_revenue", "SG&A % of revenue", "ratios", "pct", "computed",
     "overhead ratio operating leverage"),
    ("stock_comp_pct_revenue", "Stock comp % of revenue", "ratios", "pct", "computed",
     "sbc intensity dilution cost"),
    ("fcf_conversion_pct", "FCF conversion", "ratios", "pct", "computed",
     "cash conversion earnings quality fcf over net income"),
    ("dividend_payout_pct", "Dividend payout", "ratios", "pct", "computed",
     "payout ratio dividend cover"),
    ("shareholder_returns_pct_fcf", "Dividends + buybacks, % of FCF", "ratios", "pct",
     "computed", "capital return payout sustainability"),
]

# The rows a discounted-cash-flow or comps model is actually assembled from, in
# the order you would lay them out in a sheet. It is a view over the catalogue
# above, not a second copy of it — every key here appears there too, in whatever
# statement it really belongs to.
MODEL_KEYS = [
    "revenue", "revenue_growth_pct", "gross_profit", "gross_margin_pct",
    "sga_expense", "rnd_expense", "operating_income", "operating_margin_pct",
    "d_and_a", "ebitda", "interest_expense", "pretax_income",
    "income_tax_expense", "effective_tax_rate_pct", "net_income", "eps_diluted",
    "shares_diluted", "operating_cash_flow", "stock_comp", "capex",
    "capex_pct_revenue", "free_cash_flow", "fcf_margin_pct",
    "accounts_receivable", "inventory", "accounts_payable", "working_capital",
    "receivable_days", "inventory_days", "payable_days", "cash_conversion_days",
    "ppe_net", "total_assets", "total_debt", "cash_and_st_investments", "net_cash",
    "total_equity", "invested_capital", "nopat", "roic_pct",
    "dividends_paid", "buybacks",
]

CATEGORY_LABELS = {
    "model": "Model inputs",
    "income": "Income statement",
    "per_share": "Per share",
    "cashflow": "Cash flow",
    "balance": "Balance sheet",
    "ratios": "Ratios & growth",
}

# "AccountsReceivableNetCurrent" -> "Accounts Receivable Net Current". Splitting
# on the case change is what makes a raw XBRL tag skimmable in a results list.
CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# "Sep. 27, 2025" out of a rendered statement column header.
COLUMN_DATE_RE = re.compile(r"([A-Za-z]{3})\w*\.?\s+(\d{1,2}),?\s+(\d{4})")
PERIOD_MONTHS_RE = re.compile(r"(\d+)\s*months?", re.I)
MONTH_NUMBERS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def money(v):
    """SEC tags raw dollars. Humans read millions."""
    if v is None:
        return None
    return round(v / 1_000_000, 1)


def _column_year(header, years):
    """Which fiscal year end a statement column belongs to, if any.

    Only annual columns are mapped. A filer that renders quarters beside the
    year — Costco's 2017 income statement opens with seven of them — would
    otherwise hand a quarter's revenue to a year's column, which is the same
    trap `statements._period_columns` exists to avoid one layer down.
    """
    months = PERIOD_MONTHS_RE.search(str(header))
    if months and int(months.group(1)) < 10:
        return None
    m = COLUMN_DATE_RE.search(str(header))
    if not m:
        return None
    month = MONTH_NUMBERS.get(m.group(1).lower())
    if not month:
        return None
    stamped = "{:04d}-{:02d}-{:02d}".format(int(m.group(3)), month, int(m.group(2)))
    return stamped if stamped in years else None


def year_columns(t):
    """{fiscal year end: {catalogue key: value}} — one column per year.

    Every figure is the raw tagged number in dollars or shares; the page formats
    by `kind`. Series first, then derived, because two keys (minority interest,
    the fourth-quarter revenue) live in both and the tagged value is the one to
    show.
    """
    series, derived = t["series"], t["derived"]
    out = {}
    for year in t["fiscal_year_ends"]:
        d = derived.get(year, {})
        row = {}
        for key, *_ in CATALOGUE:
            if key in series:
                row[key] = series[key]["values"].get(year)
            else:
                row[key] = d.get(key)
        out[year] = row
    return out


def latest_column(t):
    """The same keys at the most recent quarter, or None when there isn't one.

    Flows come from the trailing-twelve-month roll and stocks from the latest
    balance sheet, because those are two different kinds of number: twelve
    months of trading against a photograph taken on one date. Ratios come from
    the TTM block's own derived values, which are already built on that pairing.

    A filer with a balance sheet but no TTM (no comparable prior-year period)
    still gets its stock rows — the two are independent by design upstream and
    must stay independent here.
    """
    ttm, snap = t.get("ttm"), t.get("snapshot")
    if not ttm and not snap:
        return None
    flows = (ttm or {}).get("values", {})
    ratios = (ttm or {}).get("derived", {})
    stocks = (snap or {}).get("values", {})
    stock_derived = {
        "total_debt": (snap or {}).get("total_debt"),
        "cash_and_st_investments": (snap or {}).get("cash_and_st_investments"),
        "net_cash": (snap or {}).get("net_cash"),
    }
    row = {}
    for key, *_ in CATALOGUE:
        if key in FLOW_KEYS:
            row[key] = flows.get(key)
        elif key in STOCK_KEYS:
            row[key] = stocks.get(key)
        elif key in stock_derived and stock_derived[key] is not None:
            row[key] = stock_derived[key]
        else:
            row[key] = ratios.get(key)
    label = []
    if ttm:
        label.append("LTM to {}".format(ttm["quarter_end"]))
    if snap:
        label.append("{} balance sheet {}".format(snap.get("form") or "latest filing",
                                                  snap["as_of"]))
    return {
        "values": row,
        "label": " · ".join(label),
        "short_label": "Latest",
        "quarter_end": (ttm or {}).get("quarter_end"),
        "as_of": (snap or {}).get("as_of"),
        "has_flows": bool(ttm),
        "not_tagged": (ttm or {}).get("not_tagged") or [],
    }


def catalogue_for(t):
    """The catalogue with each row's own XBRL concepts folded into its search
    terms, so `LongTermDebtNoncurrent` finds the total-debt row it built."""
    series = t["series"]
    out = []
    for key, label, category, kind, source, aliases in CATALOGUE:
        concepts = (series.get(key) or {}).get("concepts") or []
        out.append({
            "key": key, "label": label, "category": category, "kind": kind,
            "source": source, "concepts": concepts,
            "terms": " ".join([label, aliases, key.replace("_", " ")] + concepts).lower(),
        })
    return out


def as_filed_statements(out_dir, years):
    """The rendered statements, with each column tied to a fiscal year end.

    Cell values stay the verbatim strings the filing rendered. Nothing is
    rescaled: a statement mixes dollars in millions, share counts in thousands
    and per-share amounts in one table, and the only place that is stated is the
    unit header, which travels with the table.
    """
    out = []
    for path in sorted((out_dir / "statements").glob("*.json")):
        try:
            table = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        columns = table.get("columns") or []
        out.append({
            "name": path.stem,
            "title": table.get("title") or path.stem,
            "units": table.get("units") or "",
            "columns": columns,
            "column_years": [_column_year(c, years) for c in columns],
            "rows": table.get("rows") or [],
        })
    return out


def every_tagged(out_dir):
    """xbrl_by_year.json, with each concept given a readable name."""
    path = out_dir / "xbrl_by_year.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [
        {
            "concept": concept,
            "label": CAMEL_RE.sub(" ", concept),
            "unit": entry.get("unit"),
            "type": entry.get("type"),
            "values": entry.get("values") or {},
        }
        for concept, entry in sorted((data.get("concepts") or {}).items())
    ]


def load_filing(out_dir):
    """Read the extractor's own output into the shape the page renders."""
    out_dir = Path(out_dir).resolve()
    meta = json.loads((out_dir / "metadata.json").read_text())
    # Not `trends` — that is the module imported above, and shadowing it here is
    # a trap for the next person to add a call to it.
    tj = json.loads((out_dir / "trends.json").read_text())

    fyes = tj["fiscal_year_ends"]
    latest = fyes[-1] if fyes else None
    derived = tj.get("derived", {})
    d = derived.get(latest, {}) if latest else {}
    series = tj.get("series", {})

    risk = {"summary": None, "added": []}
    diff_path = out_dir / "risk_diff.md"
    if diff_path.exists():
        text = diff_path.read_text()
        m = re.search(r"\*\*(\d+ added .*?)\*\*", text)
        if m:
            risk["summary"] = m.group(1)
        added = re.search(r"## Added\n(.*?)(?:\n## |\Z)", text, re.S)
        if added:
            # top-level bullets only — each added risk carries an indented
            # "closest dropped risk" sub-bullet that is not itself a new risk
            risk["added"] = [
                ln[2:].strip()
                for ln in added.group(1).splitlines()
                if ln.startswith("- ")
            ]

    # Written since the valuation pass was added; older extractions predate it.
    vpath = out_dir / "valuation.json"
    valuation, valuation_history = None, {}
    if vpath.exists():
        v = json.loads(vpath.read_text())
        # One row per earlier year, priced off that year's own cover page. Money
        # in millions like everything else the page receives; the ratios and the
        # split factor pass through as they are.
        for year, r in (v.get("history") or {}).items():
            valuation_history[year] = dict(
                r,
                market_cap_m=money(r.get("market_cap")),
                ev_m=money(r.get("enterprise_value")),
                total_debt_m=money(r.get("total_debt")),
                cash_m=money(r.get("cash_and_equivalents")),
                public_float_m=money(r.get("public_float")),
                ebit_m=money(r.get("ebit")),
                ebitda_m=money(r.get("ebitda")),
            )
        q = v.get("quote") or {}
        valuation = {
            "price": q.get("price"),
            "price_as_of": q.get("as_of"),
            "price_source": q.get("source"),
            "price_is_floor": bool(q.get("is_floor")),
            "price_is_live": bool(q.get("is_live")),
            "public_float_m": money(q.get("public_float")),
            "cover_shares": (v["shares"].get("cover_page") or {}).get("total"),
            "fully_diluted": v["shares"].get("fully_diluted"),
            "market_cap_m": money(v.get("market_cap")),
            "ev_m": money(v["bridge"].get("enterprise_value")),
            "ev_missing": v["bridge"].get("missing_components") or [],
            "ebit_m": money(v["operating"].get("ebit")),
            "ebitda_m": money(v["operating"].get("ebitda")),
            "warning": v.get("market_cap_warning"),
            **{k: v["multiples"].get(k) for k in
               ("ev_sales", "ev_ebit", "ev_ebitda", "pe", "fcf_yield_pct")},
        }
        # Flattened for the TTM block below, which merges it with trends.json.
        # Underscored because it is plumbing between two blocks here, not a
        # field the page reads off `valuation`.
        tt = v.get("ttm")
        if tt:
            valuation["_ttm"] = {
                "market_cap_m": money(tt.get("market_cap")),
                "ev_m": money(tt["bridge"].get("enterprise_value")),
                "shares": tt.get("shares"),
                "shares_basis": tt.get("shares_basis"),
                **{k: tt["multiples"].get(k) for k in (
                    "ev_sales", "ev_ebit", "ev_ebitda", "pe", "fcf_yield_pct")},
            }

    # Trailing twelve months — the 10-K year rolled forward with the 10-Qs filed
    # since. Additive: it sits beside the annual block on the page, never over
    # it, so the 10-K-only material (risk diff, share-comp footnote, audited
    # statements) is untouched. None when no 10-Q post-dates the filing.
    t = tj.get("ttm")
    ttm = None
    if t:
        tv = (valuation or {}).pop("_ttm", None) or {}
        vals, fy_vals, growth = t["values"], t["fiscal_year"], t["ytd_growth_pct"]
        ttm = {
            "quarter_end": t["quarter_end"],
            "quarter_filed": t["quarter_filed"],
            "fiscal_period": t["fiscal_period"],
            "basis": t["basis"],
            "days_past_year_end": t["days_stale_at_fy_end"],
            "weeks_ytd": t["weeks_year_to_date"],
            "prior_quarter_end": t["prior_quarter_end"],
            "not_tagged": t.get("not_tagged") or [],
            # Each line as (fiscal year, TTM, year-to-date growth) so the page
            # can show the gap rather than just the newer number.
            "rows": [
                {"label": label, "fy": fy_vals.get(k), "ttm": vals.get(k),
                 "growth_pct": growth.get(k), "money": money_row}
                for label, k, money_row in (
                    ("Revenue", "revenue", True),
                    ("Operating income", "operating_income", True),
                    ("Net income", "net_income", True),
                    ("EPS diluted", "eps_diluted", False),
                    ("Cash from ops", "operating_cash_flow", True),
                    ("Capex", "capex", True),
                    ("D&A", "d_and_a", True),
                )
            ],
            "fcf_m": money(t["derived"].get("free_cash_flow")),
            "ebit_m": money(t["derived"].get("ebit")),
            "ebitda_m": money(t["derived"].get("ebitda")),
            "cash_m": money(t["balance"].get("cash")),
            "total_debt_m": money(t["derived"].get("total_debt")),
            **{k: tv.get(k) for k in (
                "market_cap_m", "ev_m", "shares", "shares_basis",
                "ev_sales", "ev_ebit", "ev_ebitda", "pe", "fcf_yield_pct")},
        }

    # The most recent quarter and the balance sheet behind it. Both read the
    # 10-Qs and both stand on their own — a filer the TTM roll can't handle
    # (no comparable prior-year period) still has a latest balance sheet.
    mrq = tj.get("mrq")
    s = tj.get("snapshot")
    snapshot = None
    if s:
        sv, sh = s["values"], s["shares"]
        snapshot = {
            "as_of": s["as_of"],
            "form": s["form"],
            "filed": s["filed"],
            "cash_st_inv_m": money(s.get("cash_and_st_investments")),
            "total_debt_m": money(s.get("total_debt")),
            "net_cash_m": money(s.get("net_cash")),
            "total_assets_m": money(sv.get("total_assets")),
            "inventory_m": money(sv.get("inventory")),
            "total_equity_m": money(sv.get("total_equity")),
            "cover_shares": sh.get("cover_page"),
            "cover_shares_as_of": sh.get("cover_page_as_of"),
            "shares_diluted": sh.get("weighted_average_diluted"),
        }

    rel = out_dir.relative_to(ROOT).as_posix()
    files = []
    for p in sorted(out_dir.rglob("*")):
        if p.is_file():
            files.append({
                "name": p.relative_to(out_dir).as_posix(),
                "href": "/" + p.relative_to(ROOT).as_posix(),
                "kb": round(p.stat().st_size / 1024, 1),
            })

    return {
        "ticker": meta.get("ticker"),
        "company": meta.get("company"),
        "cik": meta.get("cik"),
        "sic": meta.get("sic_description"),
        "exchanges": meta.get("exchanges") or [],
        "auditor": meta.get("auditor_name"),
        "period": meta.get("period"),
        "filed": meta.get("filed"),
        "source_url": meta.get("source_url"),
        "dir": rel,
        # Everything that used to sit in `latest` is a column of `years` now —
        # the page reads whichever year its tab is on, and there is one
        # definition of each figure rather than one per view. What stays here is
        # what only the current filing has.
        "latest": {
            "membership_fee_m": money(
                tj.get("statement_lines", {}).get("membership_fee_income")),
            # The quarter comes off the latest 10-Q, falling back to a fourth
            # quarter out of the 10-K itself. `d["mrq_revenue"]` is the 10-K-only
            # figure and is blank for almost every modern filer.
            "mrq_revenue_m": money((mrq or {}).get("revenue", d.get("mrq_revenue"))),
            "mrq_revenue_prior_m": money(
                (mrq or {}).get("prior_revenue", d.get("mrq_revenue_prior"))),
            "mrq_quarter_end": (mrq or {}).get("quarter_end"),
            "mrq_prior_quarter_end": (mrq or {}).get("prior_quarter_end"),
            "mrq_growth_pct": (mrq or {}).get("growth_pct"),
            "mrq_basis": (mrq or {}).get("basis"),
            "mrq_source": (mrq or {}).get("source"),
        },
        # One column per fiscal year, in raw dollars and shares. The page
        # formats by the catalogue's `kind`.
        "fiscal_years": fyes,
        "years": year_columns(tj),
        "latest_column": latest_column(tj),
        "catalogue": catalogue_for(tj),
        "categories": CATEGORY_LABELS,
        "model_keys": MODEL_KEYS,
        # How total debt and EBIT were assembled, per year. Both vary by filer
        # and by year, and a debt figure whose basis says "check this one" is a
        # different number from one that doesn't.
        "bases": {
            y: {"debt": derived.get(y, {}).get("debt_basis"),
                "ebit": derived.get(y, {}).get("ebit_basis")}
            for y in fyes
        },
        "shares_rescaled": (series.get("shares_diluted") or {}).get("rescaled") or {},
        "statements": as_filed_statements(out_dir, set(fyes)),
        "xbrl": every_tagged(out_dir),
        "quarters_skipped": bool(tj.get("quarters_skipped")),
        # Stock items as of the newest filing — no LTM arithmetic, one date.
        "snapshot": snapshot,
        "valuation": valuation,
        "valuation_history": valuation_history,
        "ttm": ttm,
        "cagr": derived.get("cagr", {}),
        "risk": risk,
        # trends.json records these as {"metric":..., "tried":[concepts]};
        # the page only needs the metric name
        "missing": [
            m.get("metric") if isinstance(m, dict) else str(m)
            for m in tj.get("missing", [])
        ],
        # Every other 10-K of this company already on disk, so a year tab can
        # offer to open one rather than re-pull it.
        "on_disk": periods(meta.get("ticker") or ""),
        "files": files,
    }


def periods(ticker):
    """Every fiscal year end already extracted for a ticker, oldest first."""
    base = FILINGS / ticker.upper().replace(".", "-")
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def newest_dir(ticker):
    """Latest already-extracted filing for a ticker, if any."""
    found = periods(ticker)
    if not found:
        return None
    return FILINGS / ticker.upper().replace(".", "-") / found[-1]


def year_dir(ticker, year):
    """The extracted filing for one fiscal year, if it is already on disk."""
    base = FILINGS / ticker.upper().replace(".", "-")
    match = [p for p in periods(ticker) if p[:4] == str(year)]
    return (base / match[-1]) if match else None


def run_extractor(ticker, year=None, rebuild=False):
    """Shell out to the same script the CLI uses. Returns (out_dir, log).

    `rebuild` regenerates every output from the cached SEC responses and takes a
    fresh quote — no re-download. It is how the page re-prices, and how an
    extraction written before a schema change is brought up to date without
    asking SEC for anything it has already answered.
    """
    cmd = [sys.executable, "extract_10k.py", ticker]
    if year:
        cmd += ["--year", str(year)]
    if rebuild:
        cmd.append("--rebuild")
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT / "scripts"),
        capture_output=True, text=True, timeout=EXTRACT_TIMEOUT,
    )
    log = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(log.strip() or "extractor failed with no output")

    # a fresh run ends "Done: <dir>"; an already-extracted one short-circuits
    # with "Already extracted: <dir>" and never reaches the Done line
    m = re.search(r"^(?:Done|Already extracted): (.+)$", proc.stdout or "", re.M)
    fallback = year_dir(ticker, year) if year else newest_dir(ticker)
    out_dir = Path(m.group(1).strip()) if m else fallback
    if not out_dir or not Path(out_dir).is_dir():
        raise RuntimeError("extractor finished but no output directory was found")
    return Path(out_dir), log


# Written by every run since the year tabs landed. An extraction older than that
# short-circuits on "Already extracted" and hands the page a filing with no
# per-year data in it, which renders as a tab strip of blanks. Rebuilding costs
# no SEC request — the responses are already cached — so it is cheaper to do it
# than to explain it.
def needs_rebuild(out_dir):
    return not (Path(out_dir) / "xbrl_by_year.json").exists()


# Stdlib's default error page is unstyled black-on-white, which in an all-
# #222222 site reads as the browser breaking rather than the path being wrong.
# error_message_format is the handler's own hook, so this costs no new route.
# Every literal % is doubled — this string goes through %-formatting, and a
# bare "100%" in the CSS would raise instead of render.
ERROR_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(code)d — Filed</title>
<meta name="theme-color" content="#222222">
<link rel="icon" href="/assets/favicon-icarus.png" type="image/png">
<style>
@font-face{font-family:"Libre Caslon Display";src:url("/assets/fonts/libre-caslon-display.woff2") format("woff2");font-display:swap}
body{margin:0;min-height:100dvh;display:grid;align-content:center;
  padding:2rem clamp(1.15rem,4.5vw,3.25rem);background:#222;color:#B79C8D;
  font:15px/1.7 -apple-system,Helvetica,Arial,sans-serif}
h1{margin:0 0 .6rem;font-family:"Libre Caslon Display",Baskerville,serif;
  font-weight:400;font-size:clamp(2rem,5vw,3.4rem);letter-spacing:-.015em;color:#EFD5C8}
p{margin:0;max-width:52ch;font-size:.8125rem}
a{color:#EFD5C8;text-decoration:none;border-bottom:1px solid rgba(239,213,200,.15)}
a:hover{border-bottom-color:#EFD5C8}
.n{font-family:ui-monospace,Menlo,monospace;font-size:.75rem;letter-spacing:.16em;
  text-transform:uppercase;color:#A98D7D;margin:0 0 1rem}
</style></head><body>
<p class="n">%(code)d %(message)s</p>
<h1>Nothing at that path</h1>
<p>%(explain)s. Every other error on this server answers as JSON, so if you are
seeing this page you asked for a file that isn't here.
<a href="/">Back to the ticker box</a>.</p>
</body></html>
"""


class Handler(SimpleHTTPRequestHandler):
    error_message_format = ERROR_PAGE

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(SITE), **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers ---------------------------------------------------------
    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            self.path = "/" + HOME
            return SimpleHTTPRequestHandler.do_GET(self)

        if path == "/api/cached":
            out = []
            if FILINGS.is_dir():
                for t in sorted(p.name for p in FILINGS.iterdir() if p.is_dir()):
                    found = periods(t)
                    if found:
                        # Every period, not only the newest: COST has three on
                        # disk and the list used to admit to one, so the two
                        # older ones were unreachable from the page.
                        out.append({"ticker": t, "period": found[-1], "periods": found})
            return self.send_json({"cached": out})

        # the extracted filings themselves, so the file list is clickable
        if path.startswith("/filings/"):
            rel = unquote(path.lstrip("/"))
            target = (ROOT / rel).resolve()
            try:
                target.relative_to(FILINGS.resolve())
            except ValueError:
                return self.send_json({"error": "forbidden"}, 403)
            if not target.is_file():
                return self.send_json({"error": "not found"}, 404)
            data = target.read_bytes()
            ctype = "text/plain; charset=utf-8"
            disposition = None
            if target.suffix == ".json":
                ctype = "application/json; charset=utf-8"
            elif target.suffix == ".csv":
                # A spreadsheet, so hand it over as one: served as text/plain it
                # opens as a wall of commas in a browser tab instead of saving.
                ctype = "text/csv; charset=utf-8"
                disposition = 'attachment; filename="{}-{}"'.format(
                    target.parent.parent.name, target.name)
            elif target.suffix in (".htm", ".html"):
                # the raw filing is untrusted third-party HTML; hand it over as
                # text so the browser never executes anything inside it
                ctype = "text/plain; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        if urlparse(self.path).path != "/api/extract":
            return self.send_json({"error": "not found"}, 404)

        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                return self.send_json({"error": "request too large"}, 413)
            payload = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "bad request"}, 400)

        ticker = str(payload.get("ticker", "")).strip().upper()
        if not TICKER_RE.match(ticker):
            return self.send_json(
                {"error": "That doesn't look like a ticker. Try AAPL, BRK.B, JPM."}, 400)

        year = payload.get("year")
        if year not in (None, "", 0):
            try:
                year = int(year)
            except (TypeError, ValueError):
                return self.send_json({"error": "year must be a four-digit fiscal year"}, 400)
            if not 1993 <= year <= datetime.now().year + 1:
                return self.send_json(
                    {"error": "EDGAR's full-text filings start in 1993; {} is outside "
                              "it.".format(year)}, 400)
        else:
            year = None
        # Re-price: everything is rebuilt from the cached SEC responses and the
        # quote is taken fresh. The README has always said --refresh re-prices;
        # until now the page had no way to ask for it.
        rebuild = bool(payload.get("refresh"))

        started = datetime.now()
        cached_before = year_dir(ticker, year) if year else newest_dir(ticker)

        if not _extract_lock.acquire(blocking=False):
            return self.send_json(
                {"error": "Another extraction is already running. Give it a few seconds."},
                429)
        try:
            out_dir, log = run_extractor(ticker, year=year, rebuild=rebuild)
            if needs_rebuild(out_dir):
                # Extracted before the year tabs existed, so it has no per-year
                # file. Cached responses, so this asks SEC for nothing.
                sys.stderr.write("  {} predates the per-year outputs — rebuilding "
                                 "from cache\n".format(ticker))
                out_dir, log2 = run_extractor(ticker, year=year, rebuild=True)
                log += log2
            data = load_filing(out_dir)
        except subprocess.TimeoutExpired:
            return self.send_json(
                {"error": "SEC didn't answer in time. Try again in a moment."}, 504)
        except RuntimeError as e:
            msg = str(e)
            # the browser gets one line; the whole traceback goes to the log,
            # or a failure that isn't an ERROR: line is undiagnosable from the
            # 400 characters the page can show
            sys.stderr.write("  extractor failed for %s:\n%s\n" % (ticker, msg))
            tail = [ln for ln in msg.splitlines() if ln.strip().startswith("ERROR:")]
            return self.send_json({"error": (tail[-1][6:].strip() if tail else msg)[:400]}, 502)
        except Exception as e:  # noqa: BLE001 - surface the real cause, don't 500 blindly
            return self.send_json({"error": "{}: {}".format(type(e).__name__, e)[:400]}, 500)
        finally:
            _extract_lock.release()

        m = re.search(r"SEC requests: (\d+), cache hits: (\d+)", log)
        data["run"] = {
            "seconds": round((datetime.now() - started).total_seconds(), 1),
            "sec_requests": int(m.group(1)) if m else None,
            "cache_hits": int(m.group(2)) if m else None,
            "was_cached": bool(cached_before),
            "repriced": rebuild,
            "year_requested": year,
        }
        return self.send_json(data)


def demo():
    """python3 serve.py --self-check — offline, no server, no network."""
    import tempfile

    tj = {
        "fiscal_year_ends": ["2023-12-31", "2024-12-31"],
        "series": {
            "revenue": {"concepts": ["Revenues"],
                        "values": {"2023-12-31": 100e6, "2024-12-31": 120e6}},
            "cash": {"concepts": ["CashAndCashEquivalentsAtCarryingValue"],
                     "values": {"2023-12-31": 5e6, "2024-12-31": 6e6}},
            "shares_diluted": {"concepts": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
                               "values": {"2023-12-31": 10e6, "2024-12-31": 40e6},
                               "rescaled": {"2024-12-31": 1e6}},
        },
        "derived": {"2023-12-31": {"net_margin_pct": 10.0, "total_debt": 20e6,
                                   "debt_basis": "noncurrent + current"},
                    "2024-12-31": {"net_margin_pct": 12.0, "total_debt": 22e6,
                                   "debt_basis": "noncurrent + current"},
                    "cagr": {}},
        "ttm": {"quarter_end": "2025-06-30", "values": {"revenue": 130e6},
                "derived": {"net_margin_pct": 13.0, "total_debt": 25e6},
                "balance": {}, "not_tagged": ["d_and_a"]},
        "snapshot": {"as_of": "2025-06-30", "form": "10-Q",
                     "values": {"cash": 7e6, "inventory": 3e6},
                     "total_debt": 25e6, "cash_and_st_investments": 7e6, "net_cash": -18e6},
    }

    cols = year_columns(tj)
    assert list(cols) == ["2023-12-31", "2024-12-31"], list(cols)
    assert cols["2024-12-31"]["revenue"] == 120e6
    assert cols["2024-12-31"]["net_margin_pct"] == 12.0, "derived keys must reach the column"
    assert cols["2023-12-31"]["inventory"] is None, "a blank stays blank"

    latest = latest_column(tj)
    assert latest["values"]["revenue"] == 130e6, "a flow must come off the TTM roll"
    assert latest["values"]["cash"] == 7e6, "a stock must come off the balance sheet"
    assert latest["values"]["total_debt"] == 25e6
    assert "LTM to 2025-06-30" in latest["label"] and "10-Q balance sheet" in latest["label"]

    # A filer whose roll-forward has no comparable period still has a balance
    # sheet, and the two are independent upstream — they must stay independent
    # here, or a bank loses its debt and cash along with its revenue.
    no_ttm = latest_column(dict(tj, ttm=None))
    assert no_ttm["values"]["cash"] == 7e6 and no_ttm["values"]["revenue"] is None
    assert no_ttm["has_flows"] is False
    assert latest_column({"ttm": None, "snapshot": None}) is None

    cat = {c["key"]: c for c in catalogue_for(tj)}
    assert "revenues" in cat["revenue"]["terms"], "the XBRL concept must be searchable"
    assert "cfo" in cat["operating_cash_flow"]["terms"], "the alias must be searchable"
    assert cat["total_debt"]["source"] == "computed"

    # Only annual columns map to a year. A quarter rendered beside the year is
    # the trap that once returned a quarter of membership fees as a year of them.
    years = {"2024-12-31", "2023-12-31"}
    assert _column_year("Dec. 31, 2024", years) == "2024-12-31"
    assert _column_year("Dec. 31, 2024 (12 Months Ended)", years) == "2024-12-31"
    assert _column_year("Dec. 31, 2024 (3 Months Ended)", years) is None
    assert _column_year("Dec. 31, 2019", years) is None
    assert _column_year("Line item", years) is None

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        (out / "statements").mkdir()
        (out / "statements" / "income_statement.json").write_text(json.dumps({
            "title": "INCOME", "units": "USD ($) $ in Millions",
            "columns": ["Line item", "Dec. 31, 2024", "Dec. 31, 2023"],
            "rows": [["Revenue", "$ 120", "$ 100"]]}))
        (out / "xbrl_by_year.json").write_text(json.dumps({
            "fiscal_year_ends": ["2024-12-31"],
            "concepts": {"AccountsReceivableNetCurrent": {
                "unit": "USD", "type": "instant", "values": {"2024-12-31": 9e6}}}}))
        st = as_filed_statements(out, years)
        assert st[0]["column_years"] == [None, "2024-12-31", "2023-12-31"], st[0]["column_years"]
        assert st[0]["rows"][0][1] == "$ 120", "cells must stay verbatim"
        tags = every_tagged(out)
        assert tags[0]["label"] == "Accounts Receivable Net Current", tags[0]["label"]
        assert needs_rebuild(out) is False
        (out / "xbrl_by_year.json").unlink()
        assert needs_rebuild(out) is True

    print("ok: serve — year columns, flow/stock split, catalogue search terms, "
          "as-filed column dates, rebuild detection")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=4321)
    p.add_argument("--self-check", action="store_true",
                   help="run the offline checks and exit")
    args = p.parse_args()

    if args.self_check:
        return demo()

    if not SITE.is_dir():
        sys.exit("site/ not found next to serve.py")

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Filed  ->  http://localhost:{}".format(args.port))
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
