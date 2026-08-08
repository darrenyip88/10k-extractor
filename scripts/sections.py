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


def bold_runs(soup):
    """Every bolded run of text in the filing, flattened.

    Both the risk-factor index and the condenser need this: a 10-K marks its
    headings and its risk headlines with weight, and nothing else in the
    document body is bold.
    """
    out = []
    for el in soup.find_all(["b", "strong"]):
        out.append(el.get_text(" ", strip=True))
    for el in soup.find_all(style=re.compile(r"font-weight:\s*(bold|[6-9]00)", re.I)):
        out.append(el.get_text(" ", strip=True))
    return [_flat(t) for t in out if _flat(t)]


def risk_headings(soup, risk_text, bold=None):
    """The bolded headlines inside Item 1A — a ~60-line index of what the
    company is actually worried about, instead of 70k characters of prose.

    Every risk factor gets a bold one-sentence headline; body prose does not.
    So: collect bold runs document-wide, keep the ones that also appear inside
    the Item 1A text. `bold` lets a caller that already ran `bold_runs(soup)`
    (the condenser needs it too) pass the result instead of walking the same
    tree twice.
    """
    if not risk_text:
        return []
    haystack = _flat(risk_text)
    out, seen = [], set()
    for head in bold if bold is not None else bold_runs(soup):
        if not (30 <= len(head) <= 400) or head in seen:
            continue
        if head not in haystack:
            continue
        seen.add(head)
        out.append(head)
    return out


# --- condensing ----------------------------------------------------------
#
# A 10-K section is unreadable as a wall of text: Item 1A runs 70k characters
# and most of it is the same hedge restated. Condensing here rather than in the
# browser means the CLI, the skill and the page all read the same summary, and
# it stays a pure text transform — no model, no key, no cost, same as the rest
# of the tool.
#
# The rule is extractive on purpose: every sentence in the output is a sentence
# the company actually wrote. Nothing is paraphrased, so nothing can be
# invented. What gets dropped is the second, third and fourth restatement of a
# point already made, and the cross-references ("see Note 7").

# A short line with no terminal punctuation is a heading. Kept tight on
# purpose: at 160 a sentence broken mid-clause by the extractor ("...operating
# segments (see") passes the test and gets set as a heading.
HEADING_CHARS = 90

# A line that continues the one above rather than starting something new.
# `parse()` uses get_text("\n"), which puts every inline element on its own
# line, so Apple's "iPhone" / "®" / "is the Company's line of smartphones"
# arrives as three lines and every fragment would otherwise be promoted to a
# heading. Anything opening lowercase or with punctuation is a continuation.
CONTINUATION_RE = re.compile(r"^[a-z(\[\"'’“”®™°,;:.\)\-–—%$&/]")
# Page furniture that survives text extraction: running heads and page numbers.
FURNITURE_RE = re.compile(
    r"^(table of contents|page \d+|\d{1,4}|[-–—•*]+)$|form 10-k\s*\|\s*\d+$", re.I)

# Sentences that carry no information about the business: pointers to other
# parts of the filing, and the standard disclaimers.
BOILERPLATE_RE = re.compile(
    r"(in conjunction with|refer to (part|note|item)|"
    r"see (note|item|part) \d|incorporated (herein )?by reference|"
    r"included (elsewhere )?in this (report|annual report|form 10-k)|"
    r"is not a substitute for|in accordance with (u\.s\. )?generally accepted|"
    r"forward.looking statements|we (can give )?no assurance|cannot assure|"
    r"do not undertake (any )?obligation|are discussed (below|above)|"
    r"for (further|additional|more) (discussion|information|detail))",
    re.I,
)
# What makes a sentence worth keeping past the first one: a figure, a share of
# something, a date, a count. The user's instruction is the design here — keep
# the metrics.
FIGURE_RE = re.compile(r"(\$\s?[\d,.]|\d+(\.\d+)?\s?%|\b\d[\d,.]{2,}|\b\d+(\.\d+)?\s?(billion|million|thousand|bps|basis points))")
# Abbreviations that end in a period without ending a sentence. "U.S." is the
# one that matters — it appears in nearly every 10-K paragraph.
ABBREV_RE = re.compile(
    r"(\b[A-Z]|\bNo|\bInc|\bCorp|\bCo|\bLtd|\bLLC|\bMr|\bMs|\bDr|\bSt|\bapprox|"
    r"\bJan|\bFeb|\bMar|\bApr|\bJun|\bJul|\bAug|\bSept?|\bOct|\bNov|\bDec|"
    r"\be\.g|\bi\.e|\bvs|\betc|\bFig)\.$"
)


def sentences(block):
    """Split a paragraph into sentences without breaking on "U.S." or "Inc.".

    A naive split on `(?<=[.!?])\\s+` cuts "our U.S. and Canadian operations"
    in half, and 10-K prose is full of it.

    Deliberately no rule for a trailing number: an earlier version also merged
    across "1." for numbered lists, which swallowed the following sentence on
    every paragraph ending "...in 2025." — and the swallowed sentence was often
    the boilerplate the condenser exists to drop.
    """
    out = []
    for piece in re.split(r"(?<=[.!?])\s+", block):
        if out and ABBREV_RE.search(out[-1]):
            out[-1] += " " + piece
        else:
            out.append(piece)
    return [s.strip() for s in out if s.strip()]


def reflow(text):
    """Put the paragraphs back together before condensing them.

    Section text arrives one line per inline element, so a single sentence can
    be spread over six lines around a superscript ®. Sentence-level condensing
    on that produces confetti. A line that starts lowercase or with punctuation
    belongs to the line above; anything else starts a new block.
    """
    blocks = []
    for raw in text.splitlines():
        line = _flat(raw)
        if not line or line.startswith(("#", "_Source:")) or FURNITURE_RE.match(line):
            continue
        if blocks and CONTINUATION_RE.match(line):
            blocks[-1] += " " + line
        else:
            blocks.append(line)
    return blocks


def _is_heading(block, bold):
    """Bold in the filing, or short with nothing terminal at the end.

    The bold set is what makes risk factor headlines work. Those are full
    sentences ending in a period — indistinguishable from prose by length or
    punctuation — but the filer sets every one of them in bold, so the document
    answers the question itself. Guessing instead ("a lone sentence under 200
    characters is a headline") promoted half of Apple's product paragraphs.
    """
    if block in bold:
        return True
    return len(block) <= HEADING_CHARS and not block.endswith((".", "!", "?", ",", ";"))


def condense_block(block, max_sentences=3):
    """Keep the opening sentence and whatever carries a number. Drop the rest."""
    kept = []
    for i, sentence in enumerate(sentences(block)):
        if BOILERPLATE_RE.search(sentence):
            continue
        if i == 0 or FIGURE_RE.search(sentence):
            kept.append(sentence)
        if len(kept) >= max_sentences:
            break
    return " ".join(kept)


def condense(text, bold=(), max_sentences=3):
    """Section text -> [{'kind': 'heading'|'text', 'text': ...}].

    Headings survive whole; paragraphs get condensed; paragraphs that condense
    to nothing (pure cross-reference) disappear, and a heading left with
    nothing after it at the end goes with them.
    """
    bold = set(bold)
    out = []
    for block in reflow(text):
        # Checked before the heading test: "Refer to Note 7." is one short
        # sentence on its own line and would otherwise be promoted to a heading.
        if BOILERPLATE_RE.search(block) and len(sentences(block)) == 1:
            continue
        if _is_heading(block, bold):
            out.append({"kind": "heading", "text": block})
            continue
        short = condense_block(block, max_sentences)
        if short:
            out.append({"kind": "text", "text": short})
    while out and out[-1]["kind"] == "heading":
        out.pop()
    return out


def condensed_markdown(blocks, title, source_file, original_chars):
    kept = sum(len(b["text"]) for b in blocks)
    lines = [
        "# {} — condensed".format(title),
        "",
        "_{:,} characters down to {:,} ({:.0f}% of the section). Every sentence below is the "
        "company's own — the opening sentence of each paragraph plus every sentence carrying a "
        "figure, with cross-references and boilerplate dropped. Nothing is paraphrased. Full "
        "text: `{}`._".format(
            original_chars, kept, 100.0 * kept / max(original_chars, 1), source_file
        ),
        "",
    ]
    for block in blocks:
        lines += (["## " + block["text"], ""] if block["kind"] == "heading"
                  else [block["text"], ""])
    return "\n".join(lines)


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

    # Without the bold set that sentence is prose; with it, it is a headline.
    assert condense("\n".join(["A one-line risk that reads like prose.", "Prose that follows it. " * 8]))[0][
        "kind"] == "text"
    assert condense("\n".join(["A one-line risk that reads like prose.", "Prose that follows it. " * 8]),
                    bold=["A one-line risk that reads like prose."])[0]["kind"] == "heading"

    # Inline fragments rejoin: one sentence split around a superscript.
    assert reflow("iPhone\n®\nis the Company’s line of smartphones.\nProducts") == [
        "iPhone ® is the Company’s line of smartphones.", "Products"
    ], reflow("iPhone\n®\nis the Company’s line of smartphones.\nProducts")

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

    # --- condensing -------------------------------------------------------
    # "U.S." must not end a sentence, or every 10-K paragraph splits wrong.
    assert sentences("We rely on our U.S. and Canadian operations. They are large.") == [
        "We rely on our U.S. and Canadian operations.",
        "They are large.",
    ], sentences("We rely on our U.S. and Canadian operations. They are large.")

    section = "\n".join([
        "# Item 1A. Risk Factors",
        "_Source: https://example.com/x.htm_",
        "Business and Operating Risks",
        "We are highly dependent on the financial performance of our U.S. operations.",
        "Our performance depends on those operations, which comprised 86% of net sales in 2025. "
        "This should be read in conjunction with Item 7 of this Report. "
        "We could be affected by many other things. "
        "California alone was 26% of U.S. net sales.",
        "Refer to Note 7 for further discussion.",
        "Legal Proceedings",
    ])
    headline = "We are highly dependent on the financial performance of our U.S. operations."
    blocks = condense(section, bold=[headline])
    assert blocks[0] == {"kind": "heading", "text": "Business and Operating Risks"}, blocks[0]
    assert blocks[1] == {"kind": "heading", "text": headline}, blocks[1]
    body = blocks[2]["text"]
    assert "86% of net sales" in body and "26% of U.S. net sales" in body, body
    assert "in conjunction with" not in body, "boilerplate survived"
    assert "many other things" not in body, "a figureless middle sentence survived"
    # A paragraph that is nothing but a cross-reference disappears, and the
    # trailing heading with nothing under it goes with it.
    assert len(blocks) == 3, blocks
    md = condensed_markdown(blocks, "Item 1A. Risk Factors", "sections/x.md", len(section))
    assert md.startswith("# Item 1A. Risk Factors — condensed")
    assert "## Business and Operating Risks" in md
    print("ok: sections — TOC/body split, hidden-block removal, risk headings, diff, condensing")


if __name__ == "__main__":
    demo()
