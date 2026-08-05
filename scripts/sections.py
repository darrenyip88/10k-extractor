#!/usr/bin/env python3
"""Turn a 10-K's HTML into plain text and split it into Items.

The one real trap: every "Item 1A. Risk Factors" string appears at least twice
in a 10-K — once in the table of contents, once as the actual section heading.
Naive regex extraction hands you the 24-character TOC entry instead of the
68,000-character section. Fix is in pick_sections(): keep the longest span.
"""

import difflib
import re
import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# Inline-XBRL 10-Ks open with an XML prolog, so bs4 warns that an HTML parser is
# being pointed at XML. They are XHTML documents and lxml handles them correctly.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

ITEM_RE = re.compile(r"^\s*Item\s+(\d{1,2}[A-C]?)\s*[.:\-–—]?\s*(.{0,70})", re.I | re.M)

# Items worth writing to their own file, with the filename to use. Anything
# found but not listed here (Part III items, exhibits) stays in full_text.txt.
ITEM_FILES = {
    "1": "item1_business",
    "1A": "item1a_risk_factors",
    "1B": "item1b_unresolved_staff_comments",
    "1C": "item1c_cybersecurity",
    "2": "item2_properties",
    "3": "item3_legal_proceedings",
    "4": "item4_mine_safety",
    "5": "item5_market_for_stock",
    "7": "item7_mdna",
    "7A": "item7a_market_risk",
    "8": "item8_financial_statements",
    "9A": "item9a_controls",
}

MIN_SECTION_CHARS = 200  # below this it's a cross-reference or a stub, not a section


def _flat(s):
    """Collapse every run of whitespace to one space, for comparing strings
    that were extracted with different line-break handling."""
    return re.sub(r"\s+", " ", s).strip()


def parse(html):
    """HTML -> (soup, plain text). Drops script/style and inline-XBRL hidden
    blocks, which otherwise dump a few thousand lines of tagging metadata into
    the text."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for hidden in soup.find_all(style=re.compile(r"display:\s*none", re.I)):
        hidden.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return soup, text.strip()


def pick_sections(text):
    """Split text into {item_key: {title, start, end, text}}.

    For each item, of all the places it appears, keep the one with the most
    text after it before the next item heading. TOC entries sit adjacent to
    each other so their spans are a few dozen characters; the real section
    runs for tens of thousands. Verified on AAPL, BRK and TSLA filings.
    """
    matches = [(m.group(1).upper(), m.start(), _flat(m.group(2))) for m in ITEM_RE.finditer(text)]
    best = {}
    for i, (key, start, title) in enumerate(matches):
        end = matches[i + 1][1] if i + 1 < len(matches) else len(text)
        if end - start > best.get(key, {}).get("length", 0):
            best[key] = {
                "title": title,
                "start": start,
                "end": end,
                "length": end - start,
                "text": text[start:end].strip(),
            }
    return {k: v for k, v in best.items() if v["length"] >= MIN_SECTION_CHARS}


def risk_headings(soup, risk_text):
    """The bolded headlines inside Item 1A — a ~60-line index of what the
    company is actually worried about, instead of 70k characters of prose.

    Every risk factor gets a bold one-sentence headline; body prose does not.
    So: collect bold runs document-wide, keep the ones that also appear inside
    the Item 1A text.
    """
    if not risk_text:
        return []
    haystack = _flat(risk_text)
    candidates = []
    for el in soup.find_all(["b", "strong"]):
        candidates.append(el.get_text(" ", strip=True))
    for el in soup.find_all(style=re.compile(r"font-weight:\s*(bold|[6-9]00)", re.I)):
        candidates.append(el.get_text(" ", strip=True))

    out, seen = [], set()
    for raw in candidates:
        head = _flat(raw)
        if not (30 <= len(head) <= 400) or head in seen:
            continue
        if head not in haystack:
            continue
        seen.add(head)
        out.append(head)
    return out


def _similarity(a, b):
    """How alike two risk headlines are, 0-1.

    Compares word lists, not characters, for two reasons. Words are the right
    unit for "is this the same risk reworded". And difflib's autojunk heuristic
    silently ignores any character appearing in over 1% of a sequence longer
    than 200 elements — risk headlines run 250+ characters, so character
    comparison scored two near-identical Apple risk factors at 0.41 instead of
    0.95 and reported them as one added plus one removed. Word lists are ~35
    elements, so autojunk never fires.
    """
    return difflib.SequenceMatcher(None, a.lower().split(), b.lower().split()).ratio()


def diff_risks(current, prior, threshold=0.6):
    """Which risks were added or dropped versus last year's 10-K.

    Exact string matching flags every reworded heading as both added and
    removed, so near-matches count as the same risk, reworded.

    The threshold is deliberately loose. JPMorgan reworded essentially every
    risk headline in its 2025 10-K ("can be negatively affected" -> "could be
    negatively affected") without changing a single risk; at 0.85 that showed
    up as 24 brand-new risks, which is flatly wrong. At 0.6 they pair up. Each
    pair carries its similarity score and both strings, so a loose match is
    visible rather than hidden — judge the borderline ones by eye.
    """
    prior_left = list(prior)
    cur_left, unchanged = [], []
    for head in current:
        if head in prior_left:
            prior_left.remove(head)
            unchanged.append(head)
        else:
            cur_left.append(head)

    # Score every remaining pair and assign the best ones first. Taking the best
    # match for each heading in document order instead lets an early heading
    # claim a prior risk that a later heading matches far better — which is how
    # Apple's "Investment in new business strategies and acquisitions" risk got
    # reported as both added and removed in the same year.
    pairs = []
    for head in cur_left:
        for old in prior_left:
            score = _similarity(head, old)
            if score >= threshold:
                pairs.append((score, head, old))
    pairs.sort(key=lambda p: -p[0])

    reworded, taken_cur, taken_old = [], set(), set()
    for score, head, old in pairs:
        if head in taken_cur or old in taken_old:
            continue
        taken_cur.add(head)
        taken_old.add(old)
        reworded.append({"now": head, "was": old, "similarity": round(score, 2)})

    reworded.sort(key=lambda r: r["similarity"])
    added = [h for h in cur_left if h not in taken_cur]
    removed = [h for h in prior_left if h not in taken_old]

    # For each supposedly-new risk, name its nearest dropped one even though it
    # fell below the threshold. No threshold cleanly separates "new risk" from
    # "same risk, rewritten from scratch" — JPMorgan rewrote every headline in
    # its 2025 10-K — so show the evidence instead of pretending to decide.
    for i, head in enumerate(added):
        best = max(removed, key=lambda old: _similarity(head, old), default=None)
        added[i] = {
            "risk": head,
            "closest_dropped": best,
            "similarity": round(_similarity(head, best), 2) if best else None,
        }

    return {
        "added": added,
        "removed": removed,
        "reworded": reworded,
        "unchanged": unchanged,
        # When a filer rewrites its whole risk section, added/removed stops
        # meaning "new risk" and starts meaning "rewritten". Flag it.
        "wholesale_rewrite": len(added) >= 5 and len(added) >= 0.25 * max(len(current), 1),
    }


def demo():
    """python3 sections.py — offline checks on a synthetic filing."""
    html = """<html><body>
      <p>Table of Contents</p>
      <p>Item 1. Business</p><p>Item 1A. Risk Factors</p><p>Item 7. MD&amp;A</p>
      <div style="display:none">ix hidden tagging junk that must not appear</div>
      <p>Item 1. Business</p><p>{}</p>
      <p>Item 1A. Risk Factors</p>
      <p><b>Our supply chain depends on a small number of vendors.</b></p>
      <p>{}</p>
      <p>Item 7. MD&amp;A</p><p>{}</p>
    </body></html>""".format("body of business " * 40, "risk prose " * 60, "mdna prose " * 40)

    soup, text = parse(html)
    assert "hidden tagging junk" not in text, "display:none block leaked into text"

    secs = pick_sections(text)
    assert set(secs) == {"1", "1A", "7"}, sorted(secs)
    # The TOC copies must lose to the body copies.
    assert secs["1A"]["length"] > 500, secs["1A"]["length"]
    assert "risk prose" in secs["1A"]["text"]
    assert "body of business" in secs["1"]["text"]

    heads = risk_headings(soup, secs["1A"]["text"])
    assert heads == ["Our supply chain depends on a small number of vendors."], heads

    d = diff_risks(
        ["Risk A about supply chains", "Brand new risk this year"],
        ["Risk A about supply chain", "An old risk we dropped"],
    )
    assert [a["risk"] for a in d["added"]] == ["Brand new risk this year"], d["added"]
    assert d["added"][0]["closest_dropped"] == "An old risk we dropped"
    assert d["removed"] == ["An old risk we dropped"], d["removed"]
    assert len(d["reworded"]) == 1, d["reworded"]
    assert d["wholesale_rewrite"] is False

    # Best-match-first assignment: the weak pair comes first in document order
    # but must not steal the prior heading that the strong pair needs.
    d = diff_risks(
        [
            "Investment in new business strategies, commercial relationships and acquisitions",
            "Investment in new business strategies and acquisitions could disrupt operations",
        ],
        ["Investment in new business strategies and acquisitions could disrupt operations"],
    )
    assert d["unchanged"] == [
        "Investment in new business strategies and acquisitions could disrupt operations"
    ], d["unchanged"]
    assert [a["risk"] for a in d["added"]] == [
        "Investment in new business strategies, commercial relationships and acquisitions"
    ], d["added"]
    assert d["removed"] == [], d["removed"]

    # Long headlines: difflib's autojunk would score this pair ~0.41 on
    # characters. On words it is the same risk with two clauses added.
    long_now = (
        "Investment in new business strategies, commercial relationships and acquisitions could "
        "disrupt the Company ongoing business, present risks not originally contemplated, and "
        "materially adversely affect the Company business, reputation, results of operations."
    )
    long_was = long_now.replace(", commercial relationships and acquisitions", " and acquisitions")
    assert _similarity(long_now, long_was) > 0.85, _similarity(long_now, long_was)
    d = diff_risks([long_now], [long_was])
    assert d["added"] == [] and d["removed"] == [], d
    assert len(d["reworded"]) == 1, d["reworded"]
    print("ok: sections — TOC/body split, hidden-block removal, risk headings, diff")


if __name__ == "__main__":
    demo()
