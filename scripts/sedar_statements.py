#!/usr/bin/env python3
"""Numbers for a company that only files in Canada, read off its own statements.

EDGAR hands out tagged numbers (XBRL). SEDAR+ hands out a PDF. Capital IQ and
the other paid terminals close that gap the slow way: they collect the filed
document, read each statement line into a standard template, and keep a link
back to the page it came from. This file is that, for the three primary
statements:

    statements PDF  ->  rows as filed  ->  the us-gaap concept names trends.py reads

so the history table, the ratios and the EV bridge run on a TSX-only company
exactly as they do on a 10-K filer. `statement_map.md` is the click-through:
every number, the filed line it came from, and the page.

Learned the hard way:

  - pypdf's text order is not row order. Constellation's cash flow statement
    comes out as a column of numbers followed by a column of labels, in a
    different order. pdfminer reports where every word sits on the page, so a
    row here is "words at the same height", and a label can't drift off its
    number.
  - A column is a right edge. Numbers are right-aligned, so the note-reference
    column, a blank cell and a "-" all sort themselves out by x position, where
    counting tokens from the end of the line gets all three wrong.
  - Nothing is trusted until it ties. A balance sheet whose assets don't equal
    liabilities plus equity was misread, and is dropped rather than printed.
  - Each annual filing carries two years. Ten years of history is the last ten
    filings, newest restatement winning — the same rule EDGAR's facts follow.
  - Labels are matched, not understood. "Sales" is revenue; a company that
    calls its revenue something this file has no pattern for gets a blank, and
    the blank is listed. No line is estimated.
"""

import io
import re
from collections import Counter
from datetime import date, timedelta

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
DATE_RE = re.compile(r"\b({})\s+(\d{{1,2}})\s*,?\s*(20\d\d)\b".format("|".join(MONTHS)), re.I)
DATE_RE_DMY = re.compile(r"\b(\d{{1,2}})\s+({})\s*,?\s*(20\d\d)\b".format("|".join(MONTHS)), re.I)

ROW_TOLERANCE = 2.5    # points: words this close in height are one row
COLUMN_TOLERANCE = 10  # points: right edges this close are one column
MAX_PAGES = 400        # a bank's annual report puts its statements near page 150

_NUM = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
NUMBER_RE = re.compile(r"^(?:\({0}\)|{0})$".format(_NUM))  # brackets balanced: "8)" of "(note 8)" is no number
YEAR_RE = re.compile(r"(19|20)\d\d")
DASH_RE = re.compile(r"^[-–—]$")


# --- PDF -> rows ------------------------------------------------------------

def pdf_pages(data, max_pages=MAX_PAGES):
    """PDF bytes -> one list of rows per page, lazily.

    A row is [(x0, x1, word), ...] left to right; rows run top to bottom.
    """
    import logging

    from pdfminer.high_level import extract_pages  # here, so the 10-K path never needs it
    logging.getLogger("pdfminer").setLevel(logging.ERROR)  # "should not allow text extraction": it's a public filing
    from pdfminer.layout import LAParams, LTChar, LTTextLine

    def words_of(obj, out):
        if isinstance(obj, LTTextLine):
            word, x0, x1 = "", None, None
            for ch in obj:
                text = ch.get_text()
                if isinstance(ch, LTChar) and text.strip():
                    if not word:
                        x0 = ch.x0
                    word, x1 = word + text, ch.x1
                elif word:
                    out.append((obj.y0, x0, x1, word))
                    word = ""
            if word:
                out.append((obj.y0, x0, x1, word))
        elif hasattr(obj, "__iter__"):
            for child in obj:
                words_of(child, out)

    for i, page in enumerate(extract_pages(io.BytesIO(data), laparams=LAParams(boxes_flow=None))):
        if i >= max_pages:
            return
        words = []
        words_of(page, words)
        rows = {}
        for y, x0, x1, word in sorted(words, key=lambda w: -w[0]):
            key = next((k for k in rows if abs(k - y) <= ROW_TOLERANCE), y)
            rows.setdefault(key, []).append((x0, x1, word))
        yield [sorted(rows[y]) for y in sorted(rows, reverse=True)]


def row_text(row):
    return " ".join(w for _, _, w in row)


def _number(word):
    """'(1,234)' -> -1234.0, '-' -> 0.0, anything else -> None."""
    word = word.replace("$", "")
    if DASH_RE.match(word):
        return 0.0
    if not NUMBER_RE.match(word):
        return None
    value = float(re.sub(r"[(),]", "", word))
    return -abs(value) if "(" in word else value


def _cells(row):
    """Split a row into (label words, [(x1, value), ...]) — the numbers are the
    run at the right-hand end. '$' on its own and brackets split from their
    number are glued back first."""
    words = []
    for x0, x1, w in row:
        if w in ("$", "C$", "US$"):
            continue
        if words and (w == ")" or words[-1][2] in ("(", "-(")):
            words[-1] = (words[-1][0], x1, words[-1][2] + w)
        else:
            words.append((x0, x1, w))
    cells = []
    while words and _number(words[-1][2]) is not None:
        x0, x1, w = words.pop()
        # a dash inside a label ("Balance – January 28") is not a zero
        if DASH_RE.match(w) and not cells and words and not _is_last_cell(words, x0):
            words.append((x0, x1, w))
            break
        cells.append((x1, _number(w), w))
    return [w for _, _, w in words], cells[::-1]


def _is_last_cell(words, x0):
    """A trailing dash is a value when it stands clear of the label."""
    return x0 - words[-1][1] > 12


def _columns(parsed):
    """Right edges of the value columns on a page, left to right.

    Every numeric cell votes with its right edge. A cluster made only of bare
    one- and two-digit integers with real columns to its right is the note
    column, and goes.
    """
    edges = sorted((x1, v) for _, cells in parsed for x1, v, w in cells if not DASH_RE.match(w))
    clusters = []
    for x1, v in edges:
        if clusters and x1 - clusters[-1][-1][0] <= COLUMN_TOLERANCE:
            clusters[-1].append((x1, v))
        else:
            clusters.append([(x1, v)])
    if not clusters:
        return []
    busiest = max(len(c) for c in clusters)
    clusters = [c for c in clusters if len(c) >= max(3, 0.3 * busiest)]

    def is_notes(i):
        return (len(clusters) - i > 2
                and all(v == int(v) and 0 < v < 100 for _, v in clusters[i]))

    return [max(x for x, _ in c) for i, c in enumerate(clusters) if not is_notes(i)]


def table(page):
    """A page's rows -> [(label, [value per column or None]), ...] plus the
    header years that sit over those columns, when the page prints them."""
    parsed = [_cells(row) for row in page]
    cols = _columns(parsed)
    out, years, seen_data = [], [None] * len(cols), False
    for (label, cells), row in zip(parsed, page):
        # The column headings: a short line of years above the first figures.
        if not seen_data and cols and len(row) <= 10 and any(YEAR_RE.fullmatch(w) for _, _, w in row):
            for _, x1, w in row:
                if YEAR_RE.fullmatch(w):
                    near = min(range(len(cols)), key=lambda i: abs(cols[i] - x1))
                    if abs(cols[near] - x1) <= 45 and years[near] is None:
                        years[near] = int(w)
            out.append((row_text(row), []))
            continue
        values, stray = [None] * len(cols), []
        for x1, v, word in cells:
            near = min(range(len(cols)), key=lambda i: abs(cols[i] - x1), default=None)
            # a "-" is narrow and often has a "$" after it, so it sits left of the edge
            reach = 30 if DASH_RE.match(word) else COLUMN_TOLERANCE
            if near is not None and abs(cols[near] - x1) <= reach and values[near] is None:
                values[near] = v
            else:
                stray.append(word)  # a note reference, or a number inside the label
        seen_data = seen_data or any(v is not None for v in values)
        text = " ".join(label + stray)
        if text.count("(") == text.count(")"):  # "(note 8)" keeps its 8; "Sales 19" loses its 19
            text = re.sub(r"\s+(\d{1,2}(\s?,\s?\d{1,2})*|Notes?)$", "", text)
        out.append((text, values if any(v is not None for v in values) else []))
    return out, years


# --- which page is which statement -----------------------------------------

TITLES = {
    "balance": r"(statements?|sheets?) of (consolidated )?financial position|balance sheets?",
    "income": r"statements? of (consolidated )?(net )?(earnings|income|operations|profit|loss|comprehensive)"
              r"|income statements?",
    "cash": r"statements? of (consolidated )?cash ?flows?|cash ?flows? statements?",
}


def page_kind(page):
    """'balance', 'income', 'cash' or None — the title has to be on the page
    *and* the rows have to look like that statement, which is what keeps the
    auditor's report and the notes out."""
    lines = [row_text(r) for r in page]
    if any(re.match(r"\s*notes to (the )?(consolidated )?financial statements", ln, re.I)
           for ln in lines[:6]):
        return None
    head = " \n".join(lines[:12] + lines[-8:]).lower()
    body = "\n".join(lines).lower()
    if sum(1 for r in page if len(_cells(r)[1]) >= 2) < 8:
        return None  # a table of contents lists these titles too, beside one column of page numbers
    if re.search(TITLES["cash"], head) and all(
            re.search(k + r"\s+activities", body) for k in ("operating", "investing", "financing")):
        return "cash"
    if re.search(TITLES["balance"], head) and (re.search(r"^total assets\b", body, re.M) or (
            re.search(r"^assets\b", body, re.M) and "liabilities" in body
            and re.search(r"equity|deficiency|deficit", body))):
        return "balance"  # "total assets" alone: Quebecor puts liabilities on the next page
    if (re.search(TITLES["income"], head) and re.search(r"per (common |ordinary )?(share|unit)", body)
            and re.search(r"\b(revenues?|sales|net (earnings|income|loss)|profit)\b", body)
            and not re.search(r"^total assets\b", body, re.M)):
        return "income"
    return None


def find_statements(pages):
    """-> {kind: (page number, rows)}, plus the text of the opening pages (for
    the dates). Stops reading the PDF as soon as all three are found."""
    found, opening, pending = {}, [], None
    for n, page in enumerate(pages, 1):
        if n <= 12:
            opening.append("\n".join(row_text(r) for r in page))
        kind = page_kind(page)
        # A balance sheet that runs over the page: assets, then liabilities.
        if pending and kind is None and n == pending + 1 and not _has_equity(found["balance"][1]):
            found["balance"] = (pending, found["balance"][1] + page)
        if kind and kind not in found:
            found[kind] = (n, page)
            pending = n if kind == "balance" else pending
        if len(found) == 3 and n > (pending or 0) + 1:
            break
    return found, "\n".join(opening)


def _has_equity(page):
    """Does this page already hold the equity half of the balance sheet? Its
    heading is enough: Metro prints the totals with no label at all."""
    return any(re.match(r"(total )?(liabilities and |(shareholders|stockholders|unitholders)['’]? )?"
                        r"(equity|deficit|deficiency)\b", row_text(r).strip(), re.I) for r in page)


# --- units ------------------------------------------------------------------

SCALES = {"thousand": 1e3, "thousands": 1e3, "million": 1e6, "millions": 1e6,
          "billion": 1e9, "billions": 1e9, "000s": 1e3, "000's": 1e3}
SCALE_RE = re.compile(r"\b(thousands?|millions?|billions?)\b|\$ ?(000['’]?s)", re.I)


def units(*texts):
    """-> (scale, currency) from "(Expressed in thousands of Canadian dollars)".
    No scale phrase means raw dollars, which is how small issuers report."""
    scale, currency = None, None
    for text in texts:
        head = text[:1500]
        if scale is None:
            m = SCALE_RE.search(head)
            if m:
                scale = SCALES[(m.group(1) or m.group(2)).lower().replace("’", "'")]
        if currency is None:
            if re.search(r"u\.?s\.? dollars|united states dollars|US ?\$|\bUSD\b", head, re.I):
                currency = "USD"
            elif re.search(r"canadian dollars|\bCAD\b|C\$|Cdn", head, re.I):
                currency = "CAD"
    return scale or 1.0, currency


# --- rows -> concepts --------------------------------------------------------

def clean(label):
    """A filed label, reduced to what the patterns match on: note references,
    bracketed alternatives ("(loss)", "(used in)") and punctuation go."""
    label = label.lower().replace("’", "'").replace("‘", "'")
    label = re.sub(r"\((?![^)]*(?:basic|diluted))[^)]*\)", " ", label).replace("(", " ").replace(")", " ")
    label = re.sub(r"\bnotes? \d[\d, and]*", " ", label)
    label = re.sub(r"[:*†‡]|\s\d{1,2}(,\s?\d{1,2})*$|\snotes?\s*$", " ", label)
    return " ".join(label.split()).strip(" -–—,.")


# (concept, pattern) per statement. First matching row wins unless noted.
HOLDERS = r"(shareholders|stockholders|unitholders|owners|equity holders)"
BALANCE = [
    ("Assets", r"total assets"),
    ("Liabilities", r"total liabilities"),
    ("TotalLiabilitiesAndEquity", r"total liabilities and .*(equity|deficiency|deficit)"),
    ("StockholdersEquity", r"(total )?(equity|.*equity) attributable to .*" + HOLDERS + r".*"),
    ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
     r"total (" + HOLDERS + r"'? )?(equity|deficiency|deficit)"),
    ("AssetsCurrent", r"total current assets"),
    ("LiabilitiesCurrent", r"total current liabilities"),
    ("CashAndCashEquivalentsAtCarryingValue",
     r"cash( and cash equivalents)?|cash and (short-term|temporary) (investments|deposits)"),
    ("ShortTermInvestments", r"(short-term|temporary|marketable) (investments|securities)"),
    ("InventoryNet", r"(merchandise )?inventor(y|ies)"),
    ("AccountsReceivableNetCurrent",
     r"(trade |accounts )(and other )?receivables?( and other.*)?|receivables|trade and other receivables"),
    ("AccountsPayableCurrent",
     r"(trade |accounts )(and other )?payables?.*|trade and other payables.*|payables and accru.*"),
    ("PropertyPlantAndEquipmentNet", r"property,? (plant,? )?and equipment.*|(capital|fixed) assets"),
    ("Goodwill", r"goodwill"),
    ("IntangibleAssetsNetExcludingGoodwill", r"(other )?intangible assets|intangibles"),
    ("RetainedEarningsAccumulatedDeficit", r"retained earnings|(accumulated )?deficit"),
    ("MinorityInterest", r"(equity attributable to )?non-?controlling interests?"),
    ("ContractWithCustomerLiabilityCurrent", r"deferred revenues?|contract liabilities|unearned revenues?"),
]
INCOME = [
    ("Revenues", r"(total |net )?(revenues?|sales)( and other income)?|total net (revenues?|sales)"
                 r"|insurance revenue"
                 r"|revenues?,? net.*|sales and (other )?operating revenues?|sales and other revenues?"
                 r"|total revenues? .*"),
    ("CostOfRevenue", r"cost of (sales|goods sold|revenues?|products sold|merchandise sold|services).*"),
    ("GrossProfit", r"gross (profit|margin)"),
    ("OperatingIncomeLoss", r"operating (income|earnings|profit)"
                            r"|(income|earnings|profit|results) from (operations|operating activities)"),
    ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
     r"(net )?(earnings|income|profit|loss) before (provision for )?(income )?tax(es)?.*"),
    ("IncomeTaxExpenseBenefit", r"(provision for |total )?income tax(es)?( expense| provision| recovery)?"),
    ("IncomeLossFromContinuingOperations",
     r"(net )?(earnings|income|profit|loss) from continuing operations"),
    ("ProfitLoss", r"net (earnings|income|loss|profit)( for the (year|period))?"
                   r"|(profit|earnings|income|loss) for the (year|period)"
                   r"|net (earnings|income) and comprehensive income"),
    ("NetIncomeLoss", r"(net )?(earnings|income|profit|loss).* attributable to .*" + HOLDERS + r".*"),
    ("SellingGeneralAndAdministrativeExpense",
     r"selling,? general,? and administrative.*|general,? (and )?administrative.*"),
    ("ResearchAndDevelopmentExpense", r"research and development.*"),
    ("InterestExpense", r"interest expense.*|interest on (long-term )?debt.*"),
    # What an IFRS filer prints instead. It can carry accretion and FX, and
    # "net" ones are after interest income, so it is a stand-in: trends.py only
    # reads it when there is no InterestExpense, and statement_map.md names it.
    ("InterestAndDebtExpense",
     r"(net )?(finance|financing|financial) (costs?|charges|expenses?)( and other.*)?"
     r"|interest and (other )?(finance|financing) (costs?|charges|expenses?).*|net interest expense.*"),
]
CASH = [
    ("PaymentsToAcquirePropertyPlantAndEquipment",
     r"(purchases?|acquisitions?|additions?) (of|to) (property|capital assets|fixed assets|plant).*"
     r"|(fixed assets?|property(,? plant,?)?,? (and )?equipment)( and [a-z ]+)? "
     r"(purchases?|purchased|additions?|expenditures)|capital expenditures.*"),
    ("PaymentsForRepurchaseOfCommonStock",
     r"(repurchases?|purchases?|redemptions?|buy-?backs?) (and cancellation )?of "
     r"(common |subordinate voting |class [a-z] |ordinary |multiple voting )?(shares|stock|units).*"
     r"|(common )?shares (repurchased|purchased for cancellation).*|normal course issuer bid.*"),
    ("PaymentsOfDividendsCommonStock",
     r"(cash )?dividends?( paid)?( to| on)? .*(common|ordinary|shareholders of the).*"),
    ("PaymentsOfDividends", r"(payment of |cash )?dividends?( paid)?( to .*| on .*)?"
                            r"|distributions? (paid|to unitholders).*"),
    ("ShareBasedCompensation", r"(share|stock|equity)-?based (compensation|payments?).*"),
    ("PaymentsToAcquireBusinessesNetOfCashAcquired",
     r"(business )?acquisitions?,? net of cash.*|acquisitions? of (a )?(business(es)?|subsidiar\w+).*"
     r"|business (acquisitions?|combinations?).*"),
]
CONTINUING = re.compile(r"continuing|discontinued")
NOT_COMMON = re.compile(r"non-?controlling|minority|preferred|received")
DEBT_RE = re.compile(r"\b(debt|borrowings|debentures|notes payable|senior notes|credit facilit\w+"
                     r"|bank indebtedness|bank loans?|term loans?|loans? payable|commercial paper)\b")
NOT_DEBT_RE = re.compile(r"lease|derivative|deferred|issu|interest|receivable|investment")
LEASE_RE = re.compile(r"lease (liabilit|obligation)|obligations? under (finance |capital )?leases?")
DA_RE = re.compile(r"^(depreciation|amortization|depletion)\b")
NOT_DA_RE = re.compile(r"financing|debt|deferred|discount|premium|third party|issue|bond|contract")
SECTIONS = [
    ("current_assets", r"current assets"),
    ("noncurrent", r"non-?current (assets|liabilities)|long-term liabilities"),
    ("current_liabilities", r"current liabilities"),
    ("equity", r"(" + HOLDERS + r"'? )(equity|deficiency|deficit)|equity"),
]


def _first(rows, pattern, skip=None):
    for i, (label, values, raw) in enumerate(rows):
        if values and re.fullmatch(pattern, label) and not (skip and skip.search(label)):
            return i
    return None


def labelled(tab):
    """[(label, values)] -> [(clean label, values, label as filed)], with two
    repairs: a label wrapped over two lines is joined, and a subtotal printed
    with no label takes "total " + the heading it sits under — but only when
    it is the sum of the lines under that heading. Metro prints total assets
    unlabelled straight after its non-current assets, and that is not their
    subtotal."""
    out, heading, used, side, since, activity = [], "", set(), "", [], ""
    for label, values in tab:
        c = clean(label)
        # NFI heads both halves of its balance sheet "Current": say which.
        side = "assets" if c == "assets" else "liabilities" if c.startswith("liabilities") else side
        if side and re.fullmatch(r"(total )?(current|non-current|long-term)", c):
            c += " " + side
        if not values:
            if c and out and not out[-1][1] and out[-1][0] and label[:1].islower():
                label = out.pop()[2] + " " + label  # a heading wrapped over two lines
                c = clean(label)
            if c:
                heading, since = c, []
                if re.search(r"(operating|investing|financing) activities", c):
                    activity = c
            out.append((c, [], label))
            continue
        if c.startswith("attributable to") and heading:
            c = heading + " " + c  # "Equity" / "Attributable to equity holders of the parent"
        if c.startswith("total"):
            used.add(heading)  # this heading has its total; a blank one below is something else
        prev = out[-1] if out else None
        if prev and not prev[1] and prev[2] and (label[:1].islower() or not c):
            out.pop()
            c, label = clean(prev[2] + " " + label), (prev[2] + " " + label).strip()
        elif not c and activity and _adds_up(values, since):
            # Cash flow: every running subtotal takes the activity's name, and
            # read_cash keeps the last one — the one after working capital and tax.
            c, label = "total " + activity, "(subtotal under “{}”)".format(activity)
        elif not c and heading and heading not in used and _adds_up(values, since):
            used.add(heading)
            c, label = "total " + heading, "(subtotal under “{}”)".format(heading)
        since.append(values)
        out.append((c, values, label))
    return out


def _adds_up(values, since):
    """Is this unlabelled line the sum of the lines above it? Any run of them
    ending at the last one counts: Couche-Tard's total equity is the two lines
    above it (equity attributable to shareholders + non-controlling interest),
    not the whole section."""
    for k in range(len(since), 0, -1):
        if k == 1 and len(since) > 1:
            break  # one line "adding up" to the next is just a repeated figure
        tail = since[-k:]
        if all(v is None or _close(v, sum(r[i] or 0 for r in tail if i < len(r)))
               for i, v in enumerate(values)):
            return True
    return False


def read_balance(rows):
    """-> {concept: (values, filed label)}. Debt and leases are sums of every
    line that says so, split by the section they sit in."""
    got, section, debt, leases = {}, None, {"current": [], "noncurrent": []}, []
    for concept, pattern in BALANCE:
        i = _first(rows, pattern)
        if i is not None:
            got[concept] = (rows[i][1], rows[i][2])
    # Only below the assets: an insurer's "Debt securities" are investments.
    past_assets, mentions_debt = False, False
    for label, values, raw in rows:
        if label.startswith(("total assets", "liabilities")) or (
                values and "Assets" in got and values == got["Assets"][0]):
            past_assets = True
        if not values:
            section = next((name for name, pat in SECTIONS if re.fullmatch(pat, label)), section)
            continue
        if label.startswith("total"):
            if "current" in label:
                section = "noncurrent"  # whatever follows the current subtotal isn't current
            continue
        if not past_assets:
            continue
        combined = re.match(r"(current portion of )?long-term debt and lease", label)  # Air Canada's one line
        mentions_debt = mentions_debt or bool(re.search(r"debt|borrow|loan|notes|debenture|credit|indebted", label))
        if LEASE_RE.search(label) and not combined:
            leases.append((values, raw))
        elif DEBT_RE.search(label) and (combined or not NOT_DEBT_RE.search(label)):
            current = section == "current_liabilities" or re.search(
                r"current (portion|maturit)|due within one year|short-term|bank indebtedness"
                r"|commercial paper", label) and "non-current" not in label
            debt["current" if current else "noncurrent"].append((values, raw))
    for concept, lines in (("DebtCurrent", debt["current"]),
                           ("LongTermDebtNoncurrent", debt["noncurrent"]),
                           ("FinanceLeaseLiability", leases)):
        if lines:
            got[concept] = (_sum_rows([v for v, _ in lines]), " + ".join(r for _, r in lines))
    if past_assets and not mentions_debt:
        # Aritzia: leases and nothing else. Zero, said out loud, so the EV
        # bridge isn't left blank for a company with no borrowings. Only when
        # no liability line so much as mentions debt, loans or notes.
        width = max(len(v) for _, v, _ in rows)
        got["LongTermDebtNoncurrent"] = ([0.0] * width, "(no line on the balance sheet mentions debt "
                                                        "or borrowings — taken as zero)")

    # Totals printed with no label at all. The grand total is the figure that
    # appears twice (assets, then liabilities + equity); total liabilities is
    # the blank line that makes up the difference from equity.
    blank = [v for label, v, _ in rows if v and not label]
    if "Assets" not in got:
        twice = next((v for i, v in enumerate(blank) if v in blank[i + 1:]), None)
        if twice:
            got["Assets"] = (twice, "(total printed with no label, for assets and again for "
                                    "liabilities + equity)")
            got["TotalLiabilitiesAndEquity"] = got["Assets"]
    eq = (got.get("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
          or got.get("StockholdersEquity"))
    if "Liabilities" not in got and "Assets" in got and eq:
        for v in blank:
            if _close(got["Assets"][0][0], (v[0] or 0) + (eq[0][0] or 0)) and v[0]:
                got["Liabilities"] = (v, "(total printed with no label: assets − equity)")
                break
    return got


def _sum_rows(rows):
    width = max(len(r) for r in rows)
    return [sum(r[i] for r in rows if i < len(r) and r[i] is not None)
            if any(i < len(r) and r[i] is not None for r in rows) else None for i in range(width)]


def read_income(rows):
    got, heading, split, nci, parts, head_raw = {}, "", False, None, {}, ""
    for concept, pattern in INCOME:
        # "…from continuing operations attributable to shareholders" is not the year's net income
        i = _first(rows, pattern, skip=CONTINUING if concept == "NetIncomeLoss" else None)
        if i is not None:
            got[concept] = (rows[i][1], rows[i][2])
    for label, values, raw in rows:
        if not values:
            heading, head_raw = label, raw
            # "Net income attributable to:" opens a block that a per-share or
            # comprehensive-income heading closes
            if "attributable to" in label:
                split = "comprehensive" not in label
            elif re.search(r"per (common |ordinary )?(share|unit)|comprehensive|weighted average", label):
                split = False
            continue
        if split and re.fullmatch(r"non-?controlling interests?", label):
            nci = (values, raw)
        where = heading + " " + label
        if "weighted average" in where:
            key = ("WeightedAverageNumberOfDilutedSharesOutstanding" if "diluted" in label
                   else "WeightedAverageNumberOfSharesOutstandingBasic" if "basic" in label else None)
            # "(millions)" beside the count is the only place its scale is stated
            unit = re.search(r"\b(thousands|millions)\b", (head_raw + " " + raw).lower())
            if key and unit:
                values = [v * SCALES[unit.group(1)] if v is not None else None for v in values]
        elif (re.search(r"per (common |ordinary )?(share|unit)", heading) and "dividend" not in heading
              and re.fullmatch(r"(from )?(continuing|discontinued) operations", label)):
            # Loblaw, 2019: EPS only as its two halves, under a "- Diluted" heading
            key = None
            half = ("EarningsPerShareDiluted" if "diluted" in heading
                    else "EarningsPerShareBasic" if "basic" in heading else None)
            if half:
                parts.setdefault(half, []).append((values, head_raw))
        elif re.search(r"per (common |ordinary )?(share|unit)", where) and "dividend" not in where:
            key = ("EarningsPerShareDiluted" if "diluted" in label
                   else "EarningsPerShareBasic" if "basic" in label else None)
            if key and "basic and diluted" in label:
                got.setdefault("EarningsPerShareBasic", (values, raw))
        elif "attributable to" in heading and re.search(HOLDERS, label) and "comprehensive" not in heading:
            key = "NetIncomeLoss"
        else:
            key = None
        if key:
            got.setdefault(key, (values, raw))
    for half, lines in parts.items():
        if half not in got:
            got[half] = (_sum_rows([v for v, _ in lines]), lines[0][1] + " (continuing + discontinued)")
    # Canadian Tire prints the shareholders' share only as continuing +
    # discontinued. Net income less the non-controlling share is the same figure.
    if "NetIncomeLoss" not in got and nci and "ProfitLoss" in got:
        total, label = got["ProfitLoss"]
        got["NetIncomeLoss"] = ([t - n if t is not None and n is not None else None
                                 for t, n in zip(total, nci[0])], "{} − {}".format(label, nci[1]))
    return got


def read_cash(rows):
    got = {}
    for concept, pattern in CASH:
        i = _first(rows, pattern, skip=NOT_COMMON)
        if i is not None:
            got[concept] = ([abs(v) if v is not None else None for v in rows[i][1]], rows[i][2])
    if "ShareBasedCompensation" in got:  # an add-back, not a payment: keep its sign
        i = _first(rows, dict(CASH)["ShareBasedCompensation"])
        got["ShareBasedCompensation"] = (rows[i][1], rows[i][2])
    # The operating total is the last line that says "operating activities":
    # the ones above it are sub-totals "before working capital".
    heading = ""
    for label, values, raw in rows:
        if not values:
            heading = label
        elif ("common share" in heading and re.match(r"(re)?purchased (and|for) cancel", label)
              and "PaymentsForRepurchaseOfCommonStock" not in got):
            # Loblaw: "Common share capital" / "Purchased and cancelled"
            got["PaymentsForRepurchaseOfCommonStock"] = ([abs(v) if v is not None else None for v in values],
                                                         heading.capitalize() + " — " + raw)
        if values and "operating activities" in label and not re.search(
                r"before|discontinued|changes? in", label):
            got["NetCashProvidedByUsedInOperatingActivities"] = (values, raw)
        if re.search(r"investing activities|financing activities", label):
            break
    da = [(v, raw) for label, v, raw in rows if v and DA_RE.match(label)
          and (label.startswith("depreciation") or not NOT_DA_RE.search(label))]
    if da:
        got["DepreciationDepletionAndAmortization"] = (
            _sum_rows([v for v, _ in da]), " + ".join(r for _, r in da))
    return got


EXPENSES = {"CostOfRevenue", "IncomeTaxExpenseBenefit", "InterestExpense", "InterestAndDebtExpense",
            "SellingGeneralAndAdministrativeExpense", "ResearchAndDevelopmentExpense"}
PER_SHARE = {"EarningsPerShareDiluted", "EarningsPerShareBasic"}
SHARES = {"WeightedAverageNumberOfDilutedSharesOutstanding",
          "WeightedAverageNumberOfSharesOutstandingBasic"}
INSTANT = {c for c, _ in BALANCE} | {"DebtCurrent", "LongTermDebtNoncurrent", "FinanceLeaseLiability"}


def _close(a, b):
    return a is not None and b is not None and abs(a - b) <= max(2.0, 0.002 * abs(a))


# --- one filing -------------------------------------------------------------

def read_filing(data, filed):
    """One annual statements PDF -> everything read off it, or None when the
    three statements can't be found.

    {period, prior, scale, currency, values: {concept: {period end: number}},
     source: {concept: (filed label, page)}, tables: {kind: {...}}, checks: [..]}
    """
    found, opening = find_statements(pdf_pages(data))
    if "balance" not in found and "income" not in found:
        return None
    heads = "\n".join(row_text(r) for k in found for r in found[k][1][:10])
    period = year_end(heads + "\n" + opening, filed)
    if not period:
        return None
    prior = prior_year_end(heads + "\n" + opening, period)
    scale, currency = units(*["\n".join(row_text(r) for r in found[k][1][:14]) for k in found], opening)

    checks, values, source, tables = [], {}, {}, {}
    for kind, reader in (("balance", read_balance), ("income", read_income), ("cash", read_cash)):
        if kind not in found:
            checks.append("{}: statement not found in the PDF".format(kind))
            continue
        page_no, page = found[kind]
        tab, years = table(page)
        width = max((len(v) for _, v in tab), default=0)
        if width < 2:
            checks.append("{}: fewer than two columns read on page {}".format(kind, page_no))
            continue
        # Which column is this year: the larger header year, else the left one.
        order = list(range(width))
        if all(y is not None for y in years) and len(set(years)) == width:
            order.sort(key=lambda i: -years[i])
        cur, old = order[0], order[1]
        rows = labelled(tab)
        got = reader(rows)

        if kind == "balance":
            assets = got.get("Assets", ([None] * width,))[0]
            both = got.get("TotalLiabilitiesAndEquity", ([None] * width,))[0]
            eq = (got.get("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
                  or got.get("StockholdersEquity") or ([None] * width,))[0]
            liab = got.get("Liabilities", ([None] * width,))[0]
            tied = (_close(assets[cur], both[cur]) and _close(assets[old], both[old])) or (
                liab[cur] is not None and eq[cur] is not None
                and _close(assets[cur], liab[cur] + eq[cur]))
            if not tied:
                checks.append("balance sheet (page {}): assets don't tie to liabilities + equity "
                              "— dropped".format(page_no))
                continue
            checks.append("balance sheet (page {}): assets = liabilities + equity ✓".format(page_no))
            # Identities, not estimates: IFRS filers often print only one of these.
            total_eq = "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
            if "Liabilities" not in got and eq[cur] is not None and (
                    total_eq in got or "MinorityInterest" not in got):
                got["Liabilities"] = ([a - e if a is not None and e is not None else None
                                       for a, e in zip(assets, eq)], "total assets − total equity")
            if total_eq not in got and "StockholdersEquity" not in got and liab[cur] is not None:
                got[total_eq] = ([a - l if a is not None and l is not None else None
                                  for a, l in zip(assets, liab)], "total assets − total liabilities")
            got.pop("TotalLiabilitiesAndEquity", None)
        if kind == "income":
            pre = got.get("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItems"
                          "NoncontrollingInterest")
            tax, net = got.get("IncomeTaxExpenseBenefit"), got.get("ProfitLoss")
            # Metro prints expenses as negatives. Either tell says so: cost of
            # sales below zero, or pretax + tax landing on net income.
            cost = got.get("CostOfRevenue")
            # Net income from continuing operations when it's printed: that is
            # the line pretax and tax reconcile to when something was sold off.
            net = got.pop("IncomeLossFromContinuingOperations", None) or net
            negative = (cost and (cost[0][cur] or 0) < 0) or (
                pre and tax and net and None not in (pre[0][cur], tax[0][cur], net[0][cur])
                and tax[0][cur] and _close(pre[0][cur] + tax[0][cur], net[0][cur]))
            if negative:
                for c in EXPENSES & set(got):
                    got[c] = ([-v if v is not None else None for v in got[c][0]], got[c][1])
                tax = got.get("IncomeTaxExpenseBenefit")
            if pre and tax and net and pre[0][cur] is not None and tax[0][cur] is not None:
                ok = _close(pre[0][cur] - tax[0][cur], net[0][cur])
                checks.append("income statement (page {}): pretax − tax {} net income{}".format(
                    page_no, "=" if ok else "≠", " ✓" if ok else " (discontinued operations, or a misread line)"))
            if "NetIncomeLoss" not in got and net:
                got["NetIncomeLoss"] = net

        for concept, (vals, raw) in got.items():
            mult = 1.0 if concept in PER_SHARE | SHARES else scale
            ends = {}
            for col, end in ((cur, period), (old, prior)):
                if col < len(vals) and vals[col] is not None:
                    ends[end] = vals[col] * mult
            if ends:
                values[concept] = ends
                source[concept] = (raw, page_no)
        tables[kind] = {"page": page_no, "columns": [cur, old],
                        "rows": [(raw, vals) for _, vals, raw in rows if raw.strip()]}

    _fix_share_scale(values)
    return {"period": period, "prior": prior, "filed": filed[:10], "scale": scale,
            "currency": currency, "values": values, "source": source, "tables": tables,
            "checks": checks}


def _fix_share_scale(values):
    """A statement says "(thousands)" beside its share count, or nothing at
    all. The filer's own arithmetic settles it: net income / EPS is what the
    count has to be, so pick the power of a thousand that lands nearest."""
    ni, eps = values.get("NetIncomeLoss", {}), values.get("EarningsPerShareDiluted") or values.get(
        "EarningsPerShareBasic", {})
    for concept in SHARES:
        for end, count in list(values.get(concept, {}).items()):
            if not count or not ni.get(end) or not eps.get(end):
                continue
            implied = abs(ni[end] / eps[end])
            values[concept][end] = min((count * s for s in (1, 1e3, 1e6)),
                                       key=lambda c: abs(c / implied - 1))


def _dates(text):
    out = []
    for rx, order in ((DATE_RE, (1, 2)), (DATE_RE_DMY, (2, 1))):
        for m in rx.finditer(text):
            try:
                out.append(date(int(m.group(3)), MONTHS.index(m.group(order[0]).lower()) + 1,
                                int(m.group(order[1]))))
            except ValueError:
                pass
    return out


def year_end(text, filed):
    """The fiscal year end: the date the opening pages repeat most, between
    three weeks and 200 days before the filing (an audit takes at least three
    weeks, so the auditor's report date and the filing date can't qualify)."""
    filed = date.fromisoformat(filed[:10])
    found = Counter(d for d in _dates(text[:12000])
                    if timedelta(days=21) <= filed - d <= timedelta(days=200))
    return max(found, key=lambda d: (found[d], d)).isoformat() if found else None


def prior_year_end(text, period):
    """The comparative column's date. A 52/53-week filer moves it by a day or
    a week each year, so it is read off the page, and only assumed when the
    page doesn't say."""
    end = date.fromisoformat(period)
    found = Counter(d for d in _dates(text) if 350 <= (end - d).days <= 380)
    if found:
        # Same day a year earlier wins outright: Intact's pages also say
        # "January 1, 2025", which is 364 days back and no year end at all.
        return max(found, key=lambda d: ((d.month, d.day) == (end.month, end.day), found[d], d)).isoformat()
    try:
        return end.replace(year=end.year - 1).isoformat()
    except ValueError:  # Feb 29
        return (end - timedelta(days=365)).isoformat()


# --- many filings -> companyfacts -------------------------------------------

def as_companyfacts(filings):
    """Parsed filings -> the companyfacts shape trends.py reads.

    Same trick as the 40-F adapter: the unit key is "USD" whatever the
    currency (nothing is converted) and the form is "10-K". `filed` is the
    SEDAR+ filing date, so a later filing's restated comparative wins, as it
    does on EDGAR.
    """
    out = {}
    for f in filings:
        for concept, ends in f["values"].items():
            unit = ("USD/shares" if concept in PER_SHARE else "shares" if concept in SHARES else "USD")
            rows = out.setdefault(concept, {"units": {unit: []}})["units"].setdefault(unit, [])
            for end, val in ends.items():
                row = {"end": end, "val": val, "form": "10-K", "filed": f["filed"],
                       "accn": "SEDAR-" + f["filed"]}
                if concept not in INSTANT:
                    row["start"] = (date.fromisoformat(end) - timedelta(days=363)).isoformat()
                rows.append(row)
    return {"facts": {"us-gaap": out, "dei": {}}}


def restatements(filings):
    """Where two filings report the same year and disagree: [(concept, year,
    as first filed, as restated)]. Worth a look, never an error — comparatives
    get restated for acquisitions and accounting changes."""
    out, first = [], {}
    for f in sorted(filings, key=lambda f: f["filed"]):
        for concept, ends in f["values"].items():
            for end, val in ends.items():
                was = first.setdefault((concept, end), val)
                if not _close(was, val) and concept not in SHARES:
                    out.append((concept, end, was, val))
    return out


# --- output -----------------------------------------------------------------

TITLE = {"balance": ("balance_sheet", "Consolidated statement of financial position"),
         "income": ("income_statement", "Consolidated statement of earnings"),
         "cash": ("cash_flow", "Consolidated statement of cash flows")}


def _stamp(iso):
    d = date.fromisoformat(iso)
    return "{}. {:02d}, {}".format(MONTHS[d.month - 1][:3].title(), d.day, d.year)


def statement_tables(f):
    """The newest filing's three statements in the {name, title, table} shape
    statements.to_markdown writes — cells verbatim, in the filed scale."""
    scale = {1e3: "thousands", 1e6: "millions", 1e9: "billions"}.get(f["scale"], "units")
    out = []
    for kind, t in f["tables"].items():
        name, title = TITLE[kind]
        tail = "" if kind == "balance" else " (12 Months Ended)"
        cols = ["Line item"] + [_stamp(f[k]) + tail for k in ("period", "prior")]
        rows, filled = [], [i for i, (_, vals) in enumerate(t["rows"]) if vals]
        for raw, vals in t["rows"][filled[0]:filled[-1] + 1] if filled else []:
            cells = [vals[c] if c < len(vals) else None for c in t["columns"]] if vals else ["", ""]
            rows.append([raw] + ["" if v == "" else "—" if v is None else
                                 "{:,.2f}".format(v) if v != int(v) else "{:,.0f}".format(v)
                                 for v in cells])
        out.append({"name": name, "title": title, "table": {
            "columns": cols, "rows": rows,
            "units": "{} in {} — as filed on SEDAR+, read from page {} of the statements PDF".format(
                f["currency"] or "currency not stated", scale, t["page"])}})
    return out


def map_markdown(filings, labels):
    """statement_map.md — the click-through. `labels` is {concept: metric name}."""
    newest = filings[0]
    lines = ["# Where each number came from", "",
             "Every figure in `trends.md` and `model.csv` was read off a statement this company "
             "filed on SEDAR+. This is the line it came from in the newest filing "
             "(fiscal year ended {}), and the page of `financial_statements.pdf` to check it "
             "against.".format(newest["period"]), "",
             "| Metric | Line as filed | Page |", "|---|---|---|"]
    seen = set()
    for concept, (raw, page) in sorted(newest["source"].items(), key=lambda kv: (kv[1][1], kv[0])):
        line = "| {} | {} | {} |".format(labels.get(concept, concept), raw.replace("|", "\\|"), page)
        if line not in seen:
            seen.add(line)
            lines.append(line)
    lines += ["", "## Tie-outs", ""]
    for f in filings:
        for c in f["checks"]:
            lines.append("- Year ended {} (filed {}): {}".format(f["period"], f["filed"], c))
    changed = restatements(filings)
    if changed:
        lines += ["", "## Restated since first filed", "",
                  "A later filing's comparative column disagrees with the original. The later "
                  "figure is the one used.", "",
                  "| Metric | Year end | First filed | Restated |", "|---|---|---|---|"]
        for concept, end, was, now in changed[:60]:
            fmt = "| {} | {} | {:,.2f} | {:,.2f} |" if concept in PER_SHARE else "| {} | {} | {:,.0f} | {:,.0f} |"
            lines.append(fmt.format(labels.get(concept, concept), end, was, now))
    return "\n".join(lines) + "\n"


def demo():
    """python3 sedar_statements.py — offline; rows are built by hand, no PDF."""
    def row(label, *cells):
        # label words at the left, numbers right-aligned on 400 and 500
        words = [(40 + 30 * i, 65 + 30 * i, w) for i, w in enumerate(label.split())]
        return words + [(x - 40, x, c) for x, c in zip((300, 400, 500)[-len(cells):], cells) if c]

    page = [row("Consolidated Statements of Financial Position"), row("", "2025", "2024"),
            row("Current assets"), row("Cash and cash equivalents", "7", "331,569", "122,685"),
            row("Inventories", "", "1,103,175", "921,095"), row("", "1,434,744", "1,043,780"),
            row("Goodwill", "6,10", "801,709", "727,782"),
            row("Deferred income taxes", "16", "25,714", "-"),
            row("Total assets", "2,262,167", "1,771,562"),
            row("Current liabilities"), row("Current portion of long-term debt", "13", "398,275", "273,771"),
            row("Lease liabilities", "8", "377,743", "278,705"),
            row("Total liabilities", "1,000,000", "900,000"),
            row("Shareholders’ equity"), row("Retained earnings (Deficit)", "14", "128,858", "(225,301)"),
            row("Total shareholders’ equity", "1,262,167", "871,562"),
            row("Total liabilities and shareholders’ equity", "2,262,167", "1,771,562")]
    tab, years = table(page)
    assert years == [2025, 2024], years
    rows = labelled(tab)
    got = read_balance(rows)
    assert got["Assets"][0] == [2262167.0, 1771562.0], got["Assets"]
    assert got["CashAndCashEquivalentsAtCarryingValue"][0][0] == 331569.0   # the 7 is a note ref
    assert got["Goodwill"][0] == [801709.0, 727782.0]                      # so is "6,10"
    assert got["AssetsCurrent"][0][0] == 1434744.0, "unlabelled subtotal takes its heading"
    assert got["RetainedEarningsAccumulatedDeficit"][0][1] == -225301.0
    assert got["DebtCurrent"][0][0] == 398275.0 and got["FinanceLeaseLiability"][0][0] == 377743.0
    assert dict((l, v) for l, v, _ in rows)["deferred income taxes"] == [25714.0, 0.0]  # "-" is zero

    assert clean("Net cash flows from (used in) operating activities (note 4):") == \
        "net cash flows from operating activities"
    inc = labelled([("Sales 19", [7255.0, 6413.0]), ("Net earnings", [1309.0, 1168.0]),
                    ("Earnings per common share", []), ("Diluted", [4.73, 4.16]),
                    ("Weighted average number of shares (thousands)", []), ("Diluted", [276.7, 280.8])])
    got = read_income(inc)
    assert got["Revenues"][0][0] == 7255.0 and got["EarningsPerShareDiluted"][0][0] == 4.73
    assert got["WeightedAverageNumberOfDilutedSharesOutstanding"][0][0] == 276700.0  # "(thousands)"

    cf = labelled([("Depreciation of property", [48.0, 47.0]), ("Amortization of intangibles", [29.0, 32.0]),
                   ("Cash generated by operating activities before interest", [301.0, 138.0]),
                   ("Net cash generated by operating activities", [173.0, 15.0]),
                   ("Investing activities", []),
                   ("Acquisition of property, plant and equipment", [-33.0, -30.0]),
                   ("Dividends paid to non-controlling interests", [-3.0, -89.0]),
                   ("Dividends paid to common shareholders of the Company", [-85.0, -85.0])])
    got = read_cash(cf)
    assert got["NetCashProvidedByUsedInOperatingActivities"][0] == [173.0, 15.0]
    assert got["DepreciationDepletionAndAmortization"][0] == [77.0, 79.0]
    assert got["PaymentsToAcquirePropertyPlantAndEquipment"][0] == [33.0, 30.0]
    assert got["PaymentsOfDividendsCommonStock"][0] == [85.0, 85.0]

    vals = {"NetIncomeLoss": {"y": 1309438e3}, "EarningsPerShareDiluted": {"y": 4.73},
            "WeightedAverageNumberOfDilutedSharesOutstanding": {"y": 276684.0}}
    _fix_share_scale(vals)
    assert vals["WeightedAverageNumberOfDilutedSharesOutstanding"]["y"] == 276684e3

    cover = "As at February 1, 2026 and February 2, 2025\nFebruary 1, 2026\nreport dated March 20, 2026"
    assert year_end(cover, "2026-03-24") == "2026-02-01"
    assert prior_year_end(cover, "2026-02-01") == "2025-02-02"
    assert prior_year_end("nothing", "2025-12-31") == "2024-12-31"
    assert units("(Expressed in thousands of Canadian dollars)") == (1e3, "CAD")
    assert units("(In millions of U.S. dollars, except per share amounts)") == (1e6, "USD")

    old = {"filed": "2025-03-01", "values": {"Revenues": {"2024-12-31": 100.0}, "Assets": {"2024-12-31": 9.0}}}
    new = {"filed": "2026-03-01", "values": {"Revenues": {"2025-12-31": 120.0, "2024-12-31": 110.0}}}
    facts = as_companyfacts([new, old])["facts"]["us-gaap"]
    assert "start" in facts["Revenues"]["units"]["USD"][0] and "start" not in facts["Assets"]["units"]["USD"][0]
    assert restatements([new, old]) == [("Revenues", "2024-12-31", 100.0, 110.0)]
    print("ok: sedar_statements — columns, note refs, subtotals, debt, EPS, cash flow, dates, facts")


if __name__ == "__main__":
    demo()
