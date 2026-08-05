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

import pandas as pd

warnings.filterwarnings("ignore", message="Passing literal html")

REPORT_RE = re.compile(r"<Report[^>]*>(.*?)</Report>", re.S | re.I)

# ShortName keyword -> stable output filename, so income_statement.md is always
# income_statement.md regardless of what the company calls it.
CANONICAL = [
    ("comprehensive income", "comprehensive_income"),
    ("cash flow", "cash_flow"),
    ("balance sheet", "balance_sheet"),
    ("financial position", "balance_sheet"),
    ("stockholders' equity", "equity"),
    ("shareholders' equity", "equity"),
    ("equity", "equity"),
    ("operations", "income_statement"),
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

    if isinstance(df.columns, pd.MultiIndex):
        headers = [str(c[-1]) for c in df.columns]
        units = str(df.columns[0][0])
    else:
        headers = [str(c) for c in df.columns]
        units = headers[0]
    # Column 0's header repeats the table title; the periods start at column 1.
    columns = ["Line item"] + headers[1:]

    rows = []
    for _, row in df.iterrows():
        cells = ["" if pd.isna(v) else re.sub(r"\s+", " ", str(v)).strip() for v in row]
        if not cells or not cells[0]:
            continue
        rows.append(cells)
    return {"units": re.sub(r"\s+", " ", units).strip(), "columns": columns, "rows": rows}


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
            table = parse_report(client.filing_file(cik, accession_plain, rep["file"]))
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
    print("ok: statements — report classification, biggest-table pick, markdown")


if __name__ == "__main__":
    demo()
