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

The same call also carries every 10-Q the company has filed, which is what
`ttm()` at the bottom uses to roll the newest fiscal year forward to the most
recent quarter. Free — the facts are already in hand.
"""

import math
from datetime import date, timedelta

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
    "d_and_a": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "Depreciation",
    ],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    # Expense only. InterestIncomeExpenseNet is *net* interest income for a bank
    # — adding that to pretax income to reach EBIT gets the sign backwards.
    "interest_expense": [
        "InterestExpense",
        "InterestExpenseNonoperating",
        "InterestAndDebtExpense",
        "InterestExpenseDebt",
    ],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    # --- the lines a model asks for that the first pass didn't carry -------
    # Every one of these is in the same companyfacts response as the rows
    # above, so none of them costs a request. They are here because a forecast
    # is built out of them: you cannot get to unlevered free cash flow without
    # a tax rate, or to a working-capital schedule without receivables and
    # payables, or judge earnings quality without stock comp.
    "income_tax_expense": ["IncomeTaxExpenseBenefit"],
    "sga_expense": [
        "SellingGeneralAndAdministrativeExpense",
        "GeneralAndAdministrativeExpense",
    ],
    "selling_marketing_expense": ["SellingAndMarketingExpense"],
    # The cash flow statement's own add-back, which is the one that belongs in
    # a cash-flow bridge. The footnote's expense figure is the same number for
    # most filers and a different one for a few.
    "stock_comp": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "eps_basic": ["EarningsPerShareBasic"],
    "shares_basic": ["WeightedAverageNumberOfSharesOutstandingBasic"],
    "dividends_per_share": [
        "CommonStockDividendsPerShareDeclared",
        "CommonStockDividendsPerShareCashPaid",
    ],
    "acquisitions": ["PaymentsToAcquireBusinessesNetOfCashAcquired"],
    "interest_income": ["InvestmentIncomeInterest", "InterestAndDividendIncomeOperating"],
}

INSTANT_CONCEPTS = {
    "total_assets": ["Assets"],
    "total_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    # The second concept folds in restricted cash. It's the only one banks and
    # Berkshire tag, and `trends.md` prints which concept fed the row.
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "short_term_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent"],
    "inventory": ["InventoryNet"],
    # Debt is split across five entries rather than one priority list because
    # the concepts overlap: LongTermDebt *includes* current maturities while
    # LongTermDebtNoncurrent excludes them, and DebtCurrent already contains
    # commercial paper. Merging them into one list double-counts. `_total_debt`
    # below picks a non-overlapping combination and records which one it used.
    "long_term_debt": ["LongTermDebtNoncurrent"],
    # Named so there is no doubt they include the current portion.
    "debt_incl_current": [
        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
        "DebtLongtermAndShorttermCombinedAmount",
    ],
    # `LongTermDebt` is genuinely ambiguous in practice — Apple tags it as
    # noncurrent + current maturities, Tesla tags it as noncurrent only. See
    # _total_debt.
    "long_term_debt_ambiguous": ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"],
    "current_debt": ["LongTermDebtCurrent"],
    "debt_current_total": ["DebtCurrent"],
    "short_term_borrowings": ["ShortTermBorrowings", "OtherShortTermBorrowings"],
    "commercial_paper": ["CommercialPaper"],
    "total_liabilities": ["Liabilities"],
    # The rest of the EV bridge.
    "preferred_stock": ["PreferredStockValue"],
    "preferred_liquidation": ["PreferredStockLiquidationPreferenceValue"],
    "minority_interest": ["MinorityInterest"],
    "finance_lease_liability": ["FinanceLeaseLiability"],
    "operating_lease_liability": ["OperatingLeaseLiability"],
    # --- the balance sheet a model actually schedules ----------------------
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "accounts_receivable": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],
    "accounts_payable": [
        "AccountsPayableCurrent",
        "AccountsPayableAndAccruedLiabilitiesCurrent",
    ],
    "ppe_net": ["PropertyPlantAndEquipmentNet"],
    "goodwill": ["Goodwill"],
    "intangibles": [
        "IntangibleAssetsNetExcludingGoodwill",
        "FiniteLivedIntangibleAssetsNet",
    ],
    "deferred_revenue": ["ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
    "long_term_investments": ["LongTermInvestments", "MarketableSecuritiesNoncurrent"],
}

MIN_DAYS, MAX_DAYS = 340, 400  # what counts as an annual period

# What counts as a quarter, for the most-recent-quarter revenue rows. 13 weeks
# is 91 days; Costco's fourth quarter is 16 or 17 weeks (112 or 119).
QUARTER_MIN_DAYS, QUARTER_MAX_DAYS = 60, 125

# Quarterly revenue, if the filer put any in its 10-K. Most no longer do: the
# SEC dropped the selected-quarterly-financial-data requirement in 2021, and
# none of AAPL, NVDA, TSLA, JPM, KO, MCD, COST or BRK tag a quarterly duration
# in a recent 10-K. Older filings do — Costco's 2017 10-K tags its Q4 — and a
# 10-Q would have it, but a 10-Q is not a 10-K and this tool reads 10-Ks. So
# the row is usually blank, and blank is the honest answer rather than a
# quarter reconstructed from somewhere else.
QUARTERLY_CONCEPTS = {"mrq_revenue": DURATION_CONCEPTS["revenue"]}

# Alternate routes to total debt rather than metrics in their own right. Apple
# tags no DebtCurrent because it tags the pieces instead — reporting that as a
# missing metric would be noise, not information.
ALTERNATE_LOOKUPS = {
    "debt_incl_current", "long_term_debt_ambiguous", "debt_current_total",
    "short_term_borrowings", "commercial_paper", "preferred_liquidation",
}

# Lines a company can legitimately not have, as opposed to lines it failed to
# tag. Apple has no goodwill worth speaking of and Tesla pays no dividend;
# listing those under "not tagged by this filer" is noise that buries the ones
# that matter. They still render as a blank wherever they appear — the blank is
# just not called out as an omission.
OPTIONAL_LINES = {
    "goodwill", "intangibles", "deferred_revenue", "interest_income",
    "long_term_investments", "acquisitions", "dividends_per_share",
    "selling_marketing_expense", "dividends_paid", "buybacks",
    "preferred_stock", "minority_interest", "finance_lease_liability",
    "operating_lease_liability",
}


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


def quarter_series(facts, concepts, fiscal_year_ends):
    """Quarter-length values whose period ends on a fiscal year end — the
    fourth quarter, when the filer discloses one."""
    def keep(row):
        if not row.get("form", "").startswith("10-K") or "start" not in row:
            return False
        if row["end"] not in fiscal_year_ends:
            return False
        days = (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days
        return QUARTER_MIN_DAYS <= days <= QUARTER_MAX_DAYS

    return _merge(facts, concepts, keep)


def instant_series(facts, concepts, fiscal_year_ends):
    """Point-in-time (balance sheet) values at the given fiscal year ends."""
    return _merge(
        facts, concepts, lambda row: "start" not in row and row["end"] in fiscal_year_ends
    )


def _unit_name(concept_data):
    """The unit key `_units` picked for this concept — USD, shares, USD/shares."""
    units = concept_data.get("units", {})
    for key in ("USD", "shares", "USD/shares"):
        if key in units:
            return key
    return next(iter(units), None)


def tagged_by_year(facts, years):
    """Every us-gaap concept this filer tagged, valued at each fiscal year end.

    The curated series above are the ~34 lines a model asks for by name. This is
    the rest of the filing — goodwill, receivables, deferred revenue, every lease
    and tax line, whatever this particular filer happens to tag. It is what lets
    a search box answer a question the catalogue never anticipated, and it costs
    no extra request: companyfacts is already in hand.

    Same filters as the curated series, deliberately, so the same concept read
    through either route is the same number: annual 10-K durations of 340-400
    days, instants at exactly those period ends, restatements resolved by taking
    the most recently filed row.
    """
    wanted = set(years)
    out = {}
    for concept, data in sorted(facts.items()):
        rows = _units(data)
        if not rows:
            continue
        duration = [
            r for r in rows
            if "start" in r and r.get("form", "").startswith("10-K")
            and r["end"] in wanted and MIN_DAYS <= _duration_days(r) <= MAX_DAYS
        ]
        # No form filter on instants, matching instant_series: a year-end balance
        # sheet is restated as the comparative column of later filings and the
        # newest of those is the value the curated series uses.
        instant = [r for r in rows if "start" not in r and r["end"] in wanted]
        kept, kind = (duration, "duration") if duration else (instant, "instant")
        if not kept:
            continue
        out[concept] = {
            "unit": _unit_name(data),
            "type": kind,
            "values": _latest_filed(kept),
        }
    return out


def filing_accessions(facts, years):
    """-> {fiscal year end: the accession of that year's *own* 10-K}.

    Earliest-filed wins, which is the opposite of the rule everywhere else in
    this file. Everywhere else the question is "what is this figure now", and
    the newest restatement is the answer. Here the question is "which document
    is this", and a 10-K restates the two years before it as comparative
    columns — so the newest row for FY2019 belongs to the FY2021 10-K. The
    public float and share count on a cover page belong to the filing that
    printed them, and pairing FY2019's numbers with FY2021's cover page prices
    the wrong year.

    Exact form "10-K": a 10-K/A amends a fragment and its cover page restates
    nothing that matters here.
    """
    wanted = set(years)
    best = {}
    for data in facts.values():
        for row in _units(data):
            if row.get("form") != "10-K" or "start" not in row or row["end"] not in wanted:
                continue
            if not (MIN_DAYS <= _duration_days(row) <= MAX_DAYS) or not row.get("accn"):
                continue
            prev = best.get(row["end"])
            if prev is None or row["filed"] < prev[0]:
                best[row["end"]] = (row["filed"], row["accn"])
    return {year: accn for year, (_, accn) in sorted(best.items())}


# A filer can change the scale it tags a share count at partway through its own
# history. McDonald's tags WeightedAverageNumberOfDilutedSharesOutstanding as
# 750,100,000 through fiscal 2020 and as 716.4 from 2021 — the same figure, in
# millions. Left alone that renders as 0.0M shares, a -100% share-count change
# in 2021 and a +100,000% one in 2022, and every per-share figure built on it is
# out by a factor of a million.
#
# The cross-check is the filer's own arithmetic: diluted EPS is net income over
# the diluted count, so net income / EPS is what the count has to be. That is
# also what makes this safe on a reverse split, where the count really does fall
# by a large factor — EPS moves with it, the implied count moves too, and
# nothing is rescaled. Only a units error puts a power of ten between them.
SCALE_BREAK = 100      # 100x inside one year is not a real move; 1000x never is


def _rescale_shares(series):
    """Put the diluted share count on one scale. -> {year: factor applied}."""
    entry = series.get("shares_diluted") or {}
    values = entry.get("values") or {}
    income = series.get("net_income", {}).get("values", {})
    per_share = series.get("eps_diluted", {}).get("values", {})
    applied = {}
    for year, count in list(values.items()):
        ni, eps = income.get(year), per_share.get(year)
        if not count or not ni or not eps:
            continue
        implied = ni / eps
        ratio = implied / count
        if ratio < SCALE_BREAK:
            continue
        factor = 10 ** round(math.log10(ratio))
        # Only a clean power of ten is a units error. Anything else is data this
        # routine doesn't understand, and leaving it alone is the honest answer.
        if abs(implied / (count * factor) - 1) > 0.05:
            continue
        values[year] = count * factor
        applied[year] = factor
    if applied:
        entry["rescaled"] = dict(sorted(applied.items()))
    return applied


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


def build(companyfacts, max_years=10, as_of=None, with_ttm=True):
    """companyfacts JSON -> {years, series, derived, ttm, missing}.

    `as_of` caps the history at a fiscal year end, so pulling an old 10-K gives
    the history as it stood then rather than years the filing predates.

    `ttm` rolls the newest of those years forward with whatever 10-Qs have been
    filed since — see the trailing-twelve-months block below. It is None when no
    10-Q post-dates the 10-K, and it never replaces an annual figure.
    """
    facts = companyfacts.get("facts", {}).get("us-gaap", {})
    series, missing = {}, []

    for name, concepts in DURATION_CONCEPTS.items():
        used, values = duration_series(facts, concepts)
        if not used and name not in ALTERNATE_LOOKUPS | OPTIONAL_LINES:
            missing.append({"metric": name, "tried": concepts})
        series[name] = {"concepts": used, "values": values}

    # Before anything derived is computed off them: a share count tagged in
    # millions for some years and raw for others makes every ratio touching it
    # wrong, and the fix has to land before derive() reads the series.
    _rescale_shares(series)

    # Fiscal year ends come from the revenue series (or whatever duration
    # series is longest) — these are the dates the balance sheet is stamped at.
    anchor = max((s["values"] for s in series.values()), key=len, default={})
    fiscal_year_ends = set(anchor)

    for name, concepts in INSTANT_CONCEPTS.items():
        used, values = instant_series(facts, concepts, fiscal_year_ends)
        if not used and name not in ALTERNATE_LOOKUPS | OPTIONAL_LINES:
            missing.append({"metric": name, "tried": concepts})
        series[name] = {"concepts": used, "values": values}

    for name, concepts in QUARTERLY_CONCEPTS.items():
        used, values = quarter_series(facts, concepts, fiscal_year_ends)
        if not used:
            missing.append({
                "metric": name,
                "tried": concepts,
                "note": "no quarterly period is tagged in this filer's 10-K. The SEC dropped "
                        "the selected-quarterly-data requirement in 2021, so most 10-Ks now "
                        "report the year only — the quarter is in the 10-Q, which this tool "
                        "does not read.",
            })
        series[name] = {"concepts": used, "values": values}

    years = sorted(y for y in fiscal_year_ends if not as_of or y <= as_of)[-max_years:]
    for entry in series.values():
        entry["values"] = {y: entry["values"].get(y) for y in years}

    return {
        "fiscal_year_ends": years,
        "series": series,
        "derived": derive(series, years),
        "ttm": ttm(facts, series, years[-1]) if with_ttm and years else None,
        # Both read the 10-Qs, and both are independent of the TTM roll: a
        # missing prior-year comparable kills the roll-forward but says nothing
        # about the latest quarter's revenue or its balance sheet.
        "mrq": mrq(facts, series, years) if with_ttm and years else None,
        "snapshot": snapshot(companyfacts, years[-1]) if with_ttm and years else None,
        # All three of the above read the 10-Qs, so --no-ttm drops all three.
        # Recorded, because "you told me not to look" and "the filer doesn't tag
        # it" produce the same blank and are not the same statement.
        "quarters_skipped": not with_ttm,
        "missing": missing,
    }


def _add(*values):
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _current_debt(val, year):
    """The short-term half of total debt, without double-counting it.

    DebtCurrent is the whole current bucket already — commercial paper and
    current maturities are inside it, so the pieces must not be added back.
    """
    total = val("debt_current_total", year)
    if total is not None:
        return total, "DebtCurrent (commercial paper and current maturities are already in it)"
    return (
        _add(val("short_term_borrowings", year), val("commercial_paper", year),
             val("current_debt", year)),
        "short-term borrowings + commercial paper + current maturities",
    )


def _total_debt(val, year):
    """-> (total borrowings, how it was built). None when nothing is tagged.

    Three shapes, in order of how much they can be trusted:

      1. LongTermDebtNoncurrent + the current bucket. Unambiguous, and what
         most filers tag.
      2. A concept whose name says it includes current maturities, plus the
         short-term borrowings that are never inside it.
      3. `LongTermDebt`, which filers use both ways: Apple's is noncurrent
         *plus* current maturities (82,300 = 71,340 + 11,007), Tesla's is
         noncurrent only (6,584, with 1,569 of current debt tagged separately).
         Nothing in the data says which, so this adds the current bucket — the
         reading that matches Tesla's own balance sheet — and says so in the
         basis line, since the other reading double-counts the current portion.

    Only one shape ever applies: they overlap, and adding two would double-count.
    """
    current, current_basis = _current_debt(val, year)
    noncurrent = val("long_term_debt", year)
    if noncurrent is not None:
        return _add(noncurrent, current), "= long-term debt (noncurrent) + " + current_basis

    inclusive = val("debt_incl_current", year)
    if inclusive is not None:
        short = _add(val("short_term_borrowings", year), val("commercial_paper", year))
        return _add(inclusive, short), (
            "= long-term debt including current maturities"
            + (" + short-term borrowings and commercial paper" if short else "")
        )

    ambiguous = val("long_term_debt_ambiguous", year)
    if ambiguous is not None:
        return _add(ambiguous, current), (
            "= LongTermDebt + " + current_basis + ". **Check this one against "
            "`statements/balance_sheet.md`**: filers tag LongTermDebt both with and without "
            "current maturities, and if this filer included them, the current portion is in "
            "here twice"
        )
    return None, None


def _days(numerator, denominator):
    """A balance divided by a year of flow, in days. None when either is absent."""
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator * 365, 1)


def _model_ratios(val, year, d):
    """The ratios a forecast is actually built out of.

    Everything here is arithmetic on rows already above it, and every one of
    them is a line a model needs and would otherwise be worked out by hand off
    a printed statement:

      - **Effective tax rate** is the gate to unlevered free cash flow. It is
        computed only on a positive pretax figure: tax over a loss is a rate
        with no forecasting meaning, and printing one invites it into a model.
      - **Invested capital** here is debt + equity − cash and short-term
        investments, which is the operating-capital reading. Other definitions
        exist; this one is stated wherever the number is shown so it can be
        recomputed rather than guessed at.
      - **The days ratios** turn the balance sheet into a working-capital
        schedule. Receivable days run on revenue, inventory and payable days on
        cost of revenue — a filer with no cost line (a bank) gets blanks rather
        than days computed off the wrong denominator.
    """
    rev, ni = val("revenue", year), val("net_income", year)
    ebit, ebitda = d.get("ebit"), d.get("ebitda")
    pretax, tax = val("pretax_income", year), val("income_tax_expense", year)
    cogs = val("cost_of_revenue", year)
    fcf, equity = d.get("free_cash_flow"), val("total_equity", year)
    shares = val("shares_diluted", year)
    debt, liquid = d.get("total_debt"), d.get("cash_and_st_investments")
    net_cash = d.get("net_cash")
    cur_a, cur_l = val("current_assets", year), val("current_liabilities", year)
    divs, buys = val("dividends_paid", year), val("buybacks", year)

    tax_rate = _ratio(tax, pretax) if pretax and pretax > 0 and tax is not None else None
    nopat = ebit * (1 - tax_rate / 100) if ebit is not None and tax_rate is not None else None
    invested = _add(debt, equity, -liquid if liquid is not None else None) \
        if equity is not None else None
    working_capital = (cur_a - cur_l) if cur_a is not None and cur_l is not None else None
    returned = _add(divs, buys)

    dso, dio = _days(val("accounts_receivable", year), rev), _days(val("inventory", year), cogs)
    dpo = _days(val("accounts_payable", year), cogs)

    return {
        "effective_tax_rate_pct": tax_rate,
        "nopat": nopat,
        "invested_capital": invested,
        "invested_capital_basis": "total debt + total equity − cash and short-term investments"
        if invested is not None else None,
        "roic_pct": _ratio(nopat, invested),
        "asset_turnover": _ratio(rev, val("total_assets", year), pct=False),
        "current_ratio": _ratio(cur_a, cur_l, pct=False),
        "working_capital": working_capital,
        "net_debt_to_ebitda": _ratio(-net_cash, ebitda, pct=False)
        if net_cash is not None and ebitda else None,
        "interest_coverage": _ratio(ebit, val("interest_expense", year), pct=False),
        "receivable_days": dso,
        "inventory_days": dio,
        "payable_days": dpo,
        "cash_conversion_days": round(dso + dio - dpo, 1)
        if None not in (dso, dio, dpo) else None,
        "capex_pct_revenue": _ratio(val("capex", year), rev),
        "rnd_pct_revenue": _ratio(val("rnd_expense", year), rev),
        "sga_pct_revenue": _ratio(val("sga_expense", year), rev),
        "stock_comp_pct_revenue": _ratio(val("stock_comp", year), rev),
        # Cash earnings against accounting earnings. Persistently under 100%
        # is the classic tell that profit is not turning into cash.
        "fcf_conversion_pct": _ratio(fcf, ni),
        "fcf_per_share": _ratio(fcf, shares, pct=False),
        "book_value_per_share": _ratio(equity, shares, pct=False),
        "revenue_per_share": _ratio(rev, shares, pct=False),
        "dividend_payout_pct": _ratio(divs, ni),
        "shareholder_returns": returned,
        "shareholder_returns_pct_fcf": _ratio(returned, fcf),
    }


def derive(series, years):
    """Ratios computed from the tagged values above. Every one of these is
    calculated here, not reported by the company."""
    def val(name, year):
        # .get on both levels: derive() is also handed the synthetic one-period
        # dicts that ttm() and snapshot() build, and a line neither of those
        # carries must read as "not there", not raise.
        return series.get(name, {}).get("values", {}).get(year)

    out = {}
    for i, year in enumerate(years):
        prev = years[i - 1] if i else None
        rev, ni = val("revenue", year), val("net_income", year)
        ocf, capex = val("operating_cash_flow", year), val("capex", year)
        fcf = ocf - capex if ocf is not None and capex is not None else None
        gross = val("gross_profit", year)
        if gross is None and rev is not None and val("cost_of_revenue", year) is not None:
            gross = rev - val("cost_of_revenue", year)
        debt, debt_basis = _total_debt(val, year)
        liquid = _add(val("cash", year), val("short_term_investments", year))
        # Operating income is the clean EBIT. Banks and insurers don't tag it at
        # all (JPMorgan, Berkshire), so fall back to the definition: profit
        # before tax and interest.
        ebit = val("operating_income", year)
        ebit_basis = "operating income, as the company tagged it"
        if ebit is None:
            pretax, interest = val("pretax_income", year), val("interest_expense", year)
            if pretax is not None and interest is not None:
                ebit, ebit_basis = pretax + interest, "pretax income + interest expense"
            else:
                ebit_basis = None
        d_and_a = val("d_and_a", year)

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
            "ebit": ebit,
            "ebit_basis": ebit_basis,
            "ebitda": (ebit + d_and_a) if ebit is not None and d_and_a is not None else None,
            "total_debt": debt,
            "debt_basis": debt_basis,
            # Par value is usually zero even when there is real preferred stock
            # outstanding; the liquidation preference is what an EV bridge wants.
            # max(), not _add(): these are two readings of the same balance, not
            # two components to sum. A 0 either tags (no preferred) is real, not
            # missing — only "neither tagged" should read as missing.
            "preferred": (
                max(v for v in (val("preferred_stock", year), val("preferred_liquidation", year))
                    if v is not None)
                if val("preferred_stock", year) is not None or val("preferred_liquidation", year) is not None
                else None
            ),
            "minority_interest": val("minority_interest", year),
            "net_cash": (liquid - debt) if liquid is not None and debt is not None else None,
            "debt_to_equity": _ratio(debt, val("total_equity", year), pct=False),
            "shares_change_pct": _pct(val("shares_diluted", year), val("shares_diluted", prev))
            if prev
            else None,
            # The liquid side of the net-cash line, on its own — it is an input
            # to a valuation in its own right, not only a subtraction.
            "cash_and_st_investments": liquid,
            # Q4, and Q4 a year earlier, when the filer tags them. See
            # QUARTERLY_CONCEPTS for why this is usually blank.
            "mrq_revenue": val("mrq_revenue", year),
            "mrq_revenue_prior": val("mrq_revenue", prev) if prev else None,
        }
        out[year].update(_model_ratios(val, year, out[year]))

    out["cagr"] = {
        "revenue_3y_pct": _cagr(series["revenue"]["values"], 3),
        "revenue_5y_pct": _cagr(series["revenue"]["values"], 5),
        "net_income_5y_pct": _cagr(series["net_income"]["values"], 5),
        "eps_5y_pct": _cagr(series["eps_diluted"]["values"], 5),
    }
    return out


# --- trailing twelve months ----------------------------------------------
#
# The 10-K is audited, complete and up to a year stale. The 10-Qs filed since
# are the only tagged data that closes that gap, and rolling the year forward
# with them is one subtraction:
#
#     TTM = fiscal year + year-to-date this year - year-to-date a year ago
#
# Year-to-date, never discrete quarters. Two reasons, both fatal to the other
# approach: a 10-Q's cash flow statement is *always* cumulative from the year
# start, so there is no discrete-quarter operating cash flow to add up; and the
# fourth quarter never appears in a 10-Q at all, so a four-quarter sum is always
# missing a leg. One shape works for every line on every statement.
#
# All of it comes out of the companyfacts call the run already makes, so the
# whole feature costs zero extra SEC requests.

# Flow items only. A weighted-average share count is deliberately absent:
# differencing two weighted averages produces a number that means nothing.
# EPS *is* here — summing per-share amounts across periods is the standard TTM
# convention, and it drifts from net-income/shares only by buybacks inside the
# year. `ttm()` records both so the drift is checkable.
TTM_METRICS = [
    "revenue", "cost_of_revenue", "gross_profit", "rnd_expense", "operating_income",
    "net_income", "eps_diluted", "operating_cash_flow", "capex", "d_and_a",
    "pretax_income", "interest_expense", "buybacks", "dividends_paid",
    # Same rule as the rest: a flow, tagged cumulatively every quarter, so the
    # year-to-date subtraction works on it unchanged. Per-share amounts are
    # summed across periods, which is the standard TTM convention.
    "income_tax_expense", "sga_expense", "selling_marketing_expense", "stock_comp",
    "eps_basic", "dividends_per_share", "acquisitions", "interest_income",
]

# The anchor exists only to establish *which two periods* are being
# differenced, so any line the filer tags cumulatively every quarter will do.
# Revenue first. Banks are why there is a fallback: JPMorgan tags `Revenues` in
# its 10-K and nothing from the revenue list in its 10-Qs, so anchoring on
# revenue alone throws away a net-income roll-forward it can perfectly well
# support. Lines the anchor can't carry come back in `not_tagged`.
TTM_ANCHORS = [DURATION_CONCEPTS["revenue"], DURATION_CONCEPTS["net_income"]]

TTM_TOLERANCE_DAYS = 10   # a 53-week fiscal year stretches a period by 7
TTM_YEAR_DAYS = 364       # 52 weeks — retail calendars land here, not on 365
TTM_END_TOLERANCE = 20    # how far the prior year's quarter end may drift
# A quarter this far past the year end belongs to a later fiscal year, not this
# one. Q3 lands ~250 days out; the following year's Q1 lands ~450.
TTM_MAX_GAP_DAYS = 400


def _duration_days(row):
    return (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days


def _quarterly_rows(facts, concepts, after=None):
    """Every 10-Q duration row, from the first concept still tagged after `after`.

    First-concept-wins rather than merging: the two periods being differenced
    have to come from one concept, or an accounting-standard change inside the
    window silently subtracts two different definitions of revenue.

    `after` is what makes that safe. Filers abandon concepts without deleting
    the history: Uber tagged RevenueFromContractWithCustomerExcludingAssessedTax
    in its 10-Qs only through 2019 and reports `Revenues` now, so "first concept
    with any rows at all" locks onto a series seven years dead and the
    roll-forward finds no quarter to anchor on. Every row of the chosen concept
    comes back, including ones before the cutoff — the prior-year comparative is
    always one of those.
    """
    for concept in concepts:
        rows = [r for r in _units(facts.get(concept, {}))
                if r.get("form") == "10-Q" and "start" in r and "val" in r]
        if rows and (after is None or any(r["end"] > after for r in rows)):
            return concept, rows
    return None, []


def _pick_ytd(rows, end, days=None):
    """The year-to-date row ending on `end`, or None.

    Q3 tags both a 13-week and a 39-week period finishing the same day, and only
    the cumulative one can be differenced against a full year — so the longest
    duration wins. Passing `days` pins the length instead, which is how the
    prior year's comparable period is matched. Restatements tie-break on filing
    date, latest winning.
    """
    best = None
    for row in rows:
        if row["end"] != end:
            continue
        d = _duration_days(row)
        if days is not None and abs(d - days) > TTM_TOLERANCE_DAYS:
            continue
        if best is None or (d, row["filed"]) > (_duration_days(best), best["filed"]):
            best = row
    return best


def _prior_year_end(rows, q_end, days):
    """The period end ~52 weeks before `q_end` covering the same span.

    Matched on the data rather than computed, because fiscal calendars drift:
    Costco's Q3 ends 2026-05-10 and 2025-05-11, 364 days apart, and neither is
    the same calendar date.
    """
    target = date.fromisoformat(q_end) - timedelta(days=TTM_YEAR_DAYS)
    best = None
    for row in rows:
        if abs(_duration_days(row) - days) > TTM_TOLERANCE_DAYS:
            continue
        gap = abs((date.fromisoformat(row["end"]) - target).days)
        if gap <= TTM_END_TOLERANCE and (best is None or gap < best[0]):
            best = (gap, row["end"])
    return best[1] if best else None


def _anchor_rows(facts, fy_end):
    """(concept, rows) for the first anchor line this filer still tags."""
    for concepts in TTM_ANCHORS:
        concept, rows = _quarterly_rows(facts, concepts, after=fy_end)
        if rows:
            return concept, rows
    return None, []


def latest_quarter(facts, fy_end):
    """The newest 10-Q period that belongs to the year *after* `fy_end`.

    Read off the facts rather than the submissions index, so the period named is
    the one that actually supplied the numbers — and so pulling an old 10-K with
    --year rolls it forward with that year's quarters, not today's.
    """
    _, rows = _anchor_rows(facts, fy_end)
    if not rows:
        return None
    cutoff = date.fromisoformat(fy_end) + timedelta(days=TTM_MAX_GAP_DAYS)
    ends = [r["end"] for r in rows
            if fy_end < r["end"] <= cutoff.isoformat()]
    return _pick_ytd(rows, max(ends)) if ends else None


def ttm(facts, series, fy_end):
    """Roll the fiscal year forward with 10-Q data. None when that can't be done.

    Returns None — rather than a guess — when no 10-Q post-dates the 10-K (the
    annual report is the company's newest filing), or when the prior year's
    comparable period isn't tagged. A blank TTM is the honest answer; nothing
    here is extrapolated.
    """
    anchor = latest_quarter(facts, fy_end)
    if anchor is None:
        return None
    q_end, days = anchor["end"], _duration_days(anchor)
    anchor_concept, anchor_rows = _anchor_rows(facts, fy_end)
    prior_end = _prior_year_end(anchor_rows, q_end, days)
    if prior_end is None:
        return None

    values, ytd, prior_ytd, growth, concepts, annual, missing = {}, {}, {}, {}, {}, {}, []
    for name in TTM_METRICS:
        concept, rows = _quarterly_rows(facts, DURATION_CONCEPTS[name], after=fy_end)
        cur = _pick_ytd(rows, q_end, days) if rows else None
        pri = _pick_ytd(rows, prior_end, days) if rows else None
        fy = series.get(name, {}).get("values", {}).get(fy_end)
        ytd[name] = cur["val"] if cur else None
        prior_ytd[name] = pri["val"] if pri else None
        growth[name] = _pct(ytd[name], prior_ytd[name])
        concepts[name] = concept
        annual[name] = fy
        if cur is not None and pri is not None and fy is not None:
            values[name] = fy + cur["val"] - pri["val"]
        else:
            values[name] = None
            missing.append(name)

    # The balance sheet is restated at the quarter end too, so the EV bridge
    # moves with the earnings instead of staying a year behind them.
    balance, balance_concepts = {}, {}
    for name, cs in INSTANT_CONCEPTS.items():
        used, vals = _merge(facts, cs, lambda r: "start" not in r and r["end"] == q_end)
        balance[name] = vals.get(q_end)
        balance_concepts[name] = used[0] if used else None

    # Feed the same derive() the annual rows go through — one definition of
    # EBIT, EBITDA, free cash flow and total debt across both, including the
    # debt-shape logic that decides what does and doesn't double-count.
    synthetic = {name: {"values": {}} for name in
                 list(DURATION_CONCEPTS) + list(INSTANT_CONCEPTS) + list(QUARTERLY_CONCEPTS)}
    for name, v in list(values.items()) + list(balance.items()):
        synthetic[name]["values"][q_end] = v
    derived = derive(synthetic, [q_end])[q_end]

    weeks = round(days / 7)
    return {
        "quarter_end": q_end,
        "anchor_concept": anchor_concept,
        "quarter_filed": anchor.get("filed"),
        "fiscal_period": anchor.get("fp"),
        "accession": anchor.get("accn"),
        "prior_quarter_end": prior_end,
        "fiscal_year_end": fy_end,
        "weeks_year_to_date": weeks,
        "days_stale_at_fy_end": (date.fromisoformat(q_end) - date.fromisoformat(fy_end)).days,
        "basis": (
            "fiscal year ended {} + {} weeks ended {} − {} weeks ended {}".format(
                fy_end, weeks, q_end, weeks, prior_end)
        ),
        "values": values,
        "fiscal_year": annual,
        "year_to_date": ytd,
        "prior_year_to_date": prior_ytd,
        "ytd_growth_pct": growth,
        "concepts": concepts,
        "balance": balance,
        "balance_concepts": balance_concepts,
        "derived": derived,
        "not_tagged": missing,
    }


# --- the most recent quarter ---------------------------------------------
#
# A single quarter, not a rolling year. The 10-K tags one only if the filer
# still publishes selected quarterly data (most don't since 2021), so the real
# source is the latest 10-Q — and when the 10-K *is* the newest filing, the
# fourth quarter is the year less the last year-to-date 10-Q of that year.


def _annual_value(facts, concept, end):
    """One concept's own annual value for the year ending `end`, latest filing
    winning. None when that concept has no annual row there."""
    if not concept:
        return None
    rows = [r for r in _units(facts.get(concept, {}))
            if r.get("form", "").startswith("10-K") and "start" in r and r["end"] == end
            and MIN_DAYS <= _duration_days(r) <= MAX_DAYS]
    return max(rows, key=lambda r: r["filed"])["val"] if rows else None


def _quarter_row(rows, end):
    """The discrete quarter-length row ending on `end`. Latest filing wins.

    Q2 and Q3 tag a cumulative period ending the same day; those run past
    QUARTER_MAX_DAYS and drop out here. Q1's only row is both at once.
    """
    best = None
    for row in rows:
        if row["end"] != end or not (
            QUARTER_MIN_DAYS <= _duration_days(row) <= QUARTER_MAX_DAYS
        ):
            continue
        if best is None or row["filed"] > best["filed"]:
            best = row
    return best


def _quarter_value(rows, end):
    """-> (the quarter ending `end`, how it was arrived at).

    Falls back to differencing two cumulative periods for filers that tag only
    year-to-date figures. Both legs start on the same day — the fiscal year
    start — which is what identifies the pair without knowing the calendar.
    """
    q = _quarter_row(rows, end)
    if q is not None:
        return q["val"], "tagged as a discrete quarter"
    cur = _pick_ytd(rows, end)
    if cur is None:
        return None, None
    same_year = [
        r for r in rows
        if r["end"] < end
        and abs((date.fromisoformat(r["start"]) - date.fromisoformat(cur["start"])).days)
        <= TTM_TOLERANCE_DAYS
    ]
    if not same_year:
        return None, None
    prev = max(same_year, key=lambda r: (r["end"], _duration_days(r), r["filed"]))
    return cur["val"] - prev["val"], (
        "year-to-date to {} less year-to-date to {} — this filer tags no discrete "
        "quarter".format(end, prev["end"])
    )


def mrq(facts, series, years):
    """Revenue for the most recent quarter, and the same quarter a year earlier.

    The latest 10-Q when one post-dates the 10-K; the fourth quarter out of the
    10-K itself when it doesn't. Never a full year mislabelled as a quarter, and
    never a quarter differenced across two revenue concepts.
    """
    if not years:
        return None
    fy_end = years[-1]
    prior_fy = years[-2] if len(years) > 1 else None

    concept, rows = _quarterly_rows(facts, DURATION_CONCEPTS["revenue"], after=fy_end)
    cutoff = (date.fromisoformat(fy_end) + timedelta(days=TTM_MAX_GAP_DAYS)).isoformat()
    ends = [r["end"] for r in rows if fy_end < r["end"] <= cutoff]
    if ends:
        q_end = max(ends)
        ytd = _pick_ytd(rows, q_end)
        prior_end = _prior_year_end(rows, q_end, _duration_days(ytd)) if ytd else None
        value, basis = _quarter_value(rows, q_end)
        prior, _ = _quarter_value(rows, prior_end) if prior_end else (None, None)
        return {
            "quarter_end": q_end,
            "revenue": value,
            "prior_quarter_end": prior_end,
            "prior_revenue": prior,
            "growth_pct": _pct(value, prior),
            "concept": concept,
            "source": "latest 10-Q",
            "basis": basis,
        }

    # No 10-Q since the year end: the most recent quarter is the fourth.
    tagged = series.get("mrq_revenue", {}).get("values", {}).get(fy_end)
    if tagged is not None:
        return {
            "quarter_end": fy_end,
            "revenue": tagged,
            "prior_quarter_end": prior_fy,
            "prior_revenue": series["mrq_revenue"]["values"].get(prior_fy),
            "growth_pct": _pct(tagged, series["mrq_revenue"]["values"].get(prior_fy)),
            "concept": series["mrq_revenue"]["concepts"][0]
            if series["mrq_revenue"]["concepts"] else None,
            "source": "10-K (the filer tags a fourth quarter)",
            "basis": "tagged as a discrete quarter in the 10-K",
        }
    if prior_fy is None:
        return None
    concept, rows = _quarterly_rows(facts, DURATION_CONCEPTS["revenue"], after=prior_fy)

    def q4(year_end, year_start):
        """Fiscal year less the last year-to-date 10-Q inside it.

        The annual leg is read off `concept` specifically, not off the merged
        series: `_merge` walks a priority list and can't say afterwards which
        concept supplied a given year. A filer that tags revenue including
        assessed tax annually and excluding it quarterly would otherwise have
        the two definitions differenced — the exact mismatch `_quarterly_rows`
        exists to prevent one line up.
        """
        annual = _annual_value(facts, concept, year_end)
        inside = [r["end"] for r in rows if year_start and year_start < r["end"] < year_end]
        last = _pick_ytd(rows, max(inside)) if inside and annual is not None else None
        return (annual - last["val"], last["end"]) if last else (None, None)

    value, through = q4(fy_end, prior_fy)
    prior, _ = q4(prior_fy, years[-3] if len(years) > 2 else None)
    return {
        "quarter_end": fy_end,
        "revenue": value,
        "prior_quarter_end": prior_fy,
        "prior_revenue": prior,
        "growth_pct": _pct(value, prior),
        "concept": concept,
        "source": "10-K less the year's last 10-Q",
        "basis": "the fiscal year less year-to-date through {} — this filer tags no fourth "
                 "quarter".format(through) if value is not None else
                 "no fourth quarter is tagged in the 10-K, and there is no year-to-date 10-Q "
                 "inside the fiscal year on the same concept to difference the year against",
    }


# --- the balance sheet, as of the newest filing --------------------------
#
# Stock items take no LTM arithmetic and no roll-forward: whatever the most
# recent filing states is the number. Deliberately independent of ttm(), which
# returns None when the prior-year comparable is missing — a balance sheet needs
# no comparable, so a missing one must not take the snapshot down with it.

# Every filer tags assets and equity every quarter; cash is the backstop for a
# shell or a fund that somehow doesn't.
BALANCE_ANCHORS = ["Assets", "StockholdersEquity", "CashAndCashEquivalentsAtCarryingValue"]

# How far past the year end a balance sheet may sit and still belong to this
# filing's forward window. Tighter than TTM_MAX_GAP_DAYS, and the gap between
# the two numbers is the whole point: the *next* fiscal year end lands at ~364
# days, inside a 400-day window. On a default run that filing doesn't exist yet,
# but with --year it does, and picking it up would hand back the following
# year's audited balance sheet as "the latest 10-Q". A Q3 balance sheet is the
# furthest legitimate hit, ~275 days out, and a Q3 cover-page date ~310.
SNAPSHOT_MAX_GAP_DAYS = 340


def _latest_instant_date(facts, fy_end):
    """Newest balance-sheet date in a 10-K or 10-Q at or after `fy_end`.

    Capped short of the next fiscal year end, so pulling an old 10-K with --year
    gets that filing's own following quarters rather than a later year.
    """
    cutoff = (date.fromisoformat(fy_end) + timedelta(days=SNAPSHOT_MAX_GAP_DAYS)).isoformat()
    ends = [
        r["end"]
        for concept in BALANCE_ANCHORS
        for r in _units(facts.get(concept, {}))
        if "start" not in r and r.get("form") in ("10-K", "10-Q")
        and fy_end <= r["end"] <= cutoff
    ]
    return max(ends) if ends else None


def _latest_share_count(facts, dei, end, fy_end):
    """Cover-page shares outstanding and weighted-average diluted, newest first.

    Two different counts, both reported, neither substituted for the other: the
    cover page states shares actually outstanding on a date after the quarter
    closed, while the income statement's diluted count is a weighted average
    over the quarter. A model wants to know which cell it is wiring.
    """
    cutoff = (date.fromisoformat(fy_end) + timedelta(days=SNAPSHOT_MAX_GAP_DAYS)).isoformat()
    cover_rows = [
        r for r in dei.get("EntityCommonStockSharesOutstanding", {})
        .get("units", {}).get("shares", [])
        if fy_end <= r["end"] <= cutoff
    ]
    cover = max(cover_rows, key=lambda r: (r["end"], r["filed"])) if cover_rows else None

    diluted, concept = None, None
    for name in DURATION_CONCEPTS["shares_diluted"]:
        rows = [
            r for r in _units(facts.get(name, {}))
            if "start" in r and r.get("form") in ("10-K", "10-Q") and r["end"] == end
        ]
        if rows:
            # Shortest period ending that day is the quarter, not the year to
            # date; a restatement of the same period tie-breaks on filing date.
            shortest = min(_duration_days(r) for r in rows)
            diluted = max((r for r in rows if _duration_days(r) == shortest),
                          key=lambda r: r["filed"])
            concept = name
            break
    return {
        "cover_page": cover["val"] if cover else None,
        "cover_page_as_of": cover["end"] if cover else None,
        "cover_page_form": cover.get("form") if cover else None,
        "cover_page_note": None if cover else
        "not in companyfacts — multi-class filers tag this per class, and the API "
        "carries undimensioned facts only. valuation.md reads it off the rendered "
        "cover page instead.",
        "weighted_average_diluted": diluted["val"] if diluted else None,
        "weighted_average_diluted_days": _duration_days(diluted) if diluted else None,
        "weighted_average_diluted_concept": concept,
    }


def snapshot(companyfacts, fy_end):
    """The balance sheet as of the most recent 10-K or 10-Q. No LTM math.

    Every line comes from one filing on one date, so cash, debt, assets and
    inventory are internally consistent rather than assembled from whichever
    filing tagged each concept last.
    """
    facts = companyfacts.get("facts", {}).get("us-gaap", {})
    end = _latest_instant_date(facts, fy_end)
    if end is None:
        return None

    values, concepts, form, filed, accession = {}, {}, None, None, None
    for name, cs in INSTANT_CONCEPTS.items():
        used, vals = _merge(facts, cs, lambda r: "start" not in r and r["end"] == end)
        values[name] = vals.get(end)
        concepts[name] = used[0] if used else None
    # Which filing this balance sheet *is*, not which one restated it last. A
    # year-end balance sheet reappears as the comparative column in the next
    # three 10-Qs, all filed later, so latest-filed-wins would label the 10-K's
    # own balance sheet a 10-Q and point at the wrong accession.
    expected = "10-K" if end == fy_end else "10-Q"
    for concept in BALANCE_ANCHORS:
        rows = [r for r in _units(facts.get(concept, {}))
                if "start" not in r and r["end"] == end and r.get("form") == expected]
        if rows:
            row = min(rows, key=lambda r: r["filed"])
            form, filed, accession = row.get("form"), row.get("filed"), row.get("accn")
            break

    # Same derive() the annual rows go through, so total debt is built by the
    # one routine that knows which debt concepts overlap.
    synthetic = {name: {"values": {}} for name in
                 list(DURATION_CONCEPTS) + list(INSTANT_CONCEPTS) + list(QUARTERLY_CONCEPTS)}
    for name, v in values.items():
        synthetic[name]["values"][end] = v
    d = derive(synthetic, [end])[end]

    return {
        "as_of": end,
        "form": form,
        "filed": filed,
        "accession": accession,
        "values": values,
        "concepts": concepts,
        "total_debt": d["total_debt"],
        "debt_basis": d["debt_basis"],
        "cash_and_st_investments": d["cash_and_st_investments"],
        "net_cash": d["net_cash"],
        "shares": _latest_share_count(facts, companyfacts.get("facts", {}).get("dei", {}),
                                      end, fy_end),
    }


# --- rendering -----------------------------------------------------------

MILLIONS = {
    "revenue", "cost_of_revenue", "gross_profit", "rnd_expense", "operating_income",
    "net_income", "operating_cash_flow", "capex", "buybacks", "dividends_paid",
    "total_assets", "total_equity", "cash", "short_term_investments", "inventory",
    "long_term_debt", "current_debt", "total_liabilities", "d_and_a", "pretax_income",
    "interest_expense", "preferred_stock", "preferred_liquidation", "minority_interest",
    "finance_lease_liability", "operating_lease_liability",
}
MILLIONS |= {"mrq_revenue", "income_tax_expense", "sga_expense",
             "selling_marketing_expense", "stock_comp", "acquisitions", "interest_income",
             "current_assets", "current_liabilities", "accounts_receivable",
             "accounts_payable", "ppe_net", "goodwill", "intangibles",
             "deferred_revenue", "retained_earnings", "long_term_investments"}
PER_SHARE = {"eps_diluted", "eps_basic", "dividends_per_share"}
DERIVED_MILLIONS = {"free_cash_flow", "ebit", "ebitda", "total_debt", "preferred",
                    "minority_interest", "net_cash", "cash_and_st_investments",
                    "mrq_revenue", "mrq_revenue_prior",
                    "nopat", "invested_capital", "working_capital", "shareholder_returns"}
DERIVED_PER_SHARE = {"fcf_per_share", "book_value_per_share", "revenue_per_share"}
ROW_LABELS = [
    ("revenue", "Revenue"), ("mrq_revenue", "MRQ revenue (Q4)"),
    ("gross_profit", "Gross profit"),
    ("operating_income", "Operating income"), ("d_and_a", "D&A"),
    ("interest_expense", "Interest expense"), ("net_income", "Net income"),
    ("eps_diluted", "EPS (diluted)"), ("shares_diluted", "Diluted shares"),
    ("operating_cash_flow", "Cash from ops"), ("capex", "Capex"),
    ("total_assets", "Total assets"), ("total_equity", "Total equity"),
    ("inventory", "Inventory"),
    ("cash", "Cash"), ("short_term_investments", "Short-term investments"),
    ("long_term_debt", "Long-term debt"),
    ("minority_interest", "Minority interest"),
    ("operating_lease_liability", "Operating lease liabilities"),
    ("buybacks", "Buybacks"), ("dividends_paid", "Dividends paid"),
    # --- the model lines --------------------------------------------------
    ("sga_expense", "SG&A"), ("selling_marketing_expense", "Selling & marketing"),
    ("rnd_expense", "R&D"), ("stock_comp", "Stock-based compensation"),
    ("income_tax_expense", "Income tax expense"),
    ("interest_income", "Interest income"),
    ("eps_basic", "EPS (basic)"), ("shares_basic", "Basic shares"),
    ("dividends_per_share", "Dividends per share"),
    ("acquisitions", "Acquisitions"),
    ("current_assets", "Current assets"), ("current_liabilities", "Current liabilities"),
    ("accounts_receivable", "Accounts receivable"),
    ("accounts_payable", "Accounts payable"),
    ("ppe_net", "PP&E, net"), ("goodwill", "Goodwill"), ("intangibles", "Intangibles"),
    ("deferred_revenue", "Deferred revenue"),
    ("retained_earnings", "Retained earnings"),
    ("long_term_investments", "Long-term investments"),
    ("total_liabilities", "Total liabilities"),
]
DERIVED_LABELS = [
    ("revenue_growth_pct", "Revenue growth %"), ("eps_growth_pct", "EPS growth %"),
    ("gross_margin_pct", "Gross margin %"), ("operating_margin_pct", "Operating margin %"),
    ("net_margin_pct", "Net margin %"), ("ebit", "EBIT"), ("ebitda", "EBITDA"),
    ("free_cash_flow", "Free cash flow"),
    ("fcf_margin_pct", "FCF margin %"), ("roe_pct", "ROE %"), ("roa_pct", "ROA %"),
    ("total_debt", "Total debt"), ("cash_and_st_investments", "Cash + ST investments"),
    ("net_cash", "Net cash / (net debt)"),
    ("debt_to_equity", "Debt / equity"), ("shares_change_pct", "Share count change %"),
    # --- what a forecast is built out of ----------------------------------
    ("effective_tax_rate_pct", "Effective tax rate %"),
    ("nopat", "NOPAT"), ("invested_capital", "Invested capital"), ("roic_pct", "ROIC %"),
    ("asset_turnover", "Asset turnover"), ("current_ratio", "Current ratio"),
    ("working_capital", "Working capital"),
    ("net_debt_to_ebitda", "Net debt / EBITDA"),
    ("interest_coverage", "Interest coverage (EBIT / interest)"),
    ("receivable_days", "Receivable days"), ("inventory_days", "Inventory days"),
    ("payable_days", "Payable days"), ("cash_conversion_days", "Cash conversion cycle, days"),
    ("capex_pct_revenue", "Capex % of revenue"), ("rnd_pct_revenue", "R&D % of revenue"),
    ("sga_pct_revenue", "SG&A % of revenue"),
    ("stock_comp_pct_revenue", "Stock comp % of revenue"),
    ("fcf_conversion_pct", "FCF conversion % (FCF / net income)"),
    ("fcf_per_share", "FCF per share"), ("book_value_per_share", "Book value per share"),
    ("revenue_per_share", "Revenue per share"),
    ("dividend_payout_pct", "Dividend payout %"),
    ("shareholder_returns", "Dividends + buybacks"),
    ("shareholder_returns_pct_fcf", "Dividends + buybacks, % of FCF"),
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
        kind = "millions" if key in MILLIONS else ("decimal" if key in PER_SHARE else "whole")
        cells = [_fmt(entry["values"].get(y), kind) for y in years]
        lines.append("| {} | {} |".format(label, " | ".join(cells)))

    rescaled = (data["series"].get("shares_diluted") or {}).get("rescaled")
    if rescaled:
        lines += [
            "",
            "> **Diluted share count, {}:** this filer tags those years at a different scale "
            "from the rest of its history (a factor of {:,.0f}). The row above is on one scale, "
            "because net income divided by diluted EPS says that is what the count is — nothing "
            "real moves a share count by that much in a year. No other row is touched.".format(
                ", ".join(rescaled), max(rescaled.values())),
        ]

    lines += ["", "## Derived (computed here, not reported)", "", head, rule]
    for key, label in DERIVED_LABELS:
        kind = ("millions" if key in DERIVED_MILLIONS
                else "decimal" if key in DERIVED_PER_SHARE
                else "pct" if "_pct" in key or key.endswith("_days") else "decimal")
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
            lines.append("- `{}` — tried: {}{}".format(
                m["metric"], ", ".join(m["tried"]),
                ". " + m["note"] if m.get("note") else ""))
    return "\n".join(lines) + "\n"


def to_csv(data, ticker="", company=""):
    """The whole history as one spreadsheet — years across, metrics down.

    The markdown twin is for reading and this is for pasting: raw numbers in
    whole dollars, no thousands separators, no currency symbols, no em dashes.
    A blank cell is a blank cell, which is what a spreadsheet reads as "no
    value" — writing 0 there would be a number the filer never reported, and
    every average computed over it afterwards would be wrong.

    The last column is the trailing twelve months where there is one, so the
    sheet opens with the audited history and the current run-rate side by side.
    """
    import csv as _csv
    import io as _io

    years = data["fiscal_year_ends"]
    t = data.get("ttm") or {}
    snap = data.get("snapshot") or {}
    ltm_flows, ltm_stocks = t.get("values") or {}, (snap.get("values") or {})
    ltm_derived = t.get("derived") or {}
    has_ltm = bool(t or snap)
    ltm_header = "LTM to {}".format(t.get("quarter_end") or snap.get("as_of") or "")

    buf = _io.StringIO()
    w = _csv.writer(buf, lineterminator="\n")
    w.writerow(["# {} ({}) — {} fiscal years from SEC XBRL, whole dollars, "
                "as filed".format(company or "company", ticker, len(years))])
    w.writerow(["# Flow lines in the LTM column are twelve months to the quarter; stock lines "
                "are that one filing's balance sheet. An empty cell is a figure the filer never "
                "tagged, never a zero."])
    w.writerow(["Metric", "Section", "Source"] + years + ([ltm_header] if has_ltm else []))

    def cell(v):
        return "" if v is None else v

    for key, label in ROW_LABELS:
        entry = data["series"].get(key, {})
        if not entry.get("concepts"):
            continue
        latest = ltm_flows.get(key) if key in DURATION_CONCEPTS else ltm_stocks.get(key)
        w.writerow([label, "as reported", ", ".join(entry["concepts"])] +
                   [cell(entry["values"].get(y)) for y in years] +
                   ([cell(latest)] if has_ltm else []))

    for key, label in DERIVED_LABELS:
        w.writerow([label, "computed here", ""] +
                   [cell(data["derived"].get(y, {}).get(key)) for y in years] +
                   ([cell(ltm_derived.get(key))] if has_ltm else []))

    cagr = data["derived"].get("cagr", {})
    for key, label in [("revenue_3y_pct", "Revenue 3-yr CAGR %"),
                       ("revenue_5y_pct", "Revenue 5-yr CAGR %"),
                       ("net_income_5y_pct", "Net income 5-yr CAGR %"),
                       ("eps_5y_pct", "EPS 5-yr CAGR %")]:
        w.writerow([label, "computed here", ""] + [""] * (len(years) - 1) +
                   [cell(cagr.get(key))] + ([""] if has_ltm else []))
    return buf.getvalue()


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

    # MRQ: absent unless the filer tags a quarter-length period in its 10-K.
    assert d["series"]["mrq_revenue"]["values"] == {"2023-12-31": None, "2024-12-31": None}
    assert any(m["metric"] == "mrq_revenue" and m.get("note") for m in d["missing"]), d["missing"]
    assert "no quarterly period is tagged" in to_markdown(d, "Test Co")
    # And present when it does. Q4 ends on the fiscal year end, a 10-Q row at
    # the same length must not be mistaken for it.
    g["Revenues"]["units"]["USD"] += [
        {"start": "2024-10-01", "end": "2024-12-31", "val": 40e6, "form": "10-K",
         "filed": "2025-02-01"},
        {"start": "2023-10-01", "end": "2023-12-31", "val": 30e6, "form": "10-K",
         "filed": "2024-02-01"},
        {"start": "2024-07-01", "end": "2024-09-30", "val": 99e6, "form": "10-Q",
         "filed": "2024-10-01"},
    ]
    q = build(facts)
    assert q["series"]["mrq_revenue"]["values"]["2024-12-31"] == 40e6
    assert q["derived"]["2024-12-31"]["mrq_revenue_prior"] == 30e6
    assert not any(m["metric"] == "mrq_revenue" for m in q["missing"])
    # Cash + short-term investments is exposed on its own, not only inside net cash.
    g["CashAndCashEquivalentsAtCarryingValue"] = {"units": {"USD": [
        {"end": "2024-12-31", "val": 60e6, "form": "10-K", "filed": "2025-02-01"}]}}
    g["ShortTermInvestments"] = {"units": {"USD": [
        {"end": "2024-12-31", "val": 15e6, "form": "10-K", "filed": "2025-02-01"}]}}
    liq = build(facts)["derived"]["2024-12-31"]["cash_and_st_investments"]
    assert liq == 75e6, liq

    # as_of caps history at the filing being extracted, so pulling an old 10-K
    # doesn't show years that filing predates.
    old = build(facts, as_of="2023-12-31")
    assert old["fiscal_year_ends"] == ["2023-12-31"], old["fiscal_year_ends"]

    # --- the debt shapes, which are where double-counting hides -----------
    def debt_of(**tagged):
        return _total_debt(lambda name, _year: tagged.get(name), "2024-12-31")

    # Components: Apple's shape — commercial paper sits outside term debt.
    total, basis = debt_of(long_term_debt=80, current_debt=12, commercial_paper=8)
    assert total == 100 and "noncurrent" in basis, (total, basis)
    # DebtCurrent already contains both, so the pieces must not be added again.
    total, _ = debt_of(long_term_debt=80, debt_current_total=20, current_debt=12,
                       commercial_paper=8)
    assert total == 100, total
    # A concept that says it includes current maturities: don't add them again,
    # but short-term borrowings are never inside it.
    total, basis = debt_of(debt_incl_current=92, current_debt=12, commercial_paper=8)
    assert total == 100 and "including current maturities" in basis, (total, basis)
    # Bare LongTermDebt: add the current bucket and flag the double-count risk.
    total, basis = debt_of(long_term_debt_ambiguous=88, debt_current_total=12)
    assert total == 100 and "balance_sheet.md" in basis, (total, basis)
    assert debt_of() == (None, None), "nothing tagged is missing, not zero"

    # EBIT falls back to pretax + interest for filers that never tag operating
    # income (banks), and EBITDA adds D&A on top of whichever basis was used.
    d2 = derive(
        {
            "operating_income": {"values": {"2024-12-31": None}},
            "pretax_income": {"values": {"2024-12-31": 90e6}},
            "interest_expense": {"values": {"2024-12-31": 10e6}},
            "d_and_a": {"values": {"2024-12-31": 5e6}},
            "preferred_stock": {"values": {"2024-12-31": 0}},
            "preferred_liquidation": {"values": {"2024-12-31": 3e6}},
            **{k: {"values": {}}
               for k in list(DURATION_CONCEPTS) + list(INSTANT_CONCEPTS) + list(QUARTERLY_CONCEPTS)
               if k not in ("operating_income", "pretax_income", "interest_expense", "d_and_a",
                            "preferred_stock", "preferred_liquidation")},
        },
        ["2024-12-31"],
    )["2024-12-31"]
    assert d2["ebit"] == 100e6 and d2["ebit_basis"] == "pretax income + interest expense"
    assert d2["ebitda"] == 105e6, d2["ebitda"]
    # Par value of zero must not beat a real liquidation preference.
    assert d2["preferred"] == 3e6, d2["preferred"]

    # --- trailing twelve months ------------------------------------------
    # No 10-Q anywhere: the roll-forward must return None, not a guess.
    assert build(facts)["ttm"] is None, "TTM invented from 10-K rows alone"

    # A filer with a 52-week calendar, three quarters filed since the year end.
    # Q3 tags a discrete 13-week period and a cumulative 39-week one ending the
    # same day; only the cumulative one may be differenced against the year.
    tf = {"facts": {"us-gaap": {}}}
    tg = tf["facts"]["us-gaap"]
    tg.update(dur("Revenues", {"2023-12-30": 800e6, "2024-12-28": 1000e6}))
    tg.update(dur("NetIncomeLoss", {"2023-12-30": 80e6, "2024-12-28": 100e6}))
    tg.update(dur("NetCashProvidedByUsedInOperatingActivities",
                  {"2023-12-30": 90e6, "2024-12-28": 120e6}))
    q10 = lambda s, e, v: {"start": s, "end": e, "val": v, "form": "10-Q",
                           "filed": e, "accn": "acc-" + e, "fp": "Q3"}
    tg["Revenues"]["units"]["USD"] += [
        q10("2024-12-29", "2025-09-27", 850e6),   # 272d cumulative, this year
        q10("2025-06-29", "2025-09-27", 300e6),   # 90d discrete, same end date
        q10("2023-12-31", "2024-09-28", 780e6),   # 272d cumulative, a year back
        q10("2024-06-30", "2024-09-28", 280e6),   # 90d discrete, a year back
    ]
    tg["NetIncomeLoss"]["units"]["USD"] += [
        q10("2024-12-29", "2025-09-27", 88e6), q10("2023-12-31", "2024-09-28", 76e6)]
    tg["NetCashProvidedByUsedInOperatingActivities"]["units"]["USD"] += [
        q10("2024-12-29", "2025-09-27", 100e6), q10("2023-12-31", "2024-09-28", 85e6)]
    tg.update(dur("PaymentsToAcquirePropertyPlantAndEquipment",
                  {"2023-12-30": 30e6, "2024-12-28": 40e6}))
    tg["PaymentsToAcquirePropertyPlantAndEquipment"]["units"]["USD"] += [
        q10("2024-12-29", "2025-09-27", 36e6), q10("2023-12-31", "2024-09-28", 28e6)]
    # Balance sheet at the quarter end, so the EV bridge moves with the earnings.
    tg["CashAndCashEquivalentsAtCarryingValue"] = {"units": {"USD": [
        {"end": "2024-12-28", "val": 50e6, "form": "10-K", "filed": "2025-02-01"},
        {"end": "2025-09-27", "val": 70e6, "form": "10-Q", "filed": "2025-10-01"}]}}
    tg["LongTermDebtNoncurrent"] = {"units": {"USD": [
        {"end": "2025-09-27", "val": 30e6, "form": "10-Q", "filed": "2025-10-01"}]}}

    t = build(tf)["ttm"]
    assert t["quarter_end"] == "2025-09-27", t["quarter_end"]
    # The 272-day row, not the 90-day one that ends on the same day.
    assert t["weeks_year_to_date"] == 39, t["weeks_year_to_date"]
    assert t["prior_quarter_end"] == "2024-09-28", t["prior_quarter_end"]
    # 1000 + 850 - 780. Discrete quarters would have produced 300 + something.
    assert t["values"]["revenue"] == 1070e6, t["values"]["revenue"]
    assert t["values"]["net_income"] == 112e6, t["values"]["net_income"]
    assert t["values"]["operating_cash_flow"] == 135e6, t["values"]["operating_cash_flow"]
    assert t["ytd_growth_pct"]["revenue"] == 9.0, t["ytd_growth_pct"]["revenue"]
    assert t["fiscal_year"]["revenue"] == 1000e6, "annual value not carried for comparison"
    assert t["accession"] == "acc-2025-09-27", t["accession"]
    # Balance sheet and derived figures come off the quarter, not the year end.
    assert t["balance"]["cash"] == 70e6, t["balance"]["cash"]
    assert t["derived"]["total_debt"] == 30e6, t["derived"]["total_debt"]
    # FCF is built from the TTM legs, not lifted from the year: (120+100-85)
    # minus (40+36-28) = 135 - 48.
    assert t["values"]["capex"] == 48e6, t["values"]["capex"]
    assert t["derived"]["free_cash_flow"] == 87e6, t["derived"]["free_cash_flow"]
    # A line the 10-Qs never tag is reported, never back-filled from the year.
    assert "d_and_a" in t["not_tagged"], t["not_tagged"]
    assert t["values"]["d_and_a"] is None

    # An old 10-K anchors on its *own* following quarter. Anchoring on the
    # newest quarter in the file would value a 2023 year with 2025 quarters.
    tgf = tf["facts"]["us-gaap"]
    assert latest_quarter(tgf, "2023-12-30")["end"] == "2024-09-28"
    assert latest_quarter(tgf, "2024-12-28")["end"] == "2025-09-27"
    # A year with no quarters after it at all has nothing to roll forward with.
    assert latest_quarter(tgf, "2025-09-27") is None
    # And with no prior-year comparable in the data to difference against, the
    # roll-forward refuses rather than returning a half-built year.
    assert build(tf, as_of="2023-12-30")["ttm"] is None

    # with_ttm=False is the --no-ttm path: annual figures untouched, no TTM key.
    off = build(tf, with_ttm=False)
    assert off["ttm"] is None and off["series"]["revenue"]["values"]["2024-12-28"] == 1000e6

    # A concept the filer abandoned must not win the anchor. Uber's shape: the
    # preferred revenue concept has 10-Q rows, but none since 2019.
    stale = {"facts": {"us-gaap": dict(tgf)}}
    sg = stale["facts"]["us-gaap"]
    sg["RevenueFromContractWithCustomerExcludingAssessedTax"] = {"units": {"USD": [
        q10("2019-01-01", "2019-03-31", 5e6)]}}
    st = build(stale)["ttm"]
    assert st and st["quarter_end"] == "2025-09-27", "anchored on a dead concept"
    assert st["values"]["revenue"] == 1070e6, st["values"]["revenue"]

    # A bank tags no revenue in its 10-Qs at all. Net income still rolls
    # forward; revenue comes back in not_tagged rather than wrong. (JPMorgan.)
    bank = {"facts": {"us-gaap": {k: v for k, v in tgf.items() if k != "Revenues"}}}
    bank["facts"]["us-gaap"]["Revenues"] = {"units": {"USD": [
        r for r in tgf["Revenues"]["units"]["USD"] if r.get("form") == "10-K"]}}
    bt = build(bank)["ttm"]
    assert bt["anchor_concept"] == "NetIncomeLoss", bt["anchor_concept"]
    assert bt["quarter_end"] == "2025-09-27" and bt["values"]["net_income"] == 112e6
    assert bt["values"]["revenue"] is None and "revenue" in bt["not_tagged"]

    # --- the most recent quarter -----------------------------------------
    # Costco's real shape: discrete quarters alongside cumulative ones ending
    # the same day. Both are already in the fixture above.
    full = build(tf)
    m = full["mrq"]
    assert m["quarter_end"] == "2025-09-27" and m["revenue"] == 300e6, m
    assert m["prior_quarter_end"] == "2024-09-28" and m["prior_revenue"] == 280e6, m
    assert m["growth_pct"] == 7.1 and m["basis"] == "tagged as a discrete quarter", m
    # 300 is the discrete quarter; 850 is the year to date ending the same day.
    assert m["revenue"] != 850e6, "cumulative period returned as a quarter"

    # A filer that tags no discrete quarter: the difference of two cumulative
    # periods sharing a fiscal-year start.
    ytd_only = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            r for r in tg["Revenues"]["units"]["USD"]
            if "start" not in r or _duration_days(r) > QUARTER_MAX_DAYS
        ] + [q10("2024-12-29", "2025-06-28", 550e6)]}},
    }}}
    ym = mrq(ytd_only["facts"]["us-gaap"],
             {"revenue": {"values": {"2024-12-28": 1000e6}},
              "mrq_revenue": {"values": {}, "concepts": []}},
             ["2023-12-30", "2024-12-28"])
    assert ym["revenue"] == 300e6, ym          # 850 year-to-date − 550 at the half
    assert "less year-to-date" in ym["basis"], ym["basis"]

    # No 10-Q since the year end: the fourth quarter is the year less the last
    # year-to-date 10-Q inside it. Costco's real numbers: 275,235 − 189,079.
    no_q = {"facts": {"us-gaap": {c: {"units": {u: [
        r for r in rows if not (r.get("form") == "10-Q" and r["end"] > "2024-12-28")]
        for u, rows in d["units"].items()}} for c, d in tg.items()}}}
    q4 = build(no_q)["mrq"]
    assert q4["quarter_end"] == "2024-12-28", q4
    assert q4["revenue"] == 1000e6 - 780e6, q4   # the year less its last 39-week 10-Q
    assert "no fourth quarter" in q4["basis"], q4["basis"]

    # Both legs of that subtraction must come from the same concept. Here the
    # 10-Qs report `Revenues` while the annual series *merges* in a
    # higher-priority concept the 10-Qs never use — reading the year off the
    # merged series would difference two definitions of revenue.
    mixed = {"facts": {"us-gaap": dict(no_q["facts"]["us-gaap"])}}
    mixed["facts"]["us-gaap"].update(
        dur("RevenueFromContractWithCustomerExcludingAssessedTax", {"2024-12-28": 1200e6}))
    mixed_built = build(mixed)
    assert mixed_built["series"]["revenue"]["values"]["2024-12-28"] == 1200e6, "fixture wrong"
    assert mixed_built["mrq"]["revenue"] == 1000e6 - 780e6, mixed_built["mrq"]

    # A year the 10-Q concept has no annual row for is a blank with a reason,
    # not a number differenced out of the wrong concept.
    assert _annual_value(tgf, "Revenues", "2024-12-28") == 1000e6
    assert _annual_value(tgf, "Revenues", "2099-12-31") is None
    assert _annual_value(tgf, None, "2024-12-28") is None

    # --- the balance-sheet snapshot ---------------------------------------
    tf["facts"]["dei"] = {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        {"end": "2025-02-14", "val": 501e6, "form": "10-K", "filed": "2025-02-14"},
        {"end": "2025-10-20", "val": 498e6, "form": "10-Q", "filed": "2025-10-20"},
    ]}}}
    tg["WeightedAverageNumberOfDilutedSharesOutstanding"] = {"units": {"shares": [
        {**q10("2024-12-29", "2025-09-27", 505e6), "form": "10-Q"},
        {**q10("2025-06-29", "2025-09-27", 503e6), "form": "10-Q"},
    ]}}
    # LongTermDebtCurrent of exactly 0 is a real zero, not a missing value: it
    # must not stop total debt resolving to the noncurrent figure.
    tg["LongTermDebtCurrent"] = {"units": {"USD": [
        {"end": "2025-09-27", "val": 0, "form": "10-Q", "filed": "2025-10-01"}]}}
    tg["Assets"] = {"units": {"USD": [
        {"end": "2024-12-28", "val": 800e6, "form": "10-K", "filed": "2025-02-14"},
        {"end": "2024-12-28", "val": 800e6, "form": "10-Q", "filed": "2025-10-01"},
        {"end": "2025-09-27", "val": 900e6, "form": "10-Q", "filed": "2025-10-01"}]}}
    tg["InventoryNet"] = {"units": {"USD": [
        {"end": "2025-09-27", "val": 190e6, "form": "10-Q", "filed": "2025-10-01"}]}}
    tg["ShortTermInvestments"] = {"units": {"USD": [
        {"end": "2025-09-27", "val": 10e6, "form": "10-Q", "filed": "2025-10-01"}]}}
    snap = build(tf)["snapshot"]
    assert snap["as_of"] == "2025-09-27" and snap["form"] == "10-Q", snap
    assert snap["values"]["total_assets"] == 900e6 and snap["values"]["inventory"] == 190e6
    assert snap["cash_and_st_investments"] == 80e6, snap["cash_and_st_investments"]
    assert snap["total_debt"] == 30e6, snap["total_debt"]  # 30 noncurrent + a real 0
    # Two counts, both reported, neither substituted for the other.
    assert snap["shares"]["cover_page"] == 498e6, snap["shares"]
    assert snap["shares"]["cover_page_as_of"] == "2025-10-20"
    assert snap["shares"]["weighted_average_diluted"] == 503e6, "took the year to date"

    # A year-end balance sheet reappears as the comparative column of the next
    # three 10-Qs. The snapshot must name the 10-K, not the last filing to
    # restate it. (Same facts, every later 10-Q row removed.)
    q4_snap = build(no_q)["snapshot"]
    assert q4_snap["as_of"] == "2024-12-28" and q4_snap["form"] == "10-K", q4_snap

    # --year: the *next* fiscal year end sits ~364 days out, inside the 400-day
    # window the TTM roll uses. Pulling an old 10-K must not pick it up and hand
    # back a later year's audited balance sheet labelled as this one's 10-Q.
    tg["Assets"]["units"]["USD"].append(
        {"end": "2025-12-27", "val": 1200e6, "form": "10-K", "filed": "2026-02-14"})
    back = build(tf, as_of="2024-12-28")["snapshot"]
    assert back["as_of"] == "2025-09-27", back["as_of"]  # Q3, not the next year end
    assert SNAPSHOT_MAX_GAP_DAYS < 364, "the window must stop short of the next fiscal year end"
    # And the balance sheet still has to reach the furthest legitimate quarter.
    assert (date.fromisoformat("2025-09-27") - date.fromisoformat("2024-12-28")).days \
        < SNAPSHOT_MAX_GAP_DAYS

    # --- a share count whose scale changes partway through ----------------
    # McDonald's shape: whole shares through 2020, millions from 2021. Net
    # income over diluted EPS is what settles which of the two is wrong.
    sc = {"facts": {"us-gaap": {}}}
    scg = sc["facts"]["us-gaap"]
    scg.update(dur("Revenues", {"2020-12-31": 19e9, "2021-12-31": 23e9}))
    scg.update(dur("NetIncomeLoss", {"2020-12-31": 4730.5e6, "2021-12-31": 7545.2e6}))
    scg.update(dur("EarningsPerShareDiluted", {"2020-12-31": 6.31, "2021-12-31": 10.04}))
    scg.update(dur("WeightedAverageNumberOfDilutedSharesOutstanding",
                   {"2020-12-31": 750.1e6, "2021-12-31": 751.8}))
    sb = build(sc)
    sv = sb["series"]["shares_diluted"]["values"]
    assert sv["2020-12-31"] == 750.1e6, sv                      # already whole shares
    assert abs(sv["2021-12-31"] - 751.8e6) < 1, sv              # 751.8 was millions
    assert sb["series"]["shares_diluted"]["rescaled"] == {"2021-12-31": 1e6}, \
        sb["series"]["shares_diluted"].get("rescaled")
    # The ratio that read -100% before is a real buyback figure now.
    assert sb["derived"]["2021-12-31"]["shares_change_pct"] == 0.2, \
        sb["derived"]["2021-12-31"]["shares_change_pct"]
    assert "different scale" in to_markdown(sb, "Scale Co")

    # A real reverse split must survive untouched: the count falls hard, but EPS
    # rises by the same factor, so the implied count falls with it.
    rs = {"facts": {"us-gaap": {}}}
    rsg = rs["facts"]["us-gaap"]
    rsg.update(dur("Revenues", {"2020-12-31": 1e9, "2021-12-31": 1.1e9}))
    rsg.update(dur("NetIncomeLoss", {"2020-12-31": 100e6, "2021-12-31": 110e6}))
    rsg.update(dur("EarningsPerShareDiluted", {"2020-12-31": 0.10, "2021-12-31": 11.0}))
    rsg.update(dur("WeightedAverageNumberOfDilutedSharesOutstanding",
                   {"2020-12-31": 1e9, "2021-12-31": 10e6}))
    rb = build(rs)
    assert rb["series"]["shares_diluted"]["values"]["2021-12-31"] == 10e6, "erased a split"
    assert "rescaled" not in rb["series"]["shares_diluted"]

    # --- every concept the filer tagged, not only the curated ones ---------
    scg["Goodwill"] = {"units": {"USD": [
        {"end": "2021-12-31", "val": 2.8e9, "form": "10-K", "filed": "2022-02-01"},
        {"end": "2021-06-30", "val": 9e9, "form": "10-Q", "filed": "2021-07-01"}]}}
    every = tagged_by_year(scg, sb["fiscal_year_ends"])
    assert every["Goodwill"]["type"] == "instant", every["Goodwill"]
    # the quarter-end instant is not a fiscal year end, so it never appears
    assert every["Goodwill"]["values"] == {"2021-12-31": 2.8e9}, every["Goodwill"]
    assert every["Revenues"]["type"] == "duration" and every["Revenues"]["unit"] == "USD"
    scg["NothingAtAYearEnd"] = {"units": {"USD": [
        {"end": "2019-06-30", "val": 1.0, "form": "10-K", "filed": "2019-07-01"}]}}
    assert "NothingAtAYearEnd" not in tagged_by_year(scg, sb["fiscal_year_ends"])

    # --- which filing a year belongs to ------------------------------------
    # The same year appears in three consecutive 10-Ks. Only the first is that
    # year's own filing, and only its cover page carries that year's float.
    restated = {"Revenues": {"units": {"USD": [
        {"start": "2023-01-01", "end": "2023-12-31", "val": 100e6, "form": "10-K",
         "filed": "2024-02-01", "accn": "fy2023"},
        {"start": "2023-01-01", "end": "2023-12-31", "val": 101e6, "form": "10-K",
         "filed": "2025-02-01", "accn": "fy2024"},
        {"start": "2023-01-01", "end": "2023-12-31", "val": 99e6, "form": "10-K/A",
         "filed": "2024-03-01", "accn": "amendment"},
    ]}}}
    assert filing_accessions(restated, ["2023-12-31"]) == {"2023-12-31": "fy2023"}
    assert filing_accessions(restated, ["2020-12-31"]) == {}

    # --- the spreadsheet ---------------------------------------------------
    csv_text = to_csv(build(tf), "TEST", "Test Co")
    header = csv_text.splitlines()[2].split(",")
    assert header[:3] == ["Metric", "Section", "Source"], header[:3]
    assert header[3] == "2023-12-30" and "LTM to 2025-09-27" in header[-1], header
    rev = [ln for ln in csv_text.splitlines() if ln.startswith("Revenue,")][0].split(",")
    assert rev[3] == "800000000.0" and rev[4] == "1000000000.0", rev
    assert rev[5] == "1070000000.0", "the LTM column must carry the roll-forward"
    # A figure the filer never tagged is an empty cell, not a zero — a zero
    # would average into every forecast built on the column.
    gross = [ln for ln in csv_text.splitlines() if ln.startswith("Gross margin %,")][0]
    assert gross.endswith(",,,"), gross
    # Paste-ready, checked on the value cells only: a spreadsheet reads "$1,234"
    # and "—" as text, and one text cell turns a whole column into text. Row
    # labels are prose and may say "%" all they like.
    import csv as _csv_check
    import io as _io_check
    values = [c for row in _csv_check.reader(_io_check.StringIO(csv_text))
              if len(row) > 3 and not row[0].startswith("#")
              for c in row[3:]]
    assert not any(c in v for v in values for c in "—$%,"), \
        [v for v in values if any(c in v for c in "—$%,")][:3]
    assert "" in values, "a missing figure must be an empty cell"

    print("ok: trends — duration/instant split, restatement pick, debt shapes, "
          "EBIT/EBITDA, TTM roll-forward, MRQ, balance-sheet snapshot, share-count "
          "scale breaks, every tagged concept, per-year accessions")


if __name__ == "__main__":
    demo()
