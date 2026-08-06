#!/usr/bin/env python3
"""Share count, market cap, the enterprise value bridge, and the multiples.

The share count is the part everyone gets wrong. Three different numbers are
all called "shares":

  1. **Cover page** — actual shares outstanding a few weeks after year end.
     This is what market cap should use, not the balance sheet count.
  2. **Weighted-average diluted** — the income statement denominator. It's an
     average over the year, so a company that bought back stock all year has a
     weighted-average well above where it actually ended.
  3. **Cover page + unvested RSUs + options outstanding** — fully diluted, from
     the share-based compensation footnote. The honest count for a company
     paying a lot of comp in stock. Note it can land below the weighted average
     if buybacks shrank the count faster than awards grew it.

All three get reported. Then:

    EV = market cap + total debt + preferred + minority interest − cash

Gotchas, each of which cost real debugging:

  - **The cover-page share fact is dimensioned for multi-class filers.** The
    XBRL companyfacts API only carries facts with no dimensions, so Berkshire's
    undimensioned `EntityCommonStockSharesOutstanding` is stale by fifteen years
    (2011!) while Class A and Class B live in dimensions the API drops. Fix:
    match the fact on the filing's own accession number, and when that finds
    nothing, read the rendered cover-page report, which lists each class.
  - **Class A and Class B are not interchangeable.** BRK.A is ~1,500x BRK.B, so
    summing the classes and applying one price is off by orders of magnitude.
    When one class dwarfs another by more than CLASS_GAP_LIMIT, this refuses to
    compute market cap and asks for `--shares`.
  - **Options and RSU counts are dimensioned too**, so companyfacts is useless
    for them — Apple's last undimensioned RSU count is from 2013. They come from
    the rendered footnote instead, whose share counts are scaled ("shares in
    Thousands" for Apple, "in Millions" for NVIDIA) in the table's unit header.
  - **The price comes out of the filing, not a quote feed.** Nothing in this
    tool touches live market data. `filing_price` below says what a 10-K
    actually contains by way of a share price and what that costs in accuracy.
"""

import json
import math
import re
import sys
from datetime import date

# A share class more than this many times bigger than another means a
# super-voting class (BRK.A vs BRK.B): one price across both would be nonsense.
CLASS_GAP_LIMIT = 20

# Reports to mine for the option/RSU counts. Deliberately narrow: the equity
# footnote ("Shares of Common Stock (Details)") also has an "ending balance"
# row, and that one is the 14.8 *billion* share count, not an award count.
AWARD_REPORT_RE = re.compile(
    r"(share.?based|stock.?based|equity (award|incentive|compensation)|restricted stock|"
    r"\brsus?\b|stock option|option activity|employee share)", re.I
)
# Same table, different rows: a weighted-average exercise price or a remaining
# contractual term sits under a label almost identical to the share count, and
# its cell often carries no currency symbol to give it away. Coca-Cola's
# "Outstanding on December 31, Weighted-Average Exercise Price" of $55.74 became
# 55,740,000 options before this existed.
NOT_A_COUNT_RE = re.compile(
    r"(per share|price|fair value|weighted.?average|contractual|remaining|intrinsic|"
    r"\bterm\b|\blife\b|expense|cost|\$)", re.I
)
# Roll-forward tables come first — that's where the closing count lives.
ROLLFORWARD_RE = re.compile(r"(activity|transaction|nonvested|unvested|outstanding|award)", re.I)
# The closing line of an award roll-forward. Anchored at the start of the label
# so "Exercisable on December 31" and "Vested and expected to vest" — smaller
# subsets of the same table — don't get mistaken for the total.
ENDING_ROW_RE = re.compile(
    r"^(ending balance|outstanding|nonvested|unvested|balance,? (at )?end|end of period)", re.I
)
# Roll-forwards open with the prior year's closing balance under a label that
# otherwise looks identical ("Outstanding, January 1").
OPENING_ROW_RE = re.compile(r"(january 1|jan\.? 1|beginning|start of|opening)", re.I)
OPTION_RE = re.compile(r"option", re.I)
SCALE_RE = re.compile(r"shares in (thousand|million|billion)s?", re.I)
SCALES = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


# --- share counts --------------------------------------------------------


def _number(cell):
    """'(401,672)' -> -401672.0. None for anything carrying a currency or a
    unit that means it isn't a share count."""
    text = str(cell).strip()
    if not text or "$" in text or "%" in text or "year" in text.lower():
        return None
    neg = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[(),]", "", text).split("|")[0].strip()
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if neg else value


def _parse_column_date(header):
    """'Sep. 27, 2025' -> date. Returns None for anything else."""
    m = re.search(r"([A-Za-z]{3})\w*\.?\s+(\d{1,2}),?\s+(\d{4})", str(header))
    if not m:
        return None
    month = MONTHS.get(m.group(1).lower())
    return date(int(m.group(3)), month, int(m.group(2))) if month else None


def _scale(units):
    m = SCALE_RE.search(units or "")
    return SCALES[m.group(1).lower()] if m else 1.0


def cover_shares_from_facts(companyfacts, accession):
    """Cover-page shares outstanding, matched to this exact filing.

    Matching on the accession rather than the newest 10-K row is what keeps a
    multi-class filer from silently handing back a decade-old number.
    """
    rows = companyfacts.get("facts", {}).get("dei", {}).get(
        "EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    hits = [r for r in rows if r.get("accn") == accession]
    if not hits:
        return None
    row = max(hits, key=lambda r: r["val"])
    return {
        "total": row["val"],
        "as_of": row["end"],
        "classes": [],
        "source": "XBRL cover-page fact (dei:EntityCommonStockSharesOutstanding)",
    }


def cover_shares_from_report(table):
    """Same thing off the rendered cover page, which keeps the share classes.

    Used when the fact is dimensioned by class and so absent from companyfacts.
    """
    scale = _scale(table["units"])
    dates = [_parse_column_date(c) for c in table["columns"]]
    classes, member = [], None
    for row in table["rows"]:
        label = row[0]
        if "[member]" in label.lower():
            member = re.sub(r"\s*\[member\]", "", label, flags=re.I).strip()
            continue
        if "shares outstanding" not in label.lower():
            continue
        for i, cell in enumerate(row[1:], start=1):
            value = _number(cell)
            if value:
                classes.append({
                    "name": member or "Common stock",
                    "shares": value * scale,
                    "as_of": dates[i].isoformat() if i < len(dates) and dates[i] else None,
                })
                break
    if not classes:
        return None
    return {
        "total": sum(c["shares"] for c in classes),
        "as_of": next((c["as_of"] for c in classes if c["as_of"]), None),
        "classes": classes,
        "source": "rendered cover page (share counts are tagged per class)",
    }


def rescale_to_cover(shares, cover_total):
    """-> (count, was_rescaled). Some filers tag the weighted-average share
    count in millions: McDonald's FY2025 value is literally 716.4, six orders
    of magnitude below its 710,398,642 cover-page count. Nothing real moves a
    share count by 1000x in a year, so a gap that large is a units error, and
    the fix is the power of ten that lines the two up.
    """
    if not shares or not cover_total or cover_total / shares < 1000:
        return shares, False
    return shares * 10 ** round(math.log10(cover_total / shares)), True


def class_gap(cover):
    """How lopsided the share classes are. 1.0 when there's only one."""
    counts = [c["shares"] for c in (cover or {}).get("classes", []) if c["shares"]]
    return max(counts) / min(counts) if len(counts) > 1 else 1.0


def award_counts(reports):
    """Options outstanding and unvested RSUs from the share-comp footnote.

    `reports` is [(short_name, parsed_table)]. Two rules, both crude on purpose:
    the largest qualifying row in a report wins, because one report often holds
    several roll-forwards (JPMorgan's RSUs sit above its much smaller SARs);
    and one row per report is then added up, because filers split RSUs and PSUs
    across separate tables and taking only the biggest would drop a whole class
    of awards.

    Every contributing row is returned alongside the picks. Filers label these a
    dozen different ways, and a printed table beats a confident wrong number.
    """
    found = []
    for short, table in reports:
        scale = _scale(table["units"])
        dates = [_parse_column_date(c) for c in table["columns"]]
        rows = []
        for row in table["rows"]:
            if not ENDING_ROW_RE.search(row[0]) or OPENING_ROW_RE.search(row[0]):
                continue
            if NOT_A_COUNT_RE.search(row[0]):
                continue
            for i, cell in enumerate(row[1:], start=1):
                value = _number(cell)
                if value and value > 0:
                    rows.append({
                        "report": short,
                        "label": row[0],
                        "shares": value * scale,
                        "as_of": dates[i].isoformat() if i < len(dates) and dates[i] else None,
                        "is_option": bool(OPTION_RE.search(row[0]) or OPTION_RE.search(short)),
                    })
                    break
        if rows:
            found.append(max(rows, key=lambda r: r["shares"]))

    def total(is_option):
        hits = [f["shares"] for f in found if f["is_option"] is is_option]
        return sum(hits) if hits else None

    return {
        "options_outstanding": total(True),
        "rsus_unvested": total(False),
        "rows": found,
    }


def _report_table(client, cik, accession_plain, rep):
    """Parse one rendered report, or None. One bad table never kills a run."""
    import statements

    try:
        return statements.parse_report(client.filing_file(cik, accession_plain, rep["file"]))
    except Exception as e:
        print("  warning: could not parse {} ({}): {}".format(rep["file"], rep["short"], e))
        return None


def gather(client, cik, filing, companyfacts, max_award_reports=4):
    """Network side: cover-page share count and the option/RSU footnote rows.

    Costs the FilingSummary (already cached from the statements pass) plus one
    fetch per award report, so about four requests.
    """
    import statements

    cover = cover_shares_from_facts(companyfacts, filing["accession"])
    reports = statements.parse_filing_summary(
        client.filing_file(cik, filing["accession_plain"], "FilingSummary.xml")
    )

    if cover is None:
        for rep in reports:
            low = rep["short"].lower()
            if "cover" in low or "document and entity information" in low:
                table = _report_table(client, cik, filing["accession_plain"], rep)
                cover = cover_shares_from_report(table) if table else None
                break

    # "(Detail" not "(Details)": McDonald's names its two award roll-forwards
    # "Summary of RSU Activity (Detail)", singular, and the whole footnote was
    # invisible until this stopped requiring the s.
    candidates = [
        rep for rep in reports
        if "(detail" in rep["short"].lower() and AWARD_REPORT_RE.search(rep["short"])
    ]
    # Roll-forwards first, so the cap never spends its fetches on the narrative
    # and expense tables while the activity table sits just past it.
    candidates.sort(key=lambda rep: not ROLLFORWARD_RE.search(rep["short"]))

    tables = []
    for rep in candidates[:max_award_reports]:
        table = _report_table(client, cik, filing["accession_plain"], rep)
        if table and table["rows"]:
            tables.append((rep["short"], table))
    return cover, award_counts(tables)


def public_float(companyfacts, accession):
    """The cover page's aggregate market value of non-affiliate common equity.

    Matched on the filing's own accession for the same reason the share count
    is: the newest row belongs to whatever the company filed last, which is not
    necessarily this 10-K.
    """
    rows = companyfacts.get("facts", {}).get("dei", {}).get(
        "EntityPublicFloat", {}).get("units", {}).get("USD", [])
    hits = [r for r in rows if r.get("accn") == accession]
    if not hits:
        return None
    row = max(hits, key=lambda r: r["val"])
    return {"value": row["val"], "as_of": row["end"]}


def filing_price(companyfacts, accession, cover):
    """The share price a 10-K actually contains. No quote feed, no live data.

    A 10-K states no closing price anywhere — the quarterly high/low table went
    away in 2018 and the performance graph is indexed to $100. What every cover
    page does state, and tags in XBRL, is two numbers:

        aggregate market value of common equity held by non-affiliates,
            measured on the last business day of the second fiscal quarter
        shares outstanding, counted a few weeks after fiscal year end

    Their quotient is a share price. Two things make it an approximation and
    both are printed rather than smoothed over:

      - **It excludes affiliate-held shares, so it is a floor.** Apple's
        affiliates hold a fraction of a percent and the implied price lands
        within about 1% of that day's close. Tesla's insiders hold ~13%, so its
        implied price comes in roughly a fifth low. The gap is the insider
        stake, and a filing does not disclose it (beneficial ownership is
        incorporated by reference from the proxy).
      - **The two numbers are stamped months apart**, so a filer that bought
        back stock between them divides by too few shares.

    Nothing better exists inside a 10-K. `--price` takes an outside number when
    one is wanted; without it, this is what the document supports.
    """
    pf = public_float(companyfacts, accession)
    shares = (cover or {}).get("total")
    if not pf or not shares:
        return {
            "price": None,
            "error": "the cover page's public float or share count isn't tagged in this filing",
        }
    # Same guard the market cap uses, for the same reason one step earlier: the
    # float is one number across both classes, so dividing it by A plus B prices
    # BRK.B at $649 when it trades near $485. A wrong price is worse than none.
    gap = class_gap(cover)
    if gap > CLASS_GAP_LIMIT:
        return {
            "price": None,
            "public_float": pf["value"],
            "float_as_of": pf["as_of"],
            "error": "share classes differ {:,.0f}x in size, so the cover page's single public "
                     "float figure divided by both classes is not either class's price — pass "
                     "--price".format(gap),
        }
    return {
        "price": pf["value"] / shares,
        "currency": "USD",
        "as_of": pf["as_of"],
        "source": "implied from the cover page: public float ÷ shares outstanding",
        "public_float": pf["value"],
        "float_as_of": pf["as_of"],
        "shares_as_of": cover.get("as_of"),
        "is_floor": True,
    }


# --- the bridge ----------------------------------------------------------


def _sum(*values):
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def build(series, derived, year, cover, awards, quote, shares_override=None):
    """-> the whole valuation dict for one fiscal year."""
    def val(name):
        return series.get(name, {}).get("values", {}).get(year)

    d = derived.get(year, {})
    wavg, wavg_rescaled = rescale_to_cover(
        val("shares_diluted"), cover["total"] if cover else None)
    fully_diluted = _sum(
        cover["total"] if cover else None,
        awards.get("options_outstanding"),
        awards.get("rsus_unvested"),
    ) if cover else None

    price = (quote or {}).get("price")
    gap = class_gap(cover)
    shares_for_cap, cap_basis, cap_warning = shares_override, "--shares override", None
    if shares_for_cap is None and cover:
        if gap > CLASS_GAP_LIMIT:
            cap_warning = (
                "share classes differ {:,.0f}x in size, so one price across both would be "
                "wrong by orders of magnitude — pass --shares with the count you want, or "
                "--price per class".format(gap)
            )
        else:
            shares_for_cap, cap_basis = cover["total"], "cover-page shares outstanding"
            if cover["classes"]:
                cap_warning = (
                    "{} share classes summed and priced at one quote; check whether they "
                    "actually trade at the same price".format(len(cover["classes"]))
                )

    market_cap = price * shares_for_cap if price and shares_for_cap else None
    debt, preferred = d.get("total_debt"), d.get("preferred")
    minority, cash = d.get("minority_interest"), val("cash")
    sti = val("short_term_investments")

    # Preferred and minority interest are missing because most companies have
    # none — treat those as zero. Debt and cash missing means the concept lookup
    # failed, and an EV built without them is off by hundreds of billions for a
    # bank. Refuse to print a number rather than print a confident wrong one.
    gaps = [name for name, v in (("total debt", debt), ("cash and equivalents", cash))
            if v is None]
    ev = _sum(market_cap, debt, preferred, minority, -cash if cash else None) \
        if market_cap and not gaps else None
    ev_net_investments = ev - sti if ev is not None and sti else None

    sales, ebit, ebitda = val("revenue"), d.get("ebit"), d.get("ebitda")
    fcf, eps = d.get("free_cash_flow"), val("eps_diluted")

    def over(num, den):
        return round(num / den, 1) if num is not None and den else None

    return {
        "fiscal_year_end": year,
        "shares": {
            "cover_page": cover,
            "weighted_average_diluted": wavg,
            "weighted_average_rescaled": wavg_rescaled,
            "options_outstanding": awards.get("options_outstanding"),
            "rsus_unvested": awards.get("rsus_unvested"),
            "fully_diluted": fully_diluted,
            "award_rows": awards.get("rows", []),
            "class_gap": gap,
        },
        "quote": quote,
        "market_cap": market_cap,
        "market_cap_basis": cap_basis if market_cap else None,
        "market_cap_warning": cap_warning,
        "bridge": {
            "market_cap": market_cap,
            "total_debt": debt,
            "debt_basis": d.get("debt_basis"),
            "preferred": preferred,
            "minority_interest": minority,
            "cash_and_equivalents": cash,
            "enterprise_value": ev,
            "missing_components": gaps,
            "short_term_investments": sti,
            "enterprise_value_net_of_investments": ev_net_investments,
            "finance_leases": val("finance_lease_liability"),
            "operating_leases": val("operating_lease_liability"),
        },
        "operating": {
            "sales": sales,
            "ebit": ebit,
            "ebit_basis": d.get("ebit_basis"),
            "ebitda": ebitda,
            "d_and_a": val("d_and_a"),
            "net_income": val("net_income"),
            "free_cash_flow": fcf,
            "eps_diluted": eps,
        },
        "multiples": {
            "ev_sales": over(ev, sales),
            "ev_ebit": over(ev, ebit),
            "ev_ebitda": over(ev, ebitda),
            "ev_fcf": over(ev, fcf),
            "pe": round(price / eps, 1) if price and eps else None,
            "pe_fully_diluted": round(market_cap / (val("net_income") or 0) * (
                fully_diluted / shares_for_cap), 1)
            if market_cap and val("net_income") and fully_diluted and shares_for_cap
            else None,
            "fcf_yield_pct": round(fcf / market_cap * 100, 1) if fcf and market_cap else None,
        },
    }


# --- rendering -----------------------------------------------------------


def _m(value):
    return "—" if value is None else "${:,.0f}M".format(value / 1e6)


def _sh(value):
    return "—" if value is None else "{:,.1f}M".format(value / 1e6)


def _x(value):
    return "—" if value is None else "{:.1f}x".format(value)


def to_markdown(v, company, ticker):
    s, b, o, m = v["shares"], v["bridge"], v["operating"], v["multiples"]
    cover = s["cover_page"]
    q = v["quote"] or {}
    lines = [
        "# {} ({}) — valuation, FY ended {}".format(company, ticker, v["fiscal_year_end"]),
        "",
        "Every figure below comes out of the 10-K. Nothing here is a live quote, so nothing "
        "moves after the filing was accepted — including the price.",
        "",
        "## Share count",
        "",
        "| Count | Shares | Where it comes from |",
        "|---|---|---|",
        "| Cover page, shares outstanding | {} | {} |".format(
            _sh(cover["total"] if cover else None)
            + (" across {} classes".format(len(cover["classes"]))
               if cover and len(cover["classes"]) > 1 else ""),
            "as of {} — {}".format(cover["as_of"], cover["source"]) if cover else "not found",
        ),
        "| Weighted-average diluted | {} | income statement denominator, averaged over the year{} |".format(
            _sh(s["weighted_average_diluted"]),
            " — **tagged in millions by this filer, rescaled to match the cover page**"
            if s.get("weighted_average_rescaled") else ""),
        "| + options outstanding | {} | share-based compensation footnote |".format(
            _sh(s["options_outstanding"])),
        "| + unvested RSUs | {} | share-based compensation footnote |".format(
            _sh(s["rsus_unvested"])),
        "| **Fully diluted** | **{}** | cover page + options + unvested RSUs |".format(
            _sh(s["fully_diluted"])),
        "",
        "Fully diluted is the gross count — it does not net off the cash an option holder "
        "pays on exercise, the way the treasury-stock method does, so it is the ceiling on "
        "dilution from awards already granted. It can still come in *below* the "
        "weighted-average diluted count: that average spans the whole year, so a company "
        "that bought back stock all year ended it with fewer shares than it averaged.",
        "",
    ]
    if cover and cover["classes"]:
        lines += ["Share classes on the cover page:", ""]
        lines += ["- {}: {} shares".format(c["name"], "{:,.0f}".format(c["shares"]))
                  for c in cover["classes"]]
        lines.append("")
    if s["award_rows"]:
        lines += [
            "Every roll-forward closing row found in the footnote, so the picks above can be "
            "checked against what was actually filed:",
            "",
            "| Report | Row | Shares |",
            "|---|---|---|",
        ]
        # Rendered row labels carry the XBRL unit after a pipe ("Ending balance
        # (in shares) | shares"), which would open a phantom table column.
        lines += ["| {} | {} | {} |".format(
            r["report"].replace("|", "\\|"), r["label"].replace("|", "\\|"), _sh(r["shares"]))
            for r in s["award_rows"]]
        lines.append("")
    else:
        lines += [
            "_No option or RSU roll-forward rows found in the rendered footnote, so fully "
            "diluted above is missing that piece. Read `sections/item8_financial_statements.md` "
            "for the share-based compensation note._",
            "",
        ]

    lines += [
        "## Enterprise value",
        "",
        "| | |",
        "|---|---|",
        "| Price | {} |".format(
            "—" if not q.get("price") else "${:,.2f} ({}) — {}".format(
                q["price"], q.get("as_of") or "date not stated", q.get("source", ""))),
        "| × shares | {} |".format(_sh(
            v["market_cap"] / q["price"] if v["market_cap"] and q.get("price") else None)),
        "| **Market cap** | **{}** |".format(_m(v["market_cap"])),
        "| + total debt | {} |".format(_m(b["total_debt"])),
        "| + preferred stock | {} |".format(_m(b["preferred"])),
        "| + minority interest | {} |".format(_m(b["minority_interest"])),
        "| − cash and equivalents | {} |".format(_m(b["cash_and_equivalents"])),
        "| **Enterprise value** | **{}** |".format(_m(b["enterprise_value"])),
        "",
    ]
    if q.get("is_floor"):
        lines += [
            "> **This price is the filing's own, and it is a floor.** A 10-K states no closing "
            "price. It does state, on the cover, the aggregate market value of common equity "
            "held by non-affiliates — {} on {} — and the share count above. Dividing gives the "
            "price used here. Because affiliate-held shares are excluded, a company whose "
            "insiders own a real stake prices low by roughly that percentage, and the two "
            "numbers are stamped months apart ({} vs {}). Market cap above is therefore the "
            "public float restated. Pass `--price` to value it at an outside number instead."
            .format(_m(q.get("public_float")), q.get("float_as_of") or "the cover date",
                    q.get("float_as_of") or "?", q.get("shares_as_of") or "?"),
            "",
        ]
    if v.get("market_cap_warning"):
        lines += ["> **{}**".format(v["market_cap_warning"]), ""]
    if not v["market_cap"] and not v.get("market_cap_warning"):
        lines += [
            "> No price, so no market cap and no EV{}. Pass `--price` to supply one.".format(
                " — " + q["error"] if q.get("error") else ""),
            "",
        ]
    if b["missing_components"]:
        lines += [
            "> **No enterprise value: this filer doesn't tag {} in XBRL.** The bridge would be "
            "short by that much, so it is left blank rather than printed wrong. Banks and "
            "insurers are the usual case, and EV means little for them anyway — their debt is "
            "raw material, not financing. Read `statements/balance_sheet.md` for the real "
            "figures.".format(" or ".join(b["missing_components"])),
            "",
        ]
    lines += [
        "Memo lines — not in the EV above, but standard adjustments to argue about:",
        "",
        "- Short-term investments: {} (EV net of them: {})".format(
            _m(b["short_term_investments"]), _m(b["enterprise_value_net_of_investments"])),
        "- Finance lease liabilities: {}".format(_m(b["finance_leases"])),
        "- Operating lease liabilities: {} — capitalized leases are debt to some analysts "
        "and rent to others; they are excluded above".format(_m(b["operating_leases"])),
        "",
        "Total debt {}.".format(b.get("debt_basis") or "could not be built from tagged concepts"),
        "",
        "## Operating figures and multiples",
        "",
        "| Metric | Value | Multiple |",
        "|---|---|---|",
        "| Sales | {} | EV/Sales {} |".format(_m(o["sales"]), _x(m["ev_sales"])),
        "| EBIT | {} | EV/EBIT {} |".format(_m(o["ebit"]), _x(m["ev_ebit"])),
        "| EBITDA | {} | EV/EBITDA {} |".format(_m(o["ebitda"]), _x(m["ev_ebitda"])),
        "| D&A | {} | |".format(_m(o["d_and_a"])),
        "| Free cash flow | {} | EV/FCF {} |".format(_m(o["free_cash_flow"]), _x(m["ev_fcf"])),
        "| Net income | {} | P/E {} |".format(_m(o["net_income"]), _x(m["pe"])),
        "| EPS (diluted) | {} | FCF yield {} |".format(
            "—" if o["eps_diluted"] is None else "${:,.2f}".format(o["eps_diluted"]),
            "—" if m["fcf_yield_pct"] is None else "{}%".format(m["fcf_yield_pct"])),
        "",
        "EBIT is {}. EBITDA adds back D&A from the cash flow statement.".format(
            o.get("ebit_basis") or "not available — neither operating income nor "
            "pretax income plus interest expense was tagged"),
        "",
    ]
    if m["pe_fully_diluted"]:
        lines += [
            "P/E on the fully diluted count instead of the reported one: {}.".format(
                _x(m["pe_fully_diluted"])),
            "",
        ]
    return "\n".join(lines)


# --- self-check ----------------------------------------------------------


def demo():
    """python3 valuation.py — offline checks. Nothing here touches the network."""
    assert _number("(401,672)") == -401672
    assert _number("$ 189.75") is None, "a per-share price is not a share count"
    assert _number("2 years 6 months") is None
    assert _parse_column_date("Sep. 27, 2025") == date(2025, 9, 27)
    assert _scale("Cover Page - USD ($) shares in Thousands, $ in Millions") == 1e3
    assert _scale("Equity Incentive Plans (Details) shares in Millions") == 1e6
    assert _scale("no hint here") == 1.0

    # The cover fact must match the filing's accession, not just the newest row.
    facts = {"facts": {"dei": {
        "EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2011-02-17", "val": 943242, "accn": "old", "form": "10-K"},
            {"end": "2025-10-17", "val": 14776353000, "accn": "right", "form": "10-K"},
        ]}},
        "EntityPublicFloat": {"units": {"USD": [
            {"end": "2024-03-29", "val": 2.6e12, "accn": "old", "form": "10-K"},
            {"end": "2025-03-28", "val": 3253.4e9, "accn": "right", "form": "10-K"},
        ]}},
    }}}
    assert cover_shares_from_facts(facts, "right")["total"] == 14776353000
    assert cover_shares_from_facts(facts, "missing") is None, "dimensioned filer must fall through"

    # The price is the cover page's own arithmetic, matched to this filing.
    cov = cover_shares_from_facts(facts, "right")
    px = filing_price(facts, "right", cov)
    assert round(px["price"], 2) == round(3253.4e9 / 14776353000, 2), px
    assert px["as_of"] == "2025-03-28" and px["is_floor"] is True, px
    assert public_float(facts, "old")["value"] == 2.6e12, "float must match its own filing"
    # No float tagged, or no share count: a missing price, never a guessed one.
    assert filing_price(facts, "nothing", cov)["price"] is None
    assert filing_price(facts, "right", None)["price"] is None
    # And a two-class filer gets no implied price at all: one float across BRK.A
    # and BRK.B divided by both is not either one's price.

    # Berkshire's shape: two classes, 2,700x apart, only on the rendered page.
    cover_table = {
        "units": "Document and Entity Information - USD ($)",
        "columns": ["Line item", "Dec. 31, 2025", "Jan. 31, 2026"],
        "rows": [
            ["Entity Public Float", "", "$ 902,700,000,000"],
            ["Common Class A [Member]", "", ""],
            ["Entity Common Stock, Shares Outstanding", "", "511820.0"],
            ["Common Class B [Member]", "", ""],
            ["Entity Common Stock, Shares Outstanding", "", "1389605139.0"],
        ],
    }
    cover = cover_shares_from_report(cover_table)
    assert [c["name"] for c in cover["classes"]] == ["Common Class A", "Common Class B"]
    assert cover["total"] == 511820 + 1389605139
    assert class_gap(cover) > CLASS_GAP_LIMIT, "super-voting class must trip the guard"
    two_class = filing_price(facts, "right", cover)
    assert two_class["price"] is None and "--price" in two_class["error"], two_class

    # Apple's RSU roll-forward: counts in thousands, and a per-share column
    # right underneath that must not be mistaken for one.
    rsu = {
        "units": "Share-Based Compensation - Restricted Stock Unit Activity (Details) - "
                 "Restricted stock units - $ / shares shares in Thousands",
        "columns": ["Line item", "Sep. 27, 2025", "Sep. 28, 2024"],
        "rows": [
            ["Beginning balance (in shares)", "163326", ""],
            ["RSUs vested (in shares)", "(76,845)", ""],
            ["Ending balance (in shares)", "151574", "163326"],
            ["Ending balance (in dollars per share)", "$ 189.75", "$ 158.73"],
        ],
    }
    # Coca-Cola's option table: subsets of the same balance sit *below* the
    # closing row, so "last row wins" alone would return the exercisable count.
    opts = {
        "units": "Stock Option Activity (Details) shares in Millions",
        "columns": ["Line item", "Dec. 31, 2025"],
        "rows": [
            ["Granted (in shares)", "3"],
            ["Outstanding on December 31", "27"],
            ["Vested and expected to vest", "26"],
            ["Exercisable on December 31, 2025", "20"],
        ],
    }
    awards = award_counts([("RSU Activity (Details)", rsu), ("Stock Option Activity (Details)", opts)])
    assert awards["rsus_unvested"] == 151574e3, awards["rsus_unvested"]
    assert awards["options_outstanding"] == 27e6, awards["options_outstanding"]
    assert len(awards["rows"]) == 2, "one closing row per report, per-share rows dropped"

    # Costco labels the opening balance with the prior year ("Outstanding at
    # the end of 2024"), so the closing row is the last match, not the first.
    cost = {
        "units": "Summary of RSU Transactions (Details) - $ / shares shares in Thousands",
        "columns": ["Line item", "Aug. 31, 2025", "Sep. 01, 2024"],
        "rows": [
            ["Outstanding at the end of 2024", "2799", ""],
            ["Granted", "1095", ""],
            ["Outstanding at the end of 2025", "2308", "2799"],
            ["Outstanding at the end of 2025", "$ 597.00", "$ 463.24"],
        ],
    }
    # JPMorgan's opens with "Outstanding, January 1" — same word, opening balance.
    jpm = {
        "units": "RSUs, PSUs and SARs Activity (Details) - USD ($)",
        "columns": ["Line item", "Dec. 31, 2025"],
        "rows": [
            ["Outstanding, January 1 (in shares)", "50609000"],
            ["Outstanding, December 31 (in shares)", "42560000"],
        ],
    }
    split = award_counts([("RSU Transactions (Details)", cost), ("SARs Activity (Details)", jpm)])
    # One row per report, added: two filers' worth of tables here, but the same
    # shape as one filer splitting RSUs and PSUs across two tables.
    assert split["rsus_unvested"] == 2799e3 + 42560000, split["rsus_unvested"]
    assert [r["shares"] for r in split["rows"]] == [2799e3, 42560000], split["rows"]

    series = {
        "cash": {"values": {"2025-09-27": 30e9}},
        "short_term_investments": {"values": {"2025-09-27": 20e9}},
        "revenue": {"values": {"2025-09-27": 400e9}},
        "net_income": {"values": {"2025-09-27": 100e9}},
        "eps_diluted": {"values": {"2025-09-27": 6.5}},
        "shares_diluted": {"values": {"2025-09-27": 15.2e9}},
        "d_and_a": {"values": {"2025-09-27": 12e9}},
        "finance_lease_liability": {"values": {"2025-09-27": 1e9}},
        "operating_lease_liability": {"values": {"2025-09-27": 12e9}},
    }
    derived = {"2025-09-27": {
        "total_debt": 100e9, "preferred": 5e9, "minority_interest": 2e9,
        "ebit": 130e9, "ebitda": 142e9, "free_cash_flow": 90e9,
        "debt_basis": "built from components", "ebit_basis": "operating income",
    }}
    v = build(series, derived, "2025-09-27",
              cover_shares_from_facts(facts, "right"), awards,
              {"price": 200.0, "as_of": "2026-08-05"})
    assert v["market_cap"] == 200.0 * 14776353000
    # market cap 2,955.3bn + 100 + 5 + 2 − 30 = 3,032.3bn
    assert round(v["bridge"]["enterprise_value"] / 1e9, 1) == 3032.3, v["bridge"]
    assert v["bridge"]["enterprise_value_net_of_investments"] == v["bridge"]["enterprise_value"] - 20e9
    assert v["multiples"]["ev_ebitda"] == round(v["bridge"]["enterprise_value"] / 142e9, 1)
    assert v["shares"]["fully_diluted"] == 14776353000 + 27e6 + 151574e3
    md = to_markdown(v, "Test Co", "TEST")
    assert "Enterprise value" in md and "EV/EBITDA" in md
    piped = award_counts([("Plans (Details)", {
        "units": "Plans (Details) shares in Millions",
        "columns": ["Line item", "Jan. 25, 2026"],
        "rows": [["Ending balance (in shares) | shares", "189"]],
    })])
    row = to_markdown(build(series, derived, "2025-09-27", None, piped, {"price": None}),
                      "Test Co", "TEST")
    assert "(in shares) \\| shares |" in row, "an unescaped pipe splits the table"

    # A multi-class filer must refuse a market cap rather than print a wrong one.
    blocked = build(series, derived, "2025-09-27", cover, awards, {"price": 500.0})
    assert blocked["market_cap"] is None and blocked["market_cap_warning"]
    assert blocked["bridge"]["enterprise_value"] is None
    assert "--shares" in to_markdown(blocked, "Berkshire", "BRK-B")

    # McDonald's tags its weighted-average diluted count in millions (716.4).
    assert rescale_to_cover(716.4, 710398642) == (716400000.0, True)
    assert rescale_to_cover(15004697000, 14776353000)[1] is False, "a real count must not move"
    assert rescale_to_cover(None, 100) == (None, False)
    assert v["shares"]["weighted_average_rescaled"] is False

    # A bank tags no debt, so the bridge must refuse rather than print market
    # cap plus preferred and call it enterprise value.
    thin = build(series, {"2025-09-27": dict(derived["2025-09-27"], total_debt=None)},
                 "2025-09-27", cover_shares_from_facts(facts, "right"), awards,
                 {"price": 200.0})
    assert thin["market_cap"] and thin["bridge"]["enterprise_value"] is None
    assert thin["bridge"]["missing_components"] == ["total debt"]
    assert thin["multiples"]["ev_ebitda"] is None
    assert "doesn't tag total debt" in to_markdown(thin, "Bank Co", "BANK")

    # No price is a missing row, not a crash.
    nopx = build(series, derived, "2025-09-27",
                 cover_shares_from_facts(facts, "right"), awards, {"price": None})
    assert nopx["market_cap"] is None and nopx["multiples"]["ev_ebitda"] is None
    assert nopx["operating"]["ebitda"] == 142e9, "operating figures survive a missing price"

    # The float-implied price prints its own caveat, or the run reads as if a
    # closing quote had been used.
    floored = build(series, derived, "2025-09-27", cover_shares_from_facts(facts, "right"),
                    awards, px)
    assert "it is a floor" in to_markdown(floored, "Test Co", "TEST")

    print("ok: valuation — cover/class split, footnote scaling, filing price, EV bridge")


if __name__ == "__main__":
    demo()
    if "--dump" in sys.argv:
        print(json.dumps({"note": "run extract_10k.py for a real filing"}, indent=2))
