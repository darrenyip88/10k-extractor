#!/usr/bin/env python3
"""The 10-K, for a TSX-listed company: ./run_10k.sh DOL   (or TSX:DOL, or DOL.TO)

A Canadian issuer files no 10-K. The same material is split across three
documents filed on SEDAR+, and this pulls all three:

    Annual Information Form (AIF)   ->  10-K Items 1, 1A, 3, 5   (business, risks)
    annual MD&A                     ->  Item 7
    audited annual statements       ->  Item 8

Learned the hard way:

  - SEDAR+ itself can't be scripted. It sits behind a bot wall (a bare request
    gets a 403) and has no API. TMX Money's public GraphQL endpoint lists the
    same filings for every TSX symbol with a direct PDF link, no key and no
    quota. That is the document source here.
  - PDFs carry no XBRL. About 200 TSX companies are also listed in the US and
    file a 40-F (or a 10-K) on EDGAR, and *those* are tagged, so their numbers
    — ten-year history, as-filed statements, the EV bridge — come from EDGAR.
    Everyone else (Dollarama, Couche-Tard, Loblaw) gets the same numbers read
    off the statements they filed: sedar_statements.py finds the three primary
    statements in each year's PDF, reads them line by line, and drops any
    balance sheet that doesn't tie. statement_map.md records the filed line
    and page behind every figure. Read, not estimated.
  - A 40-F filer tags in IFRS and usually in CAD. trends.py and valuation.py
    read us-gaap concepts in USD, so `as_us_gaap` below renames the concepts
    and they run unchanged. Every money figure for such a filer is in its
    reporting currency, and the output says which.
  - Canadian quarterlies go to EDGAR as 6-Ks. Some are tagged, most aren't, so
    there is no LTM roll-forward here: fiscal year only.

Nothing in this file is imported by the 10-K path. `route.py` decides which
of the two a ticker goes to.
"""

import argparse
import io
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from sec_client import TICKER_MAP_MAX_AGE, TICKER_MAP_URL, SECClient, SECError  # first: it silences urllib3's LibreSSL warning

import requests  # noqa: E402

import sections
import sedar_statements
import statements
import trends
import valuation
from extract_10k import CONDENSE_MIN_CHARS, DEFAULT_OUT, risk_diff_markdown, write

TMX_URL = "https://app-money.tmx.com/graphql"
# Generic on purpose: SEC asks for a contact email in the User-Agent, TMX and
# its PDF host don't, so they don't get one.
TMX_UA = "Mozilla/5.0 (compatible; filed-10k-extractor)"
TMX_HEADERS = {"User-Agent": TMX_UA, "Origin": "https://money.tmx.com",
               "Referer": "https://money.tmx.com/"}

QUOTE_QUERY = """query($symbol: String, $locale: String) {
  q: getQuoteBySymbol(symbol: $symbol, locale: $locale) {
    symbol name price currency exchangeCode sector industry MarketCap shareOutStanding } }"""
FILINGS_QUERY = """query($symbol: String!, $fromDate: String, $toDate: String, $limit: Int) {
  f: getCompanyFilings(symbol: $symbol, fromDate: $fromDate, toDate: $toDate, limit: $limit) {
    filingDate description name urlToPdf } }"""


def tmx(query, **variables):
    """One call to TMX Money's GraphQL endpoint. Never cached: the quote is
    live and the filing list grows."""
    try:
        resp = requests.post(TMX_URL, json={"query": query, "variables": variables},
                             headers=TMX_HEADERS, timeout=30)
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        raise SECError("TMX Money didn't answer ({}: {})".format(type(e).__name__, e))
    if body.get("errors"):
        raise SECError("TMX Money rejected the query: {}".format(body["errors"][0].get("message")))
    return body["data"]


# --- which documents ------------------------------------------------------

TMX_PAGE = 2000  # the most getCompanyFilings returns, newest first


def list_filings(symbol, since, until):
    """Every filing between two dates. A bank files ~2,000 documents a quarter
    (each note issue gets a pricing supplement), so one call covers three
    months of RY and its annual report isn't in it. Walk back a page at a time.
    """
    out, seen = [], set()
    for _ in range(15):  # RY needs 10 for its last three years
        page = tmx(FILINGS_QUERY, symbol=symbol, limit=TMX_PAGE,
                   fromDate=since, toDate=until)["f"] or []
        for f in page:
            if f.get("urlToPdf") and f["urlToPdf"] not in seen:
                seen.add(f["urlToPdf"])
                out.append(f)
        oldest = min((f["filingDate"][:10] for f in page), default=since)
        if len(page) < TMX_PAGE or oldest <= since or oldest == until:
            break
        until = oldest
    return out


def doc_kind(f):
    """aif / mda / fs / annual_report, or None for everything else an issuer files.

    Matched on the document's own name; TMX's category alone is too wide
    ("Annual financial statements" also holds earnings-coverage calculations).
    A cross-listed filer's AIF is its 10-K or 40-F, and is named that way.
    """
    name, desc = (f.get("name") or "").lower(), (f.get("description") or "").lower()
    if "interim" in name or "interim" in desc:
        return None
    if "annual information form" in name or (
            "annual information form" in desc and re.search(r"form (10-k|40-f)", name)):
        return "aif"
    if name.startswith("md&a") or "management's discussion" in name:
        return "mda" if "annual" in desc else None
    if name.startswith("audited annual financial statements"):
        return "fs"
    if name.startswith("annual report") and "annual report" in desc:
        return "annual_report"
    return None


def annual_sets(filings):
    """Group a filing list into one {fs, mda, aif} set per fiscal year, newest
    first. The statements anchor the set; the MD&A and AIF are whichever of
    each was filed closest to them (same day for most issuers, a month later
    for an AIF)."""
    docs = {}
    for f in filings:
        kind = doc_kind(f)
        if kind and f.get("urlToPdf"):
            docs.setdefault(kind, []).append(f)

    def nearest(kind, day):
        near = [(abs((date.fromisoformat(d["filingDate"][:10]) - day).days), d)
                for d in docs.get(kind, [])]
        near = [n for n in near if n[0] <= 120]
        return min(near, key=lambda n: n[0])[1] if near else None

    # Oldest first, so the copy that anchors a year is the first one filed.
    # Loblaw files everything again in French a week later, under the same
    # name; the later copy waits in fs_alt in case the first can't be read.
    # ponytail: "first filed is the English one". A filer that files the French
    # copy first, or both the same day, needs a look at the text instead.
    out = []
    by_date = lambda d: d["filingDate"]
    anchors = sorted(docs.get("fs", []), key=by_date)
    # No separate statements at all (Loblaw, 2026): the annual report holds them.
    anchors += [d for d in sorted(docs.get("annual_report", []), key=by_date)
                if not any(abs((date.fromisoformat(d["filingDate"][:10])
                                - date.fromisoformat(a["filingDate"][:10])).days) < 120 for a in anchors)]
    for fs in sorted(anchors, key=by_date):
        day = date.fromisoformat(fs["filingDate"][:10])
        same = next((s for s in out if abs((day - s["day"]).days) < 120), None)
        if same:
            same["fs_alt"].append(fs)
            continue
        out.append({"day": day, "fs": fs, "fs_alt": [], "mda": nearest("mda", day),
                    "aif": nearest("aif", day), "annual_report": nearest("annual_report", day)})
    for s in out:
        del s["day"]
    return out[::-1]


# --- PDF -> text ------------------------------------------------------------

def pdf_text(data):
    """PDF bytes -> (text, bold runs).

    Bold runs do the job the <b> tags do in a 10-K: they mark headings for the
    condenser and risk headlines for the index. A PDF whose fonts don't say
    "Bold" in their name returns none, and the risk index is skipped.
    """
    from pypdf import PdfReader  # here, so the 10-K path never needs it installed

    reader = PdfReader(io.BytesIO(data))
    bold, run = [], []

    def visit(text, _cm, _tm, font, _size):
        is_bold = "bold" in str((font or {}).get("/BaseFont", "")).lower()
        if is_bold and text.strip():
            run.append(text)
        elif text.strip() and run:
            bold.append(sections._flat("".join(run)))
            run.clear()

    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text(visitor_text=visit) or "")
        except Exception:  # one unreadable page shouldn't lose the document
            pages.append("")
    if run:
        bold.append(sections._flat("".join(run)))

    # Running headers and footers repeat on every page ("DOLLARAMA INC.", a
    # page number). Left in, they land mid-sentence in every section.
    per_page = [[ln.strip() for ln in p.splitlines()] for p in pages]
    seen_on = Counter(ln for lines in per_page for ln in set(lines) if ln)
    furniture = {ln for ln, n in seen_on.items()
                 if len(ln) < 80 and n >= max(4, 0.4 * len(pages))}
    kept = [ln for lines in per_page for ln in lines
            if ln and ln not in furniture and not re.fullmatch(r"[\d\s|–-]{1,8}", ln)]
    text = re.sub(r"[ \t\xa0]+", " ", "\n".join(kept))
    return text, [b for b in bold if b and b not in furniture]


MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
# pypdf sometimes splits the last digit off a year ("FEBRUARY 1, 202 6").
DATE_RES = [
    re.compile(r"\b({})\s+(\d{{1,2}})\s*,?\s*(20\d)\s?(\d)\b".format("|".join(MONTHS)), re.I),
    re.compile(r"\b(\d{{1,2}})\s+({})\s*,?\s*(20\d)\s?(\d)\b".format("|".join(MONTHS)), re.I),
]


def fiscal_year_end(text, filed):
    """The fiscal year end a document reports on, read off its first pages.

    The year end is the date the cover repeats; the auditor's report date and
    the filing date are also there, but those fall within three weeks of the
    filing and the year end can't (statements take longer than that to audit).
    """
    filed = date.fromisoformat(filed[:10])
    found = Counter()
    for i, rx in enumerate(DATE_RES):
        for m in rx.finditer(text[:8000]):
            month, day = (m.group(1), m.group(2)) if i == 0 else (m.group(2), m.group(1))
            try:
                d = date(int(m.group(3) + m.group(4)), MONTHS.index(month.lower()) + 1, int(day))
            except ValueError:
                continue
            if timedelta(days=21) <= filed - d <= timedelta(days=200):
                found[d] += 1
    if not found:
        return None
    return max(found, key=lambda d: (found[d], d)).isoformat()


# --- sections ---------------------------------------------------------------

# Form 51-102F2 fixes what an AIF must cover, so the headings are close to
# standard. (key, title, pattern); key None is a boundary we don't keep.
AIF_HEADS = [
    ("corporate_structure", "Corporate structure", r"corporate structure"),
    ("business_development", "General development of the business",
     r"general development of (?:the|our) business"),
    ("business", "Description of the business",
     r"(?:narrative )?description of (?:the|our) business|business of the (?:corporation|company)"
     r"|describe the business|our business"),
    # Loblaw heads it "4. RISKS", Couche-Tard "Business Risks".
    ("risk_factors", "Risk factors", r"risk factors|risks and uncertainties"
                                     r"|(?:business |enterprise )?risks(?: and risk management)?"),
    ("dividends", "Dividends", r"dividends(?: and distributions)?|dividend policy"),
    ("capital_structure", "Capital structure",
     r"(?:description of )?(?:the |our )?(?:capital|share capital) structure"),
    ("indebtedness", "Material indebtedness", r"description of material indebtedness"),
    ("legal_proceedings", "Legal proceedings",
     r"legal proceedings(?: and regulatory actions)?"),
    (None, "", r"ratings|credit ratings|market for securities|trading price and volume"
               r"|prior sales|escrowed securities|directors and (?:executive )?officers"
               r"|audit committee(?: information)?|interests? of management and others in "
               r"material transactions|transfer agents?(?: and registrars?)?|material contracts"
               r"|interests? of experts|additional information|conflicts of interest"
               r"|explanatory notes|background|glossary|forward[- ]looking (?:statements|information)"),
]
# What can end a risk section inside an MD&A, where headings follow no form.
MDA_STOPS = (r"critical accounting (?:estimates|policies)(?: and judgments)?"
             r"|[^\n.]{0,80}(?:controls and procedures|internal controls? over financial reporting)[^\n.]{0,60}"
             r"|non-(?:gaap|ifrs)[^\n]{0,40}|outlook|subsequent events?|related party transactions"
             r"|share (?:information|data)|outstanding share data|additional information"
             r"|accounting (?:policies|standards)[^\n]{0,40}|liquidity and capital resources"
             r"|financial instruments|off-balance sheet arrangements|glossary"
             r"|dividends?|selected (?:annual|quarterly)[^\n]{0,40}")
MIN_HEADLINES = 5
HEAD_LINE = r"^[ \t]*(?:(?:item\s+)?\d{{1,2}}(?:\.\d{{1,2}})?[ \t]*[.:)–—-]?[ \t]+)?(?:{})[ \t]*[.:]?[ \t]*$"
RISK_RE = re.compile(HEAD_LINE.format(
    r"risk factors(?: and (?:risk )?management| that may affect future results)?"
    r"|risks and uncertainties|(?:enterprise )?risk management|managing risk"
    r"|risks? and risk management|(?:business |enterprise )?risks"), re.I | re.M)


def split_aif(text):
    """AIF text -> {key: {title, text}}. Same trick as the 10-K splitter: each
    heading also appears in the table of contents, where the next heading
    follows within a line, so the longest span is the real section."""
    hits = []
    for key, title, pattern in AIF_HEADS:
        for m in re.finditer(HEAD_LINE.format(pattern), text, re.I | re.M):
            hits.append((m.start(), key, title))
    hits.sort()
    best = {}
    for i, (start, key, title) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        if key and end - start > len(best.get(key, {}).get("text", "")):
            best[key] = {"title": title, "text": text[start:end].strip()}
    return {k: v for k, v in best.items() if len(v["text"]) >= sections.MIN_SECTION_CHARS}


def risk_section(aif_secs, mda_text):
    """-> (text, 'AIF' or 'MD&A'). An issuer puts its risk factors in one and
    a pointer in the other — Dollarama's AIF section is a page that says "see
    the MD&A". Take whichever is longer.

    ponytail: the MD&A section ends at the first heading in MDA_STOPS or the
    first all-caps line. An MD&A that sets its headings in sentence case and
    uses none of those names runs long. Fix by reading the PDF's outline.
    """
    aif = (aif_secs.get("risk_factors") or {}).get("text", "")
    mda = ""
    for m in RISK_RE.finditer(mda_text or ""):
        rest = mda_text[m.end():]
        # > 400 skips the heading's own sub-title and a table-of-contents hit
        ends = [x.start() for rx in (STOP_NAMED, STOP_CAPS) for x in rx.finditer(rest)
                if x.start() > 400]
        chunk = mda_text[m.start():m.end() + (min(ends) if ends else len(rest))].strip()
        if len(chunk) > len(mda):
            mda = chunk
    return (aif, "AIF") if len(aif) >= len(mda) else (mda, "MD&A")


STOP_NAMED = re.compile(HEAD_LINE.format(MDA_STOPS), re.I | re.M)
# An all-caps line ends the section unless it says RISK: banks set the risk
# section's own sub-headings in caps ("TOP AND EMERGING RISKS").
STOP_CAPS = re.compile(r"^(?![^\n]*RISK)[A-Z][A-Z &,'’/-]{11,70}$", re.M)


def split_documents(aif_text, mda_text):
    """-> (AIF sections, risk text, 'AIF' or 'MD&A')."""
    aif_secs = split_aif(aif_text)
    # Shopify and Enbridge file their 10-K as their AIF. It has Items, not AIF
    # headings, so it goes through the 10-K splitter instead.
    items = sections.pick_sections(aif_text) if aif_text else {}
    if "1A" in items and len(items["1A"]["text"]) > len(
            (aif_secs.get("risk_factors") or {}).get("text", "")):
        aif_secs = {("risk_factors" if k == "1A" else sections.ITEM_FILES[k]):
                    {"title": "Item {}. {}".format(k, v["title"]), "text": v["text"]}
                    for k, v in items.items() if k in sections.ITEM_FILES}
    risk_text, risk_from = risk_section(aif_secs, mda_text)
    return aif_secs, risk_text, risk_from


def write_section(out_dir, name, title, text, source, bold):
    """sections/<name>.md plus its condensed twin. Returns (path, summary path or None)."""
    write(out_dir / "sections" / (name + ".md"),
          "# {}\n\n_Source: {}_\n\n{}\n".format(title, source, text))
    rel, summary = "sections/{}.md".format(name), None
    blocks = sections.condense(text, bold)
    kept = sum(len(b["text"]) for b in blocks)
    if blocks and len(text) >= CONDENSE_MIN_CHARS and kept < 0.9 * len(text):
        write(out_dir / "summaries" / (name + ".md"),
              sections.condensed_markdown(blocks, title, rel, len(text)))
        summary = "summaries/{}.md".format(name)
    return rel, summary


# --- EDGAR, for the numbers -------------------------------------------------

# ifrs-full concept -> the us-gaap concept trends.py already looks for.
# Like-for-like lines, with one stand-in: IFRS finance costs carry accretion
# and FX as well as interest, so they map to InterestAndDebtExpense, which
# trends.py only reads when no InterestExpense is tagged. IFRS 16 has no
# operating/finance lease split, so leases aren't mapped.
IFRS_TO_GAAP = {
    "Revenue": "Revenues",
    "CostOfSales": "CostOfRevenue",
    "GrossProfit": "GrossProfit",
    "ProfitLossFromOperatingActivities": "OperatingIncomeLoss",
    "ProfitLossAttributableToOwnersOfParent": "NetIncomeLoss",
    "ProfitLoss": "ProfitLoss",
    "ProfitLossBeforeTax":
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeTaxExpenseContinuingOperations": "IncomeTaxExpenseBenefit",
    "InterestExpense": "InterestExpense",
    "FinanceCosts": "InterestAndDebtExpense",
    "DilutedEarningsLossPerShare": "EarningsPerShareDiluted",
    "BasicAndDilutedEarningsLossPerShare": "EarningsPerShareBasicAndDiluted",
    "BasicEarningsLossPerShare": "EarningsPerShareBasic",
    "AdjustedWeightedAverageShares": "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageShares": "WeightedAverageNumberOfSharesOutstandingBasic",
    "CashFlowsFromUsedInOperatingActivities": "NetCashProvidedByUsedInOperatingActivities",
    "DepreciationAndAmortisationExpense": "DepreciationDepletionAndAmortization",
    "AdjustmentsForDepreciationAndAmortisationExpense": "DepreciationAndAmortization",
    "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities":
        "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireOrRedeemEntitysShares": "PaymentsForRepurchaseOfCommonStock",
    "DividendsPaidToEquityHoldersOfParentClassifiedAsFinancingActivities":
        "PaymentsOfDividendsCommonStock",
    "DividendsPaidClassifiedAsFinancingActivities": "PaymentsOfDividends",
    "SellingGeneralAndAdministrativeExpense": "SellingGeneralAndAdministrativeExpense",
    "ResearchAndDevelopmentExpense": "ResearchAndDevelopmentExpense",
    "AdjustmentsForSharebasedPayments": "ShareBasedCompensation",
    "Assets": "Assets",
    "Liabilities": "Liabilities",
    "EquityAttributableToOwnersOfParent": "StockholdersEquity",
    "Equity": "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    "NoncontrollingInterests": "MinorityInterest",
    "CashAndCashEquivalents": "CashAndCashEquivalentsAtCarryingValue",
    "Inventories": "InventoryNet",
    "CurrentAssets": "AssetsCurrent",
    "CurrentLiabilities": "LiabilitiesCurrent",
    "TradeAndOtherCurrentReceivables": "AccountsReceivableNetCurrent",
    "TradeAndOtherCurrentPayables": "AccountsPayableCurrent",
    "PropertyPlantAndEquipment": "PropertyPlantAndEquipmentNet",
    "Goodwill": "Goodwill",
    "IntangibleAssetsOtherThanGoodwill": "IntangibleAssetsNetExcludingGoodwill",
    "RetainedEarnings": "RetainedEarningsAccumulatedDeficit",
    "LongtermBorrowings": "LongTermDebtNoncurrent",
    "CurrentPortionOfLongtermBorrowings": "LongTermDebtCurrent",
    "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings": "DebtCurrent",
    "ShorttermBorrowings": "ShortTermBorrowings",
    "Borrowings": "DebtLongtermAndShorttermCombinedAmount",
}
ANNUAL_FORMS = ("40-F", "20-F", "6-K", "10-K")


def reporting_currency(companyfacts):
    """CAD or USD — the unit this filer tags its balance sheet in."""
    facts = companyfacts.get("facts", {})
    for tax in ("ifrs-full", "us-gaap"):
        units = facts.get(tax, {}).get("Assets", {}).get("units", {})
        if units:
            return max(units, key=lambda u: len(units[u]))
    return None


def as_us_gaap(companyfacts, currency):
    """A 40-F filer's companyfacts, reshaped so trends.py reads it as-is.

    Three renames, no arithmetic: ifrs-full concepts take their us-gaap names;
    the reporting currency's unit key becomes "USD", which is the key trends.py
    picks (the values stay in the reporting currency — nothing is converted);
    and every row's form becomes "10-K", which is trends.py's test for "annual
    report". That last one is safe because trends.py also requires a 340-400
    day period, so a tagged quarterly 6-K still can't pass as a year.
    """
    facts = companyfacts.get("facts", {})
    out = {}

    def add(name, data):
        units = {}
        for unit, rows in data.get("units", {}).items():
            key = unit.replace(currency, "USD") if currency else unit
            units[key] = [dict(r, form="10-K") for r in rows
                          if r.get("form", "").startswith(ANNUAL_FORMS)]
        if any(units.values()) and name not in out:
            out[name] = dict(data, units=units)

    for name, data in facts.get("us-gaap", {}).items():
        add(name, data)
    for name, data in facts.get("ifrs-full", {}).items():
        add(IFRS_TO_GAAP.get(name, name), data)
    return {"facts": {"us-gaap": out, "dei": facts.get("dei", {})}}


NAME_NOISE = re.compile(
    r"\b(the|inc|incorporated|corp|corporation|co|company|ltd|limited|plc|adr|cn|can|de|new)\b")


def _name_key(name):
    name = re.sub(r"/[A-Za-z]{2,4}/?$", "", name.lower().replace("&", " and "))
    # TMX names the security, not the company: "CGI Inc. Class A Subordinate Voting Shares"
    name = re.sub(r"\b(class [a-z]\b|common shares|subordinate voting|ordinary shares).*$", "", name)
    return " ".join(NAME_NOISE.sub(" ", re.sub(r"[^a-z0-9 ]", " ", name)).split())


def edgar_match(client, company):
    """TSX company name -> (cik, US ticker) if the same company files on EDGAR.

    ponytail: exact match on the name with the legal suffixes stripped. A
    company whose SEC name differs from its TSX name is missed and falls back
    to documents only; pass --cik to link it by hand.
    """
    want = _name_key(company)
    data = client.get_json(TICKER_MAP_URL, max_age=TICKER_MAP_MAX_AGE)
    hits = [e for e in data.values() if _name_key(e["title"]) == want]
    for e in hits:  # the listed security first; the map lists OTC lines too
        subs = client.submissions(int(e["cik_str"]))
        if set(subs["filings"]["recent"]["form"]) & {"40-F", "10-K"}:
            return int(e["cik_str"]), (subs.get("tickers") or [e["ticker"]])[0], subs
    return None, None, None


def numbers_from_edgar(client, cik, subs, period, years, out_dir):
    """Everything tagged: history, statements, and what the valuation needs.
    -> (trends data, adapted facts, statements, auditor, annual accession, currency)"""
    raw = client.companyfacts(cik)
    currency = reporting_currency(raw)
    facts = as_us_gaap(raw, currency)
    data = trends.build(facts, max_years=years, as_of=period, with_ttm=False)
    fyes = data["fiscal_year_ends"]
    stmts, auditor, accession = [], None, None
    if fyes:
        accession = trends.filing_accessions(facts["facts"]["us-gaap"], fyes).get(fyes[-1])
    if accession:
        try:
            stmts, auditor = statements.fetch_statements(client, cik, accession.replace("-", ""))
        except SECError as e:
            print("  as-filed statements: skipped ({})".format(e))
    write_statements(out_dir, stmts)
    return data, facts, stmts, auditor, accession, currency


def write_statements(out_dir, stmts):
    for st in stmts:
        write(out_dir / "statements" / (st["name"] + ".md"),
              statements.to_markdown(st["title"], st["table"]))
        write(out_dir / "statements" / (st["name"] + ".json"),
              json.dumps({"title": st["title"], **st["table"]}, indent=2))


# concept -> the row name trends.md prints it under, for statement_map.md
METRIC_OF = {c: name.replace("_", " ") for table in (trends.DURATION_CONCEPTS, trends.INSTANT_CONCEPTS)
             for name, concepts in table.items() for c in concepts}


_READ = {}


def read_statements(pdfs, doc):
    """sedar_statements.read_filing, once per document: the year-end lookup
    and the numbers both want it, and a 170-page annual report takes seconds."""
    url = doc["urlToPdf"]
    if url not in _READ:
        try:
            _READ[url] = sedar_statements.read_filing(pdfs.get(url, binary=True), doc["filingDate"][:10])
        except Exception as e:  # one unreadable PDF costs two years, not the run
            print("    filed {}: unreadable ({}: {})".format(doc["filingDate"][:10], type(e).__name__, e))
            _READ[url] = None
    return _READ[url]


def numbers_from_sedar(pdfs, sets, period, years, out_dir, fallback_currency):
    """The same numbers for a company EDGAR has never heard of, read off the
    statements it filed. One PDF per fiscal year, two years in each.
    -> (trends data, facts, statements, currency, parsed filings) or None."""
    parsed = []
    for s in sets[:max(1, years - 1)]:
        f = None
        for doc in [s["fs"]] + s.get("fs_alt", []):
            filed = doc["filingDate"][:10]
            f = read_statements(pdfs, doc)
            if f and f["values"]:
                break
        if not f or not f["values"]:
            print("    filed {}: no statements found in the PDF".format(filed))
            continue
        print("    year ended {} (filed {}): {} lines read — {}".format(
            f["period"], filed, len(f["values"]), "; ".join(f["checks"]) or "no tie-out possible"))
        parsed.append(f)
    if not parsed or parsed[0]["period"] != period:
        return None
    facts = sedar_statements.as_companyfacts(parsed)
    currency = parsed[0]["currency"] or fallback_currency
    data = trends.build(facts, max_years=years, as_of=period, with_ttm=False)
    stmts = sedar_statements.statement_tables(parsed[0])
    write_statements(out_dir, stmts)
    write(out_dir / "statement_map.md", sedar_statements.map_markdown(parsed, METRIC_OF))
    return data, facts, stmts, currency, parsed


# --- the run ----------------------------------------------------------------

TSX_PREFIX = re.compile(r"^(TSX|TSE)[:.]", re.I)
TSX_SUFFIX = re.compile(r"[.:-](TO|CA)$", re.I)


def parse_ticker(raw):
    """Any way of writing a Toronto symbol -> (folder, TMX symbol, Yahoo symbol).

    'DOL', 'TSX:DOL' and 'dol.to' are all ('DOL-TO', 'DOL', 'DOL.TO').
    Share classes: GIB.A -> TMX 'GIB.A', Yahoo 'GIB-A.TO'. The folder keeps
    its -TO so Loblaw (TSX:L) and Loews (L) don't land in the same place.
    """
    base = TSX_SUFFIX.sub("", TSX_PREFIX.sub("", raw.strip()))
    base = SECClient.normalize_ticker(base)  # validates; dots -> dashes
    return base + "-TO", base.replace("-", "."), base + ".TO"


def display_ticker(folder):
    """'GIB-A-TO' -> 'TSX:GIB.A', the way a terminal writes it."""
    return "TSX:" + folder[:-3].replace("-", ".")


def summary_markdown(meta, docs, written, summaries, data, val, risk_heads, risk_from,
                     diff, stmt_names, source):
    tagged = bool(source)
    cur = meta["currency"] or meta["quote_currency"] or "CAD"
    lines = [
        "# {} ({}) — annual filings for fiscal year ended {}".format(
            meta["company"], meta["ticker"], meta["period"]),
        "",
        "A TSX issuer files no 10-K. This is the same material from the three documents "
        "it files on SEDAR+ instead, fetched through TMX Money.",
        "",
        "| Document | Filed | 10-K equivalent |",
        "|---|---|---|",
    ]
    for kind, label, equiv in (("aif", "Annual Information Form", "Items 1, 1A, 3, 5"),
                               ("mda", "Annual MD&A", "Item 7"),
                               ("fs", "Audited annual financial statements", "Item 8")):
        d = docs.get(kind)
        lines.append("| {} | {} | {} |".format(
            "[{}]({})".format(label, d["urlToPdf"]) if d else label + " — not found",
            d["filingDate"][:10] if d else "—", equiv))
    lines.append("")

    fyes = data["fiscal_year_ends"]
    if tagged:
        y = fyes[-1]
        lines += [
            "## Key numbers — fiscal {} ({} millions)".format(y[:4], cur),
            "",
            ("From the XBRL this company files with the SEC (CIK {}).".format(meta["cik"])
             if source == "edgar" else
             "Read off the audited statements this company filed on SEDAR+. It files nothing "
             "with the SEC, so there is no XBRL: each figure is a line of the PDF, and "
             "`statement_map.md` lists the line and the page.")
            + " **Every money figure in this folder is in {}**, including where a table "
              "prints a bare `$`.".format(cur),
            "",
            "| | |",
            "|---|---|",
        ]
        for label, key in (("Revenue", "revenue"), ("Operating income", "operating_income"),
                           ("Net income", "net_income"), ("Cash from ops", "operating_cash_flow"),
                           ("Capex", "capex"), ("Total assets", "total_assets"),
                           ("Total equity", "total_equity"), ("Cash", "cash")):
            v = data["series"].get(key, {}).get("values", {}).get(y)
            lines.append("| {} | {} |".format(label, "—" if v is None else "{:,.0f}".format(v / 1e6)))
        eps = data["series"]["eps_diluted"]["values"].get(y)
        lines.append("| EPS diluted | {} |".format("—" if eps is None else "{:,.2f}".format(eps)))
        lines += ["", "{} fiscal years of history in `trends.md` and `model.csv`. No LTM "
                      "column: fiscal year only.".format(len(fyes)), ""]
    else:
        lines += [
            "## Key numbers",
            "",
            "None. This company doesn't file with the SEC, and the three primary statements "
            "couldn't be read out of its PDF (or the balance sheet didn't tie, and was "
            "dropped). The statements are in `sections/financial_statements.md` as text, "
            "exactly as filed. Nothing here is estimated to fill the gap.",
            "",
        ]

    q = val.get("quote") or {}
    if q.get("price"):
        lines += ["## Valuation", "",
                  "- Price: {:,.2f} {} — {}".format(q["price"], q.get("currency") or "", q.get("source"))]
        if val.get("market_cap"):
            lines.append("- Market cap: {:,.0f} million ({})".format(
                val["market_cap"] / 1e6, val.get("market_cap_basis") or "price × shares"))
        ev = val["bridge"].get("enterprise_value")
        if ev:
            lines.append("- Enterprise value: {:,.0f} million".format(ev / 1e6))
            for label, k in (("EV / sales", "ev_sales"), ("EV / EBITDA", "ev_ebitda"), ("P/E", "pe")):
                if val["multiples"].get(k):
                    lines.append("- {}: {:.1f}x".format(label, val["multiples"][k]))
        if val.get("currency_note"):
            lines.append("- " + val["currency_note"])
        lines.append("")

    lines += ["## Risk factors", ""]
    if risk_heads:
        lines.append("{} headlines indexed in `risk_headings.md`, from the {}.".format(
            len(risk_heads), risk_from))
    elif "risk_factors" in written:
        lines.append("Full text in `sections/risk_factors.md`, from the {}. No headline index: "
                     "this PDF doesn't mark its headings in a bold font, which is what the "
                     "index reads.".format(risk_from))
    else:
        lines.append("No risk section found under a recognisable heading in the AIF or the MD&A.")
    if diff:
        lines.append("Against last year: **{} added, {} removed, {} reworded** — `risk_diff.md`.".format(
            len(diff["added"]), len(diff["removed"]), len(diff["reworded"])))
    lines += ["", "## Files", "", "| File | What |", "|---|---|"]
    for key, rel in written.items():
        lines.append("| `{}` | {}{} |".format(
            rel, key.replace("_", " "),
            " — condensed: `{}`".format(summaries[key]) if summaries.get(key) else ""))
    for name in stmt_names:
        lines.append("| `statements/{}.md` | {} |".format(
            name, "as filed with the SEC" if source == "edgar" else "as filed on SEDAR+"))
    if source == "sedar":
        lines.append("| `statement_map.md` | the filed line and page behind every number, "
                     "and the tie-outs |")
    if tagged:
        lines += ["| `trends.md`, `model.csv` | {} years of tagged history |".format(len(fyes)),
                  "| `valuation.md` | price, market cap, EV bridge |"]
    lines += ["| `AIF.pdf`, `MDA.pdf`, `financial_statements.pdf` | raw, exactly as filed |",
              "| `full_text.txt` | all three documents as plain text |", ""]
    return "\n".join(lines)


def run(args):
    folder, symbol, yahoo = parse_ticker(args.ticker)
    sec = SECClient(refresh=args.refresh)
    pdfs = SECClient(refresh=args.refresh)  # same disk cache, no SEC contact header
    pdfs.session.headers["User-Agent"] = TMX_UA

    try:
        quote_tmx = tmx(QUOTE_QUERY, symbol=symbol, locale="en")["q"]
    except SECError as e:
        if "find any results" not in str(e):
            raise
        quote_tmx = None
    if not quote_tmx or not quote_tmx.get("name"):
        raise SECError("{} is not a symbol TMX Money knows. Check the ticker on money.tmx.com "
                       "(share classes are written RCI.B.TO).".format(symbol))
    company = quote_tmx["name"]
    ticker = display_ticker(folder)

    # Where the numbers will come from decides how far back to list: EDGAR has
    # the whole history in one file, the PDFs have two years apiece.
    cik, us_ticker, subs = args.cik, None, None
    if cik:
        subs = sec.submissions(cik)
        us_ticker = (subs.get("tickers") or [None])[0]
    else:
        cik, us_ticker, subs = edgar_match(sec, company)

    today = date.today()
    since = (args.year or today.year) - (2 if cik else args.years + 1)
    listing = list_filings(symbol, "{}-01-01".format(since), today.isoformat())
    sets = annual_sets(listing)
    if not sets:
        raise SECError("no audited annual financial statements listed for {} on TMX Money "
                       "since {}.".format(symbol, since))

    def load(doc):
        return pdf_text(pdfs.get(doc["urlToPdf"], binary=True)) if doc else ("", [])

    # Which year: the newest, or the one whose statements end in --year.
    chosen = None
    for i, s in enumerate(sets):
        fs_text, fs_bold = load(s["fs"])
        period = fiscal_year_end(fs_text, s["fs"]["filingDate"])
        if not period:
            # An annual report opens with photographs, not dates (Loblaw). The
            # statements inside it head every page with the year end.
            period = (read_statements(pdfs, s["fs"]) or {}).get("period")
        if period and (not args.year or period[:4] == str(args.year)):
            chosen = i
            break
        if not period and not args.year:
            raise SECError("couldn't read a fiscal year end off {}'s annual statements "
                           "(filed {}).".format(symbol, s["fs"]["filingDate"][:10]))
    if chosen is None:
        raise SECError("no annual statements for fiscal year {} listed for {}.".format(
            args.year, symbol))
    docs = sets[chosen]

    out_dir = Path(args.out or DEFAULT_OUT) / folder / period
    if (out_dir / "SUMMARY.md").exists() and not args.refresh and not args.rebuild:
        print("Already extracted: {}".format(out_dir))
        print("Pass --refresh to re-download (and to re-price at a current quote).")
        return out_dir

    print("{} — {} — annual filings for FY ended {}".format(ticker, company, period))
    print("  fetching documents from TMX Money...")
    aif_text, aif_bold = load(docs["aif"])
    mda_text, mda_bold = load(docs["mda"])
    # Banks file one annual report holding the MD&A and the statements, and no
    # separate MD&A. Read it as the MD&A rather than report none.
    if not mda_text and docs["annual_report"]:
        docs["mda"] = docs["annual_report"]
        mda_text, mda_bold = load(docs["mda"])
    for kind, name in (("aif", "AIF.pdf"), ("mda", "MDA.pdf"), ("fs", "financial_statements.pdf")):
        if docs.get(kind):
            path = out_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(pdfs.get(docs[kind]["urlToPdf"], binary=True))
    write(out_dir / "full_text.txt", "\n\n".join(
        "===== {} =====\n\n{}".format(label, text)
        for label, text in (("ANNUAL INFORMATION FORM", aif_text), ("MD&A", mda_text),
                            ("FINANCIAL STATEMENTS", fs_text)) if text))

    written, summaries = {}, {}

    def section(key, name, title, text, doc, bold):
        written[key], summaries[key] = write_section(
            out_dir, name, title, text, doc["urlToPdf"], bold)

    aif_secs, risk_text, risk_from = split_documents(aif_text, mda_text)
    for key, sec_ in aif_secs.items():
        if key != "risk_factors":
            section(key, "aif_" + key, "AIF — " + sec_["title"], sec_["text"], docs["aif"], aif_bold)
    risk_bold = aif_bold if risk_from == "AIF" else mda_bold
    if risk_text:
        section("risk_factors", "risk_factors", "Risk factors (from the {})".format(risk_from),
                risk_text, docs["aif" if risk_from == "AIF" else "mda"], risk_bold)
    if aif_text:
        section("aif", "aif", "Annual Information Form", aif_text, docs["aif"], aif_bold)
    if mda_text:
        section("mda", "mda", "Management's Discussion and Analysis", mda_text, docs["mda"], mda_bold)
    section("financial_statements", "financial_statements",
            "Audited annual financial statements", fs_text, docs["fs"], fs_bold)
    print("  documents: AIF {:,} chars, MD&A {:,}, statements {:,} — {} sections".format(
        len(aif_text), len(mda_text), len(fs_text), len(written)))

    risk_heads = sections.risk_headings(None, risk_text, risk_bold)
    if len(risk_heads) < MIN_HEADLINES:
        risk_heads = []  # two bold phrases in 70k characters is not an index
    if risk_heads:
        write(out_dir / "risk_headings.md",
              "# Risk factors — {} headlines\n\n_Full text: sections/risk_factors.md_\n\n".format(
                  len(risk_heads)) + "\n".join("- {}".format(h) for h in risk_heads) + "\n")
    print("  risk section: {:,} chars from the {}, {} headlines".format(
        len(risk_text), risk_from, len(risk_heads)))

    diff = None
    if risk_heads and not args.no_diff and chosen + 1 < len(sets):
        prior = sets[chosen + 1]
        p_aif, p_aif_bold = load(prior["aif"])
        p_mda, p_mda_bold = load(prior["mda"] or prior["annual_report"])
        _, p_text, p_from = split_documents(p_aif, p_mda)
        p_heads = sections.risk_headings(None, p_text, p_aif_bold if p_from == "AIF" else p_mda_bold)
        # Like for like only. Last year's risks read from a different document,
        # or a PDF whose fonts gave up a tenth as many headlines, diffs as
        # "200 risks added" when nothing changed.
        if p_from != risk_from or len(p_heads) < 0.5 * len(risk_heads):
            print("  risk diff: skipped (last year's risk section doesn't line up: "
                  "{} headlines from the {})".format(len(p_heads), p_from))
        else:
            diff = sections.diff_risks(risk_heads, p_heads)
            p_period = fiscal_year_end(load(prior["fs"])[0], prior["fs"]["filingDate"]) or "prior year"
            write(out_dir / "risk_diff.md", risk_diff_markdown(diff, period, p_period))
            print("  risk diff: {} added, {} removed, {} reworded".format(
                len(diff["added"]), len(diff["removed"]), len(diff["reworded"])))

    # Numbers: tagged, from EDGAR, when the company files there. Otherwise
    # read off the statements it filed in Canada.
    stmts, auditor, accession, currency, facts = [], None, None, None, {"facts": {}}
    data, source = None, None
    if cik:
        print("  on EDGAR as {} (CIK {}) — fetching tagged history...".format(subs["name"], cik))
        data, facts, stmts, auditor, accession, currency = numbers_from_edgar(
            sec, cik, subs, period, args.years, out_dir)
        source = "edgar"
    else:
        print("  not on EDGAR — reading the numbers off the filed statements...")
        got = numbers_from_sedar(pdfs, sets[chosen:], period, args.years, out_dir,
                                 quote_tmx.get("currency") or "CAD")
        if got:
            data, facts, stmts, currency, parsed = got
            source = "sedar"
    if data is None:
        print("  no statements could be read — documents only")
        data = trends.build(facts, with_ttm=False)
    tagged = bool(data["fiscal_year_ends"])
    if tagged:
        print("  trends: {} fiscal years in {}, statements: {}".format(
            len(data["fiscal_year_ends"]), currency, ", ".join(s["name"] for s in stmts) or "none"))
    else:
        source = None
    if not tagged:
        # One empty column for the year the documents cover, so everything
        # downstream (and the web page, which is built around a current year)
        # has a year to hang the price and the files on. Every figure in it is
        # None and prints as a blank.
        data["fiscal_year_ends"] = [period]
        for entry in data["series"].values():
            entry["values"] = {period: None}
        data["derived"] = trends.derive(data["series"], [period])
    fyes = data["fiscal_year_ends"]
    data["statement_lines"] = {"membership_fee_income": None, "membership_fee_column": None,
                               "source": None, "membership_ltm": None}
    data["currency"] = currency
    write(out_dir / "trends.json", json.dumps(data, indent=2))
    write(out_dir / "xbrl_by_year.json", json.dumps({
        "fiscal_year_ends": fyes,
        "concepts": trends.tagged_by_year(facts["facts"].get("us-gaap", {}), fyes)}, indent=2))
    note = "_All money figures in {}._\n\n".format(currency)
    if source == "sedar":
        note = ("_All money figures in {}. Read off the statements filed on SEDAR+, not XBRL: "
                "where this says a line is \"not tagged\", no filed line matched it. "
                "`statement_map.md` has the line and page behind each figure._\n\n".format(currency))
    if tagged:
        md = trends.to_markdown(data, company)
        if source == "sedar":
            md = md.replace("Source: SEC XBRL companyfacts (the company's own tagged numbers, as filed).",
                            "Source: the audited statements filed on SEDAR+, read from the PDFs.")
        write(out_dir / "trends.md", note + md)
        write(out_dir / "model.csv", trends.to_csv(data, ticker, company))

    # Price. The quote has to be in the currency the statements are in, or the
    # multiples divide one currency by another: a CAD reporter is priced off
    # its Toronto line, a USD reporter (Nutrien, Shopify) off its US one.
    print("  share count and valuation...")
    cover, awards = None, {}
    if accession:
        try:
            cover, awards = valuation.gather(sec, cik, {
                "accession": accession, "accession_plain": accession.replace("-", "")}, facts)
        except Exception as e:  # a 40-F with no FilingSummary; TMX's count stands in
            print("    cover-page share count: unavailable ({})".format(e))
    in_usd = currency == "USD" and us_ticker
    if args.year:
        # Same rule as the 10-K path: today's price over a 2019 share count is
        # two eras multiplied together. A 10-K has a cover-page float to fall
        # back on; a 40-F and an AIF state none, so a historical run is unpriced.
        quote = {"price": None, "error": "--year: no live quote over a historical share "
                                         "count, and these filings state no public float"}
        tmx_now = None
    else:
        quote = valuation.live_quote(us_ticker if in_usd else yahoo)
        tmx_now = quote_tmx.get("shareOutStanding")
    if not args.year and not quote.get("price") and not in_usd and quote_tmx.get("price"):
        quote = {"price": float(quote_tmx["price"]), "currency": quote_tmx.get("currency") or "CAD",
                 "as_of": datetime.now().isoformat(timespec="minutes"),
                 "source": "TMX Money quote — {}".format(symbol), "is_live": True}
    # A TSX-only company that reports in US dollars (Constellation, NFI) has no
    # US line to price it off. Convert the Toronto price at today's rate, and
    # say so, rather than divide CAD by USD.
    have, want = quote.get("currency"), currency
    if quote.get("price") and have != want and {have, want} == {"CAD", "USD"}:
        pair = "{}{}=X".format(have, want)
        fx = valuation.live_quote(pair)
        if fx.get("price"):
            quote = dict(quote, price=quote["price"] * fx["price"], currency=want,
                         source="{} — {:,.2f} {} converted at {:.4f} ({})".format(
                             quote.get("source"), quote["price"], have, fx["price"], pair))
    tmx_shares = tmx_now  # today's count; no use to a historical run
    val = valuation.build(
        data["series"], data["derived"], fyes[-1], cover, awards or {}, quote,
        shares_override=None if cover else tmx_shares)
    if not cover and tmx_shares:
        val["market_cap_basis"] = "live price × {:,.0f} shares outstanding per TMX Money".format(tmx_shares)
        # The page reads the share count off shares.cover_page.
        val["shares"]["cover_page"] = {"total": tmx_shares, "as_of": today.isoformat(),
                                       "classes": [], "source": "TMX Money (not a filing)"}
    if currency and quote.get("currency") and quote["currency"] != currency:
        val["currency_note"] = ("Price is in {} but the statements are in {} — the market cap "
                                "and every multiple mix the two. Pass --cik or treat them as "
                                "wrong.".format(quote["currency"], currency))
    val["history"] = {}
    write(out_dir / "valuation.json", json.dumps(val, indent=2))
    if tagged:
        write(out_dir / "valuation.md", "_All money figures in {}._\n\n".format(currency)
              + valuation.to_markdown(val, company, ticker)
              .replace("comes out of the 10-K", "comes out of the annual filings")
              .replace("item8_financial_statements.md", "financial_statements.md"))
    print("    price: {} · market cap {}".format(
        "—" if not quote.get("price") else "{:,.2f} {}".format(quote["price"], quote.get("currency")),
        valuation._m(val["market_cap"])))

    meta = {
        "ticker": ticker,
        "folder": folder,
        "numbers_source": {"edgar": "XBRL filed with the SEC",
                           "sedar": "statements filed on SEDAR+, read from the PDF"}.get(source),
        # The page prints a bare $, so the currency rides along in the name.
        "company": "{} — figures in {}".format(company, currency) if currency else company,
        "cik": cik,
        "sic": "",
        "sic_description": " — ".join(x for x in (quote_tmx.get("sector"), quote_tmx.get("industry")) if x),
        "exchanges": ["TSX"],
        "currency": currency,
        "quote_currency": quote.get("currency"),
        "period": period,
        "filed": docs["fs"]["filingDate"][:10],
        "source_url": "https://money.tmx.com/en/quote/{}/filings".format(symbol),
        "documents": {k: docs[k]["urlToPdf"] for k in ("aif", "mda", "fs") if docs.get(k)},
        "edgar_accession": accession,
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
        "auditor_name": (auditor or {}).get("Auditor Name"),
        "auditor_location": (auditor or {}).get("Auditor Location"),
        "auditor_firm_id": (auditor or {}).get("Auditor Firm ID"),
    }
    write(out_dir / "metadata.json", json.dumps(meta, indent=2))
    write(out_dir / "SUMMARY.md", summary_markdown(
        dict(meta, company=company), docs, written, summaries, data, val, risk_heads,
        risk_from, diff, [s["name"] for s in stmts], source))

    print("\nDone: {}".format(out_dir))
    print("Start with SUMMARY.md. SEC requests: {}, cache hits: {}".format(
        sec.requests_made, sec.cache_hits + pdfs.cache_hits))
    return out_dir


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ticker", help="TSX ticker: DOL, TSX:DOL or DOL.TO; share classes GIB.A")
    p.add_argument("--year", type=int, help="fiscal year to pull instead of the latest")
    p.add_argument("--cik", type=int, help="link to this SEC filer for the tagged numbers, "
                                           "when the name match misses it")
    p.add_argument("--years", type=int, default=10, help="years of XBRL history (default 10)")
    p.add_argument("--no-diff", action="store_true", help="skip the prior-year risk factor diff")
    p.add_argument("--refresh", action="store_true", help="ignore the cache and re-download")
    p.add_argument("--rebuild", action="store_true",
                   help="regenerate every output from the cached downloads, with a fresh quote")
    p.add_argument("--out", help="output directory (default: ../filings)")
    args = p.parse_args()
    try:
        run(args)
    except SECError as e:
        print("ERROR: {}".format(e), file=sys.stderr)
        sys.exit(1)


def demo():
    """python3 extract_tsx.py --self-check — offline, nothing touches the network."""
    for way in ("ry.to", "RY", "TSX:RY", "tsx:ry", "RY-TO", "RY:CA"):
        assert parse_ticker(way) == ("RY-TO", "RY", "RY.TO"), way
    for way in ("GIB.A.TO", "GIB.A", "TSX:GIB.A", "GIB-A"):
        assert parse_ticker(way) == ("GIB-A-TO", "GIB.A", "GIB-A.TO"), way
    assert display_ticker("GIB-A-TO") == "TSX:GIB.A" and display_ticker("L-TO") == "TSX:L"
    try:
        parse_ticker("../etc")
        raise AssertionError("a path is not a ticker")
    except SECError:
        pass

    # Year end is the repeated date, not the audit report three weeks before filing.
    cover = ("Consolidated Financial Statements\nFebruary 1, 202 6 and February 2, 2025\n"
             "As at February 1, 2026\nIndependent auditor's report dated March 20, 2026")
    assert fiscal_year_end(cover, "2026-03-24") == "2026-02-01", fiscal_year_end(cover, "2026-03-24")
    assert fiscal_year_end("no dates here", "2026-03-24") is None

    f = lambda name, desc, day: {"name": name, "description": desc, "filingDate": day, "urlToPdf": "u"}
    assert doc_kind(f("MD&A", "Interim MD&A", "")) is None
    sets = annual_sets([
        f("MD&A", "Annual MD&A", "2026-03-24"), f("MD&A", "Interim MD&A", "2026-06-11"),
        f("Audited annual financial statements", "Annual financial statements", "2026-03-24"),
        f("Annual information form", "Annual information form", "2026-04-21"),
        f("Audited annual financial statements", "Annual financial statements", "2025-04-03"),
    ])
    assert len(sets) == 2 and sets[0]["aif"]["filingDate"] == "2026-04-21", sets
    assert sets[1]["aif"] is None and sets[1]["mda"] is None, sets[1]

    # The table of contents names every heading; the real section is the long one.
    body = "x " * 300
    aif = ("CONTENTS\nRisk Factors\nDividends\n\n5 BUSINESS OF THE CORPORATION\n" + body
           + "\n6 RISK FACTORS\nSee the MD&A.\n" + "y " * 150 + "\nDIVIDENDS\n" + body)
    secs = split_aif(aif)
    assert set(secs) == {"business", "risk_factors", "dividends"}, set(secs)
    assert secs["risk_factors"]["text"].startswith("6 RISK FACTORS")
    assert "risk_factors" in split_aif(aif.replace("6 RISK FACTORS", "6 – Risk Factors"))
    mda = "Results\n" + body + "\nRisks and Uncertainties\n" + "z " * 2000 + "\nCRITICAL ACCOUNTING ESTIMATES\nmore"
    text, where = risk_section(secs, mda)
    assert where == "MD&A" and text.startswith("Risks and Uncertainties"), (where, text[:40])
    assert "CRITICAL" not in text and "more" not in text

    # IFRS in CAD reads as us-gaap; a tagged quarter doesn't become a year.
    row = lambda start, end, val, form: {"start": start, "end": end, "val": val, "form": form,
                                         "filed": "2026-02-20", "accn": "0001-26-000001"}
    raw = {"facts": {"ifrs-full": {
        "Revenue": {"units": {"CAD": [row("2025-01-01", "2025-12-31", 100.0, "40-F"),
                                      row("2025-01-01", "2025-03-31", 20.0, "6-K")]}},
        "Assets": {"units": {"CAD": [{"end": "2025-12-31", "val": 500.0, "form": "40-F",
                                      "filed": "2026-02-20", "accn": "0001-26-000001"}]}}}}}
    assert reporting_currency(raw) == "CAD"
    data = trends.build(as_us_gaap(raw, "CAD"), with_ttm=False)
    assert data["fiscal_year_ends"] == ["2025-12-31"], data["fiscal_year_ends"]
    assert data["series"]["revenue"]["values"]["2025-12-31"] == 100.0
    assert data["series"]["total_assets"]["values"]["2025-12-31"] == 500.0

    assert _name_key("The Toronto-Dominion Bank") == _name_key("TORONTO DOMINION BANK")
    assert _name_key("Canadian Pacific Kansas City Limited") == _name_key("CANADIAN PACIFIC KANSAS CITY LTD/CN")
    assert _name_key("CGI Inc. Class A Subordinate Voting Shares") == _name_key("CGI INC")
    assert _name_key("Royal Bank of Canada") != _name_key("Bank of Montreal /CAN/")
    print("ok: extract_tsx — tickers, year end, document sets, AIF split, IFRS adapter")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        demo()
    else:
        main()
