#!/usr/bin/env python3
"""As-filed financial statements, straight from the filing's own rendered tables.

Every XBRL filing ships a FilingSummary.xml listing ~70 pre-rendered report
files (R1.htm, R2.htm, ...). R3/R5/R8 are typically the income statement,
balance sheet and cash flow statement — with the company's own line-item
labels and ordering, which is strictly better than reassembling them from raw
XBRL concepts and guessing at the presentation order.

Two gotchas, both hit while building this:
  - Each R#.htm contains extra tables for the XBRL element-definition pop-ups
    (AAPL's R3 has 21 tables). "Take the biggest table" looks right and passes
    on the income statement, then quietly returns tagging metadata
    ("Namespace Prefix:", "Data Type:") for the small Auditor Information
    report, whose pop-up is larger than the report itself. The statement is
    always rendered first, so take table 0.
  - Columns come back as a 2-level MultiIndex ("12 Months Ended" / "Sep. 27,
    2025"). The date is the last level.
"""

import io
import re
import warnings
from datetime import date

import pandas as pd

warnings.filterwarnings("ignore", message="Passing literal html")

REPORT_RE = re.compile(r"<Report[^>]*>(.*?)</Report>", re.S | re.I)

# ShortName keyword -> stable output filename, so income_statement.md is always
# income_statement.md regardless of what the company calls it.
CANONICAL = [
    # "operations" first: some filers render one combined report, "...Statements
    # of Operations and Comprehensive Income" — that title also matches
    # "comprehensive income" below, and checking that first mis-filed the whole
    # income statement (revenue, opex, net income) as comprehensive_income.md,
    # leaving nothing named income_statement for later lookups (e.g. the
    # membership-fee line search) to find. A standalone comprehensive-income
    # statement is never titled with "operations", so this ordering doesn't
    # touch that case.
    ("operations", "income_statement"),
    ("comprehensive income", "comprehensive_income"),
    ("cash flow", "cash_flow"),
    ("balance sheet", "balance_sheet"),
    ("financial position", "balance_sheet"),
    ("stockholders' equity", "equity"),
    ("shareholders' equity", "equity"),
    ("equity", "equity"),
    ("income", "income_statement"),
    ("earnings", "income_statement"),
]


def _tag(block, name):
    m = re.search(r"<{0}>(.*?)</{0}>".format(name), block, re.S | re.I)
    return m.group(1).strip() if m else ""


def parse_filing_summary(xml):
    """-> list of {short, long, file, kind}. kind is 'statement', 'disclosure'
    or 'other' (cover page, auditor info)."""
    out = []
    for block in REPORT_RE.findall(xml):
        fname = _tag(block, "HtmlFileName") or _tag(block, "XmlFileName")
        if not fname.endswith(".htm"):
            continue
        long_name = _tag(block, "LongName")
        lowered = long_name.lower()
        kind = "statement" if "- statement -" in lowered else (
            "disclosure" if "- disclosure -" in lowered else "other"
        )
        out.append(
            {"short": _tag(block, "ShortName"), "long": long_name, "file": fname, "kind": kind}
        )
    return out


def canonical_name(short_name):
    """Stable filename for a statement, or None to fall back to a slug."""
    low = short_name.lower()
    if "parenthetical" in low:
        return None  # par values and share counts; keep but don't claim a canonical slot
    for keyword, name in CANONICAL:
        if keyword in low:
            return name
    return None


def slug(text):
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")[:60]


def _is_definition_popup(df):
    """XBRL element-definition pop-ups look like a statement to read_html but
    are pure tagging metadata."""
    first_col = {str(v).strip() for v in df.iloc[:, 0]} if df.shape[1] else set()
    return bool(first_col & {"Namespace Prefix:", "Data Type:", "Balance Type:", "Period Type:"})


def parse_report(html):
    """R#.htm -> {units, columns, rows}. The statement is the first table;
    everything after it is element-definition pop-ups."""
    tables = [t for t in pd.read_html(io.StringIO(html)) if not _is_definition_popup(t)]
    if not tables:
        return None
    df = tables[0]

    # The first header level is the period length ("12 Months Ended", "3 Months
    # Ended"), and it is the only thing separating two columns that otherwise
    # look identical: Costco's 2017 income statement has "Sep. 03, 2017" twice,
    # once for the fourth quarter and once for the year, with different numbers
    # under each. Dropping the level, as this used to, makes the two
    # indistinguishable — to a reader of the markdown and to line_value alike.
    if isinstance(df.columns, pd.MultiIndex):
        headers = [str(c[-1]) for c in df.columns]
        periods = [str(c[0]) for c in df.columns]
        units = str(df.columns[0][0])
    else:
        headers = [str(c) for c in df.columns]
        periods = [""] * len(headers)
        units = headers[0]
    # Column 0's header repeats the table title; the periods start at column 1.
    columns = ["Line item"] + headers[1:]
    periods = [""] + periods[1:]
    if len(set(periods[1:])) > 1:
        columns = ["Line item"] + [
            "{} ({})".format(h, p) for h, p in zip(headers[1:], periods[1:])
        ]

    rows = []
    for _, row in df.iterrows():
        cells = ["" if pd.isna(v) else re.sub(r"\s+", " ", str(v)).strip() for v in row]
        if not cells or not cells[0]:
            continue
        rows.append(cells)
    return {
        "units": re.sub(r"\s+", " ", units).strip(),
        "columns": columns,
        "periods": periods,
        "rows": rows,
    }


# "Consolidated Statements Of Income - USD ($) shares in Thousands, $ in Millions"
DOLLAR_SCALE_RE = re.compile(r"\$ in (thousand|million|billion)s?", re.I)
DOLLAR_SCALES = {"thousand": 1e3, "million": 1e6, "billion": 1e9}


def _money(cell):
    """'$ 5,323' -> 5323.0, '(154)' -> -154.0. None for anything non-numeric."""
    text = str(cell).strip()
    if not text or "%" in text:
        return None
    neg = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[(),$\s]", "", text)
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if neg else value


PERIOD_MONTHS_RE = re.compile(r"(\d+)\s*months?", re.I)
COLUMN_DATE_RE = re.compile(r"([A-Za-z]{3})\w*\.?\s+(\d{1,2}),?\s+(\d{4})")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _period_months(label):
    """'8 Months Ended' -> 8. 0 when the header states no period."""
    m = PERIOD_MONTHS_RE.search(str(label or ""))
    return int(m.group(1)) if m else 0


def _column_date(header):
    """'May 10, 2026 (8 Months Ended)' -> date(2026, 5, 10). None if undated."""
    m = COLUMN_DATE_RE.search(str(header))
    month = MONTHS.get(m.group(1).lower()) if m else None
    return date(int(m.group(3)), month, int(m.group(2))) if month else None


def _period_columns(table):
    """Column indices, longest reporting period first and newest date first.

    A filer that shows quarters alongside the year renders the quarters *first*
    — Costco's 2017 income statement opens with seven 3-month columns and puts
    the twelve-month ones at index 9. Taking the leftmost number there returns a
    quarter's membership fees ($644M) in place of the year's ($2,853M).

    "Longest", not "twelve months": a 10-Q's cumulative column is labelled for
    however far into the year it reaches — Costco's Q3 says "8 Months Ended" —
    so a rule that matched 12 or 13 months only would fall through to the
    leftmost column and hand back a single quarter as if it were the period.
    """
    periods = table.get("periods") or []
    order = list(range(1, len(table["columns"])))
    months = {i: _period_months(periods[i]) if i < len(periods) else 0 for i in order}
    longest = max(months.values(), default=0)
    if not longest:
        return order
    ranked = [i for i in order if months[i] == longest]
    dates = {i: _column_date(table["columns"][i]) for i in ranked}
    if all(dates.values()):
        ranked.sort(key=lambda i: dates[i], reverse=True)
    return ranked + [i for i in order if months[i] != longest]


def line_periods(table, pattern, lookahead=3):
    """-> [{column, months, value}] — every numeric cell on the first row
    matching `pattern`, longest period and newest date first.

    For revenue split by product or service — Costco's membership fees, and any
    other line the filer tags along a dimension — the rendered statement is the
    only source. XBRL companyfacts carries undimensioned facts only, so those
    lines are simply absent from it.

    The rendering puts the dimension on a row of its own with no numbers
    ("Membership fees"), then repeats the statement's own labels underneath it
    ("REVENUE", "Total revenue | $ 5,323"). So a match walks forward a few rows
    until it finds numbers rather than giving up on the empty label row.

    Every cell, not just the first, because a 10-Q states the current and
    prior-year periods side by side — which is exactly the pair an LTM roll
    needs, and it comes out of one table.
    """
    if not table:
        return []
    m = DOLLAR_SCALE_RE.search(table.get("units", ""))
    scale = DOLLAR_SCALES[m.group(1).lower()] if m else 1.0
    rows, cols = table["rows"], _period_columns(table)
    periods = table.get("periods") or []
    for i, row in enumerate(rows):
        if not pattern.search(row[0]):
            continue
        for candidate in rows[i:i + lookahead + 1]:
            found = [
                {
                    "column": table["columns"][col],
                    "months": _period_months(periods[col]) if col < len(periods) else 0,
                    "value": _money(candidate[col]) * scale,
                }
                for col in cols
                if col < len(candidate) and _money(candidate[col]) is not None
            ]
            if found:
                return found
    return []


def line_value(table, pattern, lookahead=3):
    """-> (value in dollars, column header) for the first row matching `pattern`."""
    found = line_periods(table, pattern, lookahead)
    return (found[0]["value"], found[0]["column"]) if found else (None, None)


def to_markdown(title, table):
    """Small markdown table writer — `tabulate` isn't installed and this is
    eight lines."""
    cols = table["columns"]
    lines = ["# {}".format(title), "", "_{}_".format(table["units"]), ""]
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("|" + "|".join(["---"] * len(cols)) + "|")
    for row in table["rows"]:
        cells = (row + [""] * len(cols))[: len(cols)]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return "\n".join(lines) + "\n"


def fetch_statements(client, cik, accession_plain, include_disclosures=False):
    """-> (list of {name, title, table}, auditor dict or None)."""
    xml = client.filing_file(cik, accession_plain, "FilingSummary.xml")
    reports = parse_filing_summary(xml)

    wanted = [r for r in reports if r["kind"] == "statement"]
    if include_disclosures:
        wanted += [r for r in reports if r["kind"] == "disclosure"]

    auditor = None
    used = set()
    results = []
    for rep in reports:
        # Apple calls this report "Auditor Information", NVIDIA "Audit
        # Information". Match on both words, not the exact phrase.
        low = rep["short"].lower()
        if "audit" in low and "information" in low:
            try:
                table = parse_report(client.filing_file(cik, accession_plain, rep["file"]))
            except Exception as e:  # same rule as the loop below: don't kill the run over one report
                print("  warning: could not parse {} ({}): {}".format(rep["file"], rep["short"], e))
                break
            if table:
                auditor = {r[0]: r[1] for r in table["rows"] if len(r) > 1 and r[1]}
            break

    for rep in wanted:
        try:
            table = parse_report(client.filing_file(cik, accession_plain, rep["file"]))
        except Exception as e:  # one unparseable table shouldn't kill the run
            print("  warning: could not parse {} ({}): {}".format(rep["file"], rep["short"], e))
            continue
        if not table or not table["rows"]:
            continue
        name = canonical_name(rep["short"]) or slug(rep["short"])
        while name in used:  # two statements mapping to the same slot
            name += "_2"
        used.add(name)
        results.append({"name": name, "title": rep["short"], "table": table})
    return results, auditor


def demo():
    """python3 statements.py — offline checks."""
    xml = """<FilingSummary><MyReports>
      <Report><ShortName>Cover Page</ShortName>
        <LongName>0000001 - Document - Cover Page</LongName><HtmlFileName>R1.htm</HtmlFileName></Report>
      <Report><ShortName>CONSOLIDATED STATEMENTS OF OPERATIONS</ShortName>
        <LongName>995 - Statement - CONSOLIDATED STATEMENTS OF OPERATIONS</LongName>
        <HtmlFileName>R3.htm</HtmlFileName></Report>
      <Report><ShortName>Revenue</ShortName>
        <LongName>996 - Disclosure - Revenue</LongName><HtmlFileName>R10.htm</HtmlFileName></Report>
    </MyReports></FilingSummary>"""
    reps = parse_filing_summary(xml)
    assert [r["kind"] for r in reps] == ["other", "statement", "disclosure"], reps
    assert canonical_name("CONSOLIDATED STATEMENTS OF OPERATIONS") == "income_statement"
    assert canonical_name("CONSOLIDATED BALANCE SHEETS") == "balance_sheet"
    assert canonical_name("CONSOLIDATED STATEMENTS OF CASH FLOWS") == "cash_flow"
    assert canonical_name("CONSOLIDATED STATEMENTS OF COMPREHENSIVE INCOME") == "comprehensive_income"

    # The statement comes first; a LARGER element-definition pop-up follows it.
    # This is the real R2.htm shape that broke "take the biggest table".
    html = """<html>
      <table>
        <tr><th>INCOME - $ in Millions</th><th>12 Months Ended</th><th>12 Months Ended</th></tr>
        <tr><th>INCOME - $ in Millions</th><th>Sep. 27, 2025</th><th>Sep. 28, 2024</th></tr>
        <tr><td>Net sales</td><td>416161</td><td>391035</td></tr>
        <tr><td>Net income</td><td>112010</td><td>93736</td></tr>
      </table>
      <table>
        <tr><th>a</th><th>b</th></tr>
        <tr><td>Name:</td><td>us-gaap_Revenues</td></tr>
        <tr><td>Namespace Prefix:</td><td>us-gaap_</td></tr>
        <tr><td>Data Type:</td><td>monetaryItemType</td></tr>
        <tr><td>Balance Type:</td><td>credit</td></tr>
        <tr><td>Period Type:</td><td>duration</td></tr>
      </table></html>"""
    t = parse_report(html)
    assert t["columns"] == ["Line item", "Sep. 27, 2025", "Sep. 28, 2024"], t["columns"]
    assert t["rows"][0] == ["Net sales", "416161", "391035"], t["rows"][0]
    assert "$ in Millions" in t["units"], t["units"]
    md = to_markdown("Income Statement", t)
    assert "| Net income | 112010 | 93736 |" in md, md

    # A dimensioned revenue line, Costco's shape: the dimension label carries no
    # numbers and the value sits two rows below it, scaled by the unit header.
    cost = {
        "units": "Consolidated Statements Of Income - USD ($) shares in Thousands, $ in Millions",
        "columns": ["Line item", "Aug. 31, 2025", "Sep. 01, 2024"],
        "rows": [
            ["Total revenue", "$ 275,235", "$ 254,453"],
            ["Interest expense", "(154)", "(169)"],
            ["Membership fees", "", ""],
            ["REVENUE", "", ""],
            ["Total revenue", "$ 5,323", "$ 4,828"],
        ],
    }
    value, col = line_value(cost, re.compile(r"membership\s+(fee|due)", re.I))
    assert value == 5323e6 and col == "Aug. 31, 2025", (value, col)
    assert line_value(cost, re.compile(r"nothing here", re.I)) == (None, None)
    assert _money("(154)") == -154.0 and _money("$ 5,323") == 5323.0 and _money("") is None

    # Costco's 2017 shape: seven quarterly columns rendered *before* the annual
    # ones. Leftmost-number-wins returns a quarter's fees instead of the year's.
    mixed = {
        "units": "Income - USD ($) $ in Millions",
        "columns": ["Line item", "May 07, 2017 (3 Months Ended)",
                    "Sep. 03, 2017 (4 Months Ended)", "Sep. 03, 2017 (12 Months Ended)"],
        "periods": ["", "3 Months Ended", "4 Months Ended", "12 Months Ended"],
        "rows": [["Membership fees", "", "", ""], ["Total revenue", "644", "943", "2,853"]],
    }
    value, col = line_value(mixed, re.compile(r"membership\s+fee", re.I))
    assert value == 2853e6 and "12 Months" in col, (value, col)

    # A 10-Q's real shape (Costco Q3 FY2026): two discrete-quarter columns and
    # two cumulative ones, the cumulative pair labelled "8 Months Ended" rather
    # than 12. The cumulative pair is what an LTM roll differences; picking the
    # leftmost column instead would substitute one quarter for eight months.
    tenq = {
        "units": "Condensed Consolidated Statements Of Income - USD ($) $ in Millions",
        "columns": ["Line item", "May 10, 2026 (3 Months Ended)", "May 11, 2025 (3 Months Ended)",
                    "May 10, 2026 (8 Months Ended)", "May 11, 2025 (8 Months Ended)"],
        "periods": ["", "3 Months Ended", "3 Months Ended", "8 Months Ended", "8 Months Ended"],
        "rows": [
            ["Total Revenue", "$ 70,527", "$ 63,205", "$ 207,431", "$ 189,079"],
            ["Membership fees", "", "", "", ""],
            ["REVENUE", "", "", "", ""],
            ["Total Revenue", "$ 1,373", "$ 1,240", "$ 4,057", "$ 3,599"],
        ],
    }
    cells = line_periods(tenq, re.compile(r"membership\s+fee", re.I))
    assert [c["value"] for c in cells] == [4057e6, 3599e6, 1373e6, 1240e6], cells
    assert cells[0]["months"] == 8 and "May 10, 2026" in cells[0]["column"], cells[0]
    # Newest date first inside the longest-period group, whatever order the
    # filer rendered them in.
    flipped = dict(tenq, columns=["Line item", "May 11, 2025 (8 Months Ended)",
                                  "May 10, 2026 (8 Months Ended)"],
                   periods=["", "8 Months Ended", "8 Months Ended"],
                   rows=[["Membership fees", "", ""], ["Total Revenue", "$ 3,599", "$ 4,057"]])
    assert [c["value"] for c in line_periods(flipped, re.compile(r"membership", re.I))] \
        == [4057e6, 3599e6]
    print("ok: statements — report classification, biggest-table pick, line lookup, markdown")


if __name__ == "__main__":
    demo()
