#!/usr/bin/env python3
"""Multi-year financials from XBRL companyfacts — one free API call.

The single 10-K gives you three years. This gives you ten, plus the derived
ratios, from the numbers the company actually tagged.

Two things learned the hard way:

  - Don't use the `frame` field. Duration facts get frames like "CY2024" but
    instant facts (Assets, equity, cash) get "CY2024Q3I" keyed to *calendar*
    quarters, so a filer with a September year end has no frame on its
    fiscal-year-end balance sheet at all. Filtering on frames silently returns
    an empty series for every balance sheet line.
  - Filter to 10-K rows, take durations of 340-400 days, and key everything by
    the period end date. Then pull instant facts at exactly those same dates.
    Works on any fiscal calendar.

Restatements mean the same period appears more than once; the row with the
latest `filed` date wins.
"""

from datetime import date

# Concept name varies by filer, so each line item is a priority list: first one
# present wins. A missing line item is reported as missing, never substituted.
DURATION_CONCEPTS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "rnd_expense": ["ResearchAndDevelopmentExpense"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps_diluted": [
        "EarningsPerShareDiluted",
        "EarningsPerShareBasicAndDiluted",
        "IncomeLossFromContinuingOperationsPerDilutedShare",
    ],
    "shares_diluted": [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasicAndDiluted",
    ],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
}

INSTANT_CONCEPTS = {
    "total_assets": ["Assets"],
    "total_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "short_term_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent"],
    "inventory": ["InventoryNet"],
    "long_term_debt": [
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
        "DebtLongtermAndShorttermCombinedAmount",
    ],
    "current_debt": ["LongTermDebtCurrent"],
    "total_liabilities": ["Liabilities"],
}

MIN_DAYS, MAX_DAYS = 340, 400  # what counts as an annual period


def _units(concept_data):
    """Pick the unit series — USD when present, else whatever the concept uses
    (shares, USD/share)."""
    units = concept_data.get("units", {})
    if not units:
        return []
    for key in ("USD", "shares", "USD/shares"):
        if key in units:
            return units[key]
    return next(iter(units.values()))


def _latest_filed(rows):
    """Collapse {end_date: [rows]} keeping the most recently filed value."""
    best = {}
    for row in rows:
        prev = best.get(row["end"])
        if prev is None or row["filed"] > prev["filed"]:
            best[row["end"]] = row
    return {k: v["val"] for k, v in sorted(best.items())}


def _merge(facts, concepts, keep):
    """Walk the priority list and merge, first concept winning per date.

    Merging rather than first-match-wins matters across accounting-standard
    changes: Apple tags revenue as RevenueFromContractWithCustomer... only from
    fiscal 2018 (ASC 606) and as SalesRevenueNet before that. Taking just the
    first concept with any data leaves the early years blank.
    """
    used, values = [], {}
    for concept in concepts:
        rows = [row for row in _units(facts.get(concept, {})) if keep(row)]
        if not rows:
            continue
        used.append(concept)
        for end, val in _latest_filed(rows).items():
            values.setdefault(end, val)
    return used, dict(sorted(values.items()))


def duration_series(facts, concepts):
    """Annual (period) values keyed by period end date."""
    def keep(row):
        if not row.get("form", "").startswith("10-K") or "start" not in row:
            return False
        days = (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days
        return MIN_DAYS <= days <= MAX_DAYS

    return _merge(facts, concepts, keep)


def instant_series(facts, concepts, fiscal_year_ends):
    """Point-in-time (balance sheet) values at the given fiscal year ends."""
    return _merge(
        facts, concepts, lambda row: "start" not in row and row["end"] in fiscal_year_ends
    )


def _pct(new, old):
    if new is None or old is None or old == 0:
        return None
    return round((new - old) / abs(old) * 100, 1)


def _cagr(series, years):
    """Compound annual growth over `years`, as a percent."""
    dates = sorted(series)
    if len(dates) <= years:
        return None
    end, start = series[dates[-1]], series[dates[-1 - years]]
    if start is None or start <= 0 or end is None or end <= 0:
        return None
    return round(((end / start) ** (1.0 / years) - 1) * 100, 1)


def _ratio(num, den, pct=True):
    if num is None or den is None or den == 0:
        return None
    return round(num / den * (100 if pct else 1), 1 if pct else 2)


def build(companyfacts, max_years=10, as_of=None):
    """companyfacts JSON -> {years, series, derived, missing}.

    `as_of` caps the history at a fiscal year end, so pulling an old 10-K gives
    the history as it stood then rather than years the filing predates.
    """
    facts = companyfacts.get("facts", {}).get("us-gaap", {})
    series, missing = {}, []

    for name, concepts in DURATION_CONCEPTS.items():
        used, values = duration_series(facts, concepts)
        if not used:
            missing.append({"metric": name, "tried": concepts})
        series[name] = {"concepts": used, "values": values}

    # Fiscal year ends come from the revenue series (or whatever duration
    # series is longest) — these are the dates the balance sheet is stamped at.
    anchor = max((s["values"] for s in series.values()), key=len, default={})
    fiscal_year_ends = set(anchor)

    for name, concepts in INSTANT_CONCEPTS.items():
        used, values = instant_series(facts, concepts, fiscal_year_ends)
        if not used:
            missing.append({"metric": name, "tried": concepts})
        series[name] = {"concepts": used, "values": values}

    years = sorted(y for y in fiscal_year_ends if not as_of or y <= as_of)[-max_years:]
    for entry in series.values():
        entry["values"] = {y: entry["values"].get(y) for y in years}

    return {
        "fiscal_year_ends": years,
        "series": series,
        "derived": derive(series, years),
        "missing": missing,
    }


def derive(series, years):
    """Ratios computed from the tagged values above. Every one of these is
    calculated here, not reported by the company."""
    def val(name, year):
        return series[name]["values"].get(year)

    out = {}
    for i, year in enumerate(years):
        prev = years[i - 1] if i else None
        rev, ni = val("revenue", year), val("net_income", year)
        ocf, capex = val("operating_cash_flow", year), val("capex", year)
        fcf = ocf - capex if ocf is not None and capex is not None else None
        gross = val("gross_profit", year)
        if gross is None and rev is not None and val("cost_of_revenue", year) is not None:
            gross = rev - val("cost_of_revenue", year)
        debt = sum(
            v for v in (val("long_term_debt", year), val("current_debt", year)) if v is not None
        ) or None
        liquid = sum(
            v
            for v in (val("cash", year), val("short_term_investments", year))
            if v is not None
        ) or None

        out[year] = {
            "revenue_growth_pct": _pct(rev, val("revenue", prev)) if prev else None,
            "net_income_growth_pct": _pct(ni, val("net_income", prev)) if prev else None,
            "eps_growth_pct": _pct(val("eps_diluted", year), val("eps_diluted", prev))
            if prev
            else None,
            "gross_margin_pct": _ratio(gross, rev),
            "operating_margin_pct": _ratio(val("operating_income", year), rev),
            "net_margin_pct": _ratio(ni, rev),
            "free_cash_flow": fcf,
            "fcf_margin_pct": _ratio(fcf, rev),
            "roe_pct": _ratio(ni, val("total_equity", year)),
            "roa_pct": _ratio(ni, val("total_assets", year)),
            "total_debt": debt,
            "net_cash": (liquid - debt) if liquid is not None and debt is not None else None,
            "debt_to_equity": _ratio(debt, val("total_equity", year), pct=False),
            "shares_change_pct": _pct(val("shares_diluted", year), val("shares_diluted", prev))
            if prev
            else None,
        }

    out["cagr"] = {
        "revenue_3y_pct": _cagr(series["revenue"]["values"], 3),
        "revenue_5y_pct": _cagr(series["revenue"]["values"], 5),
        "net_income_5y_pct": _cagr(series["net_income"]["values"], 5),
        "eps_5y_pct": _cagr(series["eps_diluted"]["values"], 5),
    }
    return out


# --- rendering -----------------------------------------------------------

MILLIONS = {
    "revenue", "cost_of_revenue", "gross_profit", "rnd_expense", "operating_income",
    "net_income", "operating_cash_flow", "capex", "buybacks", "dividends_paid",
    "total_assets", "total_equity", "cash", "short_term_investments", "inventory",
    "long_term_debt", "current_debt", "total_liabilities",
}
ROW_LABELS = [
    ("revenue", "Revenue"), ("gross_profit", "Gross profit"),
    ("operating_income", "Operating income"), ("net_income", "Net income"),
    ("eps_diluted", "EPS (diluted)"), ("shares_diluted", "Diluted shares"),
    ("operating_cash_flow", "Operating cash flow"), ("capex", "Capex"),
    ("total_assets", "Total assets"), ("total_equity", "Total equity"),
    ("cash", "Cash"), ("long_term_debt", "Long-term debt"),
    ("buybacks", "Buybacks"), ("dividends_paid", "Dividends paid"),
]
DERIVED_LABELS = [
    ("revenue_growth_pct", "Revenue growth %"), ("eps_growth_pct", "EPS growth %"),
    ("gross_margin_pct", "Gross margin %"), ("operating_margin_pct", "Operating margin %"),
    ("net_margin_pct", "Net margin %"), ("free_cash_flow", "Free cash flow"),
    ("fcf_margin_pct", "FCF margin %"), ("roe_pct", "ROE %"), ("roa_pct", "ROA %"),
    ("debt_to_equity", "Debt / equity"), ("shares_change_pct", "Share count change %"),
]


def _fmt(value, kind):
    """kind: 'millions' | 'pct' | 'decimal' | 'whole'"""
    if value is None:
        return "—"
    if kind == "millions":
        return "{:,.0f}".format(value / 1e6)
    if kind == "pct":
        return "{:,.1f}".format(value)
    if kind == "decimal":
        return "{:,.2f}".format(value)
    return "{:,.0f}".format(value)


def to_markdown(data, company):
    years = data["fiscal_year_ends"]
    head = "| Metric | " + " | ".join(years) + " |"
    rule = "|" + "|".join(["---"] * (len(years) + 1)) + "|"
    lines = [
        "# {} — {}-year financial trends".format(company, len(years)),
        "",
        "Source: SEC XBRL companyfacts (the company's own tagged numbers, as filed).",
        "Dollar figures in millions. Share counts as reported.",
        "",
        "> Caveat on old years: each figure is the most recently *filed* value for that period, "
        "which means recent years reflect restatements and splits but years far enough back that "
        "no later filing restated them sit on the old basis. A stock split therefore shows up as a "
        "step change in the EPS and share-count rows, not as an error.",
        "",
        "## As reported",
        "",
        head,
        rule,
    ]
    for key, label in ROW_LABELS:
        entry = data["series"].get(key, {})
        if not entry.get("concepts"):
            continue
        kind = "millions" if key in MILLIONS else ("decimal" if key == "eps_diluted" else "whole")
        cells = [_fmt(entry["values"].get(y), kind) for y in years]
        lines.append("| {} | {} |".format(label, " | ".join(cells)))

    lines += ["", "## Derived (computed here, not reported)", "", head, rule]
    for key, label in DERIVED_LABELS:
        kind = "millions" if key == "free_cash_flow" else ("pct" if key.endswith("_pct") else "decimal")
        cells = [_fmt(data["derived"].get(y, {}).get(key), kind) for y in years]
        lines.append("| {} | {} |".format(label, " | ".join(cells)))

    cagr = data["derived"]["cagr"]
    lines += ["", "## Compound growth", ""]
    for key, label in [
        ("revenue_3y_pct", "Revenue 3-yr CAGR"), ("revenue_5y_pct", "Revenue 5-yr CAGR"),
        ("net_income_5y_pct", "Net income 5-yr CAGR"), ("eps_5y_pct", "EPS 5-yr CAGR"),
    ]:
        v = cagr.get(key)
        lines.append("- {}: {}".format(label, "—" if v is None else "{}%".format(v)))

    lines += ["", "## XBRL concepts behind each row", ""]
    for key, entry in data["series"].items():
        if entry.get("concepts"):
            lines.append("- `{}` — {}".format(key, ", ".join(entry["concepts"])))

    if data["missing"]:
        lines += ["", "## Not tagged by this filer", ""]
        for m in data["missing"]:
            lines.append("- `{}` — tried: {}".format(m["metric"], ", ".join(m["tried"])))
    return "\n".join(lines) + "\n"


def demo():
    """python3 trends.py — offline checks on synthetic facts."""
    def dur(concept, vals):
        return {
            concept: {
                "units": {
                    "USD": [
                        {
                            "start": "{}-01-01".format(int(y[:4])),
                            "end": y,
                            "val": v,
                            "form": "10-K",
                            "filed": "{}-02-01".format(int(y[:4]) + 1),
                        }
                        for y, v in vals.items()
                    ]
                }
            }
        }

    facts = {"facts": {"us-gaap": {}}}
    g = facts["facts"]["us-gaap"]
    # Revenue split across two concepts, as happens at an accounting-standard
    # change: the preferred one covers only the recent year.
    g.update(dur("RevenueFromContractWithCustomerExcludingAssessedTax", {"2024-12-31": 125e6}))
    g.update(dur("Revenues", {"2023-12-31": 100e6, "2024-12-31": 999e6}))
    g.update(dur("NetIncomeLoss", {"2023-12-31": 10e6, "2024-12-31": 25e6}))
    # Instant facts have no `start`, and their `end` matches the fiscal year end.
    g["Assets"] = {
        "units": {
            "USD": [
                {"end": "2024-12-31", "val": 500e6, "form": "10-K", "filed": "2025-02-01"},
                # A stale value for the same date, filed earlier — must lose.
                {"end": "2024-12-31", "val": 111e6, "form": "10-K", "filed": "2024-02-01"},
                # A quarter end that is not a fiscal year end — must be ignored.
                {"end": "2024-06-30", "val": 999e6, "form": "10-Q", "filed": "2024-07-01"},
            ]
        }
    }
    g["StockholdersEquity"] = {
        "units": {"USD": [{"end": "2024-12-31", "val": 200e6, "form": "10-K", "filed": "2025-02-01"}]}
    }

    d = build(facts)
    assert d["fiscal_year_ends"] == ["2023-12-31", "2024-12-31"], d["fiscal_year_ends"]
    # Both revenue concepts merge; the higher-priority one wins the year they overlap.
    assert d["series"]["revenue"]["concepts"] == [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
    ], d["series"]["revenue"]["concepts"]
    assert d["series"]["revenue"]["values"]["2024-12-31"] == 125e6, "priority order lost"
    assert d["series"]["revenue"]["values"]["2023-12-31"] == 100e6, "older concept not merged in"
    assert d["series"]["total_assets"]["values"]["2024-12-31"] == 500e6, "restatement pick failed"
    assert d["series"]["total_assets"]["values"]["2023-12-31"] is None
    assert d["derived"]["2024-12-31"]["revenue_growth_pct"] == 25.0
    assert d["derived"]["2024-12-31"]["net_margin_pct"] == 20.0
    assert d["derived"]["2024-12-31"]["roe_pct"] == 12.5
    assert d["derived"]["2024-12-31"]["roa_pct"] == 5.0
    # Not tagged at all -> reported missing, never silently substituted.
    assert any(m["metric"] == "operating_cash_flow" for m in d["missing"]), d["missing"]
    assert d["series"]["capex"]["concepts"] == []
    md = to_markdown(d, "Test Co")
    assert "Revenue growth %" in md and "| 25.0 |" in md

    # as_of caps history at the filing being extracted, so pulling an old 10-K
    # doesn't show years that filing predates.
    old = build(facts, as_of="2023-12-31")
    assert old["fiscal_year_ends"] == ["2023-12-31"], old["fiscal_year_ends"]
    print("ok: trends — duration/instant split, restatement pick, fallbacks, derived ratios")


if __name__ == "__main__":
    demo()
