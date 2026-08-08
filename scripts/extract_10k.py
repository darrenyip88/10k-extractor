#!/usr/bin/env python3
"""Pull a company's 10-K and everything useful inside it.

    ./run_10k.sh AAPL

Writes filings/<TICKER>/<fiscal-year-end>/ containing the raw filing, the plain
text, each Item as its own file plus a condensed version of it, the as-filed
financial statements, a ten-year financial history, the risk-factor index, and
a diff of this year's risks against last year's. Then read SUMMARY.md.

Everything comes from SEC EDGAR, and every figure comes out of a 10-K —
including the share price, which the cover page implies rather than a quote
feed supplying. No API key, no quota, no cost, no live market data.

One exception, and it is additive: the 10-K is audited but up to a year stale,
so the tagged numbers from every 10-Q filed since are used to roll the year
forward to a trailing-twelve-month column that sits *beside* the annual one.
Nothing 10-K-only is displaced — the risk factors, the share-comp footnote and
the audited statements are what a 10-Q hasn't got. `--no-ttm` skips it.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import sections
import statements
import trends
import valuation
from sec_client import SECClient, SECError

EDGAR_FILING_INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "filings"

# Subscription revenue reported as its own line on the income statement — the
# membership warehouses (COST, BJ) and the club retailers. Dimensioned in XBRL,
# so it never reaches companyfacts; read off the rendered statement instead.
MEMBERSHIP_RE = re.compile(r"membership\s+(fee|due|income)", re.I)

# Below this a section is a stub or a cross-reference, not something to condense.
CONDENSE_MIN_CHARS = 3000


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def section_files(out_dir, secs, source_url, bold):
    """One markdown file per Item we care about, plus a condensed twin.

    Returns ({key: relative path}, {key: relative condensed path}).
    """
    written, condensed = {}, {}
    for key, filename in sections.ITEM_FILES.items():
        sec = secs.get(key)
        if not sec:
            continue
        title = "Item {}. {}".format(key, sec["title"])
        body = "# {}\n\n_Source: {}_\n\n{}\n".format(title, source_url, sec["text"])
        write(out_dir / "sections" / (filename + ".md"), body)
        written[key] = "sections/{}.md".format(filename)

        blocks = sections.condense(sec["text"], bold)
        # Nothing gets a summary unless it earns one. A short section has
        # nothing to condense — JPMorgan answers Item 7 with a page reference,
        # in 587 characters — and the summary's own header note would make the
        # "condensed" file the longer of the two.
        kept = sum(len(b["text"]) for b in blocks)
        if blocks and len(sec["text"]) >= CONDENSE_MIN_CHARS and kept < 0.9 * len(sec["text"]):
            write(
                out_dir / "summaries" / (filename + ".md"),
                sections.condensed_markdown(blocks, title, written[key], len(sec["text"])),
            )
            condensed[key] = "summaries/{}.md".format(filename)
    return written, condensed


def risk_diff_markdown(diff, current_year, prior_year):
    lines = [
        "# Risk factor changes: {} vs {}".format(current_year, prior_year),
        "",
        "Risks the company **added** this year are the highest-signal part of a 10-K — "
        "they are what management started worrying about since last year.",
        "",
        "Matching is on the bolded risk headlines. Similar headlines are treated as the same risk "
        "reworded, not as one added plus one removed — companies rewrite these constantly without "
        "changing what they mean. Reworded pairs are listed below with a similarity score, least "
        "similar first; the low-scoring ones are worth reading, since that is where a rewrite can "
        "hide a real change in substance.",
        "",
        "**{} added · {} removed · {} reworded · {} unchanged**".format(
            len(diff["added"]), len(diff["removed"]), len(diff["reworded"]), len(diff["unchanged"])
        ),
        "",
    ]
    if diff["wholesale_rewrite"]:
        lines += [
            "> **This filer rewrote its risk section wholesale this year.** With most headlines "
            "rewritten from scratch, the added/removed split below is not reliable evidence that "
            "a risk is genuinely new — check each one against its closest dropped counterpart.",
            "",
        ]
    lines += ["## Added", ""]
    for item in diff["added"]:
        lines.append("- {}".format(item["risk"]))
        if item["closest_dropped"]:
            lines.append(
                "  - closest dropped risk ({:.0%} similar): {}".format(
                    item["similarity"], item["closest_dropped"]
                )
            )
    if not diff["added"]:
        lines.append("_none_")
    lines += ["", "## Removed", ""]
    lines += ["- {}".format(h) for h in diff["removed"]] or ["_none_"]
    if diff["reworded"]:
        lines += ["", "## Reworded (least similar first)", ""]
        for r in diff["reworded"]:
            lines += [
                "- **{:.0%} similar**".format(r["similarity"]),
                "  - now: {}".format(r["now"]),
                "  - was: {}".format(r["was"]),
            ]
    return "\n".join(lines) + "\n"


def ttm_section(t, val):
    """The trailing-twelve-month block for SUMMARY.md.

    Additive by design: nothing above it changes, and every 10-K-only figure —
    risk factors, the share-comp footnote, the audited statements — stays put.
    A 10-Q restates the numbers, not the document.
    """
    if not t:
        return [
            "## Trailing twelve months",
            "",
            "Nothing to roll forward: no 10-Q post-dates this 10-K with a comparable "
            "prior-year period. Every figure above is the fiscal year as filed.",
            "",
        ]

    tv = (val or {}).get("ttm") or {}
    fy, ttm = t["fiscal_year_end"], t["values"]
    g, annual = t["ytd_growth_pct"], t["fiscal_year"]

    def fmt(v, money=True):
        if v is None:
            return "—"
        return "${:,.0f}M".format(v / 1e6) if money else "${:,.2f}".format(v)

    lines = [
        "## Trailing twelve months",
        "",
        "The year above is audited and up to a year old. This is the same year rolled forward "
        "with every 10-Q filed since — **{}** — using".format(t["basis"]),
        "",
        "> TTM = fiscal year + year-to-date this year − year-to-date a year ago",
        "",
        "Year-to-date, not four discrete quarters: a 10-Q's cash flow statement is always "
        "cumulative from the year start, and the fourth quarter never appears in a 10-Q at all. "
        "Source: {} ended {}, filed {}. Costs no extra SEC requests — it comes out of the same "
        "companyfacts call as the ten-year history.".format(
            t["fiscal_period"] or "latest 10-Q", t["quarter_end"], t["quarter_filed"]),
        "",
        "| Metric | FY {} | TTM to {} | YTD vs prior-year YTD |".format(fy, t["quarter_end"]),
        "|---|---|---|---|",
    ]
    for label, key, money in [
        ("Revenue", "revenue", True),
        ("Operating income", "operating_income", True),
        ("Net income", "net_income", True),
        ("EPS (diluted)", "eps_diluted", False),
        ("Cash from ops", "operating_cash_flow", True),
        ("Capex", "capex", True),
        ("D&A", "d_and_a", True),
    ]:
        lines.append("| {} | {} | {} | {} |".format(
            label,
            fmt(annual.get(key), money),
            fmt(ttm.get(key), money),
            "—" if g.get(key) is None else "{:+.1f}%".format(g[key]),
        ))
    lines += [
        "",
        "The last column compares the two year-to-date periods directly — it is the cleanest "
        "growth read in here, because both sides are the same length and neither depends on "
        "the annual figure.",
        "",
    ]
    if tv.get("multiples"):
        fm, tm = val["multiples"], tv["multiples"]
        lines += [
            "Multiples restated on TTM, with the balance sheet moved to the quarter end so a "
            "fresh numerator is not sitting over a stale bridge:",
            "",
            "| | FY {} | TTM |".format(fy),
            "|---|---|---|",
            "| Enterprise value | {} | {} |".format(
                valuation._m(val["bridge"]["enterprise_value"]),
                valuation._m(tv["bridge"]["enterprise_value"])),
            "| EV / Sales | {} | {} |".format(
                valuation._x(fm["ev_sales"]), valuation._x(tm["ev_sales"])),
            "| EV / EBITDA | {} | {} |".format(
                valuation._x(fm["ev_ebitda"]), valuation._x(tm["ev_ebitda"])),
            "| EV / EBIT | {} | {} |".format(
                valuation._x(fm["ev_ebit"]), valuation._x(tm["ev_ebit"])),
            "| P/E | {} | {} |".format(valuation._x(fm["pe"]), valuation._x(tm["pe"])),
            "",
            "Share count is the 10-Q's own cover page where it has one; the price is unchanged, "
            "since a 10-Q states one no more than a 10-K does. Full workings: `valuation.md`.",
            "",
        ]
    if t["not_tagged"]:
        lines += [
            "Not tagged in this filer's 10-Qs, so absent from the TTM column: {}. The annual "
            "figures are unaffected.".format(", ".join(t["not_tagged"])),
            "",
        ]
    return lines


def summary_markdown(meta, data, secs, files, summaries, risk_heads, diff, stmt_names, val):
    years = data["fiscal_year_ends"]
    latest = years[-1] if years else None
    d = data["derived"].get(latest, {}) if latest else {}
    s = data["series"]
    lines_from_statement = data.get("statement_lines", {})

    def money(key):
        v = s.get(key, {}).get("values", {}).get(latest)
        return "—" if v is None else "${:,.0f}M".format(v / 1e6)

    def dmoney(key):
        v = d.get(key)
        return "—" if v is None else "${:,.0f}M".format(v / 1e6)

    def count(key):
        v = s.get(key, {}).get("values", {}).get(latest)
        return "—" if v is None else "{:,.0f}".format(v)

    def pct(key):
        v = d.get(key)
        return "—" if v is None else "{}%".format(v)

    lines = [
        "# {} ({}) — 10-K for fiscal year ended {}".format(
            meta["company"], meta["ticker"], meta["period"]
        ),
        "",
        "| | |",
        "|---|---|",
        "| Filed | {} |".format(meta["filed"]),
        "| CIK | {} |".format(meta["cik"]),
        "| Industry (SIC) | {} — {} |".format(meta["sic"], meta["sic_description"]),
        "| Incorporated | {} |".format(meta["state_of_incorporation"]),
        "| Exchanges | {} |".format(", ".join(meta["exchanges"]) or "—"),
        "| Auditor | {} |".format(meta.get("auditor_name") or "—"),
        "| Source | {} |".format(meta["source_url"]),
        "",
        "## Latest year at a glance",
        "",
        "| Metric | Value |",
        "|---|---|",
        "| Revenue | {} ({} YoY) |".format(money("revenue"), pct("revenue_growth_pct")),
        "| MRQ revenue (Q4) | {} |".format(dmoney("mrq_revenue")),
        "| MRQ revenue (prior year Q4) | {} |".format(dmoney("mrq_revenue_prior")),
        "| Gross margin | {} |".format(pct("gross_margin_pct")),
        "| Operating income | {} ({} margin) |".format(
            money("operating_income"), pct("operating_margin_pct")),
        "| D&A | {} |".format(money("d_and_a")),
        "| Net income | {} ({} YoY) |".format(money("net_income"), pct("net_income_growth_pct")),
        "| Net margin | {} |".format(pct("net_margin_pct")),
        "| EPS (diluted) | {} ({} YoY) |".format(
            "—"
            if s["eps_diluted"]["values"].get(latest) is None
            else "${:,.2f}".format(s["eps_diluted"]["values"][latest]),
            pct("eps_growth_pct"),
        ),
        "| Diluted shares (weighted average) | {} |".format(count("shares_diluted")),
        "| Cash from ops | {} |".format(money("operating_cash_flow")),
        "| Capex | {} |".format(money("capex")),
        "| Free cash flow | {} ({} margin) |".format(
            "—" if d.get("free_cash_flow") is None else "${:,.0f}M".format(d["free_cash_flow"] / 1e6),
            pct("fcf_margin_pct"),
        ),
        "| ROE | {} |".format(pct("roe_pct")),
        "| Total assets | {} |".format(money("total_assets")),
        "| Inventory | {} |".format(money("inventory")),
        "| Cash + ST investments | {} |".format(dmoney("cash_and_st_investments")),
        "| Total debt | {} |".format(
            "—" if d.get("total_debt") is None else "${:,.0f}M".format(d["total_debt"] / 1e6)
        ),
        # Components spelled out because companyfacts sometimes has no row for a
        # concept in the newest filing (NVDA's FY2026 marketable securities),
        # which would otherwise show a confidently wrong net cash figure.
        "| Net cash / (net debt) | {} |".format(
            "—"
            if d.get("net_cash") is None
            else "{} = cash {} + ST investments {} − debt {}".format(
                "${:,.0f}M".format(d["net_cash"] / 1e6)
                if d["net_cash"] >= 0
                else "(${:,.0f}M)".format(abs(d["net_cash"]) / 1e6),
                money("cash").replace("—", "n/a"),
                money("short_term_investments").replace("—", "n/a"),
                "${:,.0f}M".format(d["total_debt"] / 1e6) if d.get("total_debt") else "n/a",
            )
        ),
        "| Diluted share count | {} YoY |".format(pct("shares_change_pct")),
    ]
    if lines_from_statement.get("membership_fee_income") is not None:
        lines.append("| Membership fee income | ${:,.0f}M — {}, {} |".format(
            lines_from_statement["membership_fee_income"] / 1e6,
            lines_from_statement["source"],
            lines_from_statement.get("membership_fee_column") or "latest column"))
    lines += [
        "",
        "Ratios are computed from the company's tagged XBRL values, not reported by the company. "
        "Full series and the concepts used: `trends.md`.",
        "",
    ]
    if d.get("mrq_revenue") is None:
        lines += [
            "The most-recent-quarter rows are blank because this filer's 10-K tags no quarterly "
            "period. The SEC dropped the selected-quarterly-financial-data requirement in 2021, "
            "so most 10-Ks now report the year only. Nothing is reconstructed to fill that gap — "
            "but the quarters filed *since* the year end are read straight out of the 10-Qs, "
            "under **Trailing twelve months** below.",
            "",
        ]

    v, b, m = val, val["bridge"], val["multiples"]
    lines += [
        "## Valuation",
        "",
        "| | |",
        "|---|---|",
        "| Share price | {} |".format(
            "— (not derivable from this filing)" if not (v["quote"] or {}).get("price")
            else "${:,.2f} as of {} — {}".format(
                v["quote"]["price"], v["quote"].get("as_of") or "the cover date",
                v["quote"].get("source", ""))),
        "| Shares (cover page) | {} |".format(
            "—" if not v["shares"]["cover_page"]
            else "{:,.0f} as of {}".format(
                v["shares"]["cover_page"]["total"], v["shares"]["cover_page"]["as_of"])),
        "| Shares (fully diluted, + options and RSUs) | {} |".format(
            "—" if v["shares"]["fully_diluted"] is None
            else "{:,.0f}".format(v["shares"]["fully_diluted"])),
        "| Market cap | {} |".format(valuation._m(v["market_cap"])),
        "| Enterprise value | {} = mkt cap {} + debt {} + preferred {} + minority {} − cash {} |".format(
            valuation._m(b["enterprise_value"]), valuation._m(b["market_cap"]),
            valuation._m(b["total_debt"]), valuation._m(b["preferred"]),
            valuation._m(b["minority_interest"]), valuation._m(b["cash_and_equivalents"])),
        "| EV / Sales · EBIT · EBITDA | {} · {} · {} |".format(
            valuation._x(m["ev_sales"]), valuation._x(m["ev_ebit"]), valuation._x(m["ev_ebitda"])),
        "| P/E · FCF yield | {} · {} |".format(
            valuation._x(m["pe"]),
            "—" if m["fcf_yield_pct"] is None else "{}%".format(m["fcf_yield_pct"])),
        "",
    ]
    if (v["quote"] or {}).get("is_floor"):
        lines += [
            "The price is the filing's own and comes from the cover page: aggregate market value "
            "of common equity held by non-affiliates, divided by shares outstanding. It excludes "
            "affiliate-held shares, so it is a **floor** — a company whose insiders hold a real "
            "stake prices low by roughly that percentage. Market cap here is the public float "
            "restated. `valuation.md` shows the whole calculation; `--price` overrides it.",
            "",
        ]
    if v.get("market_cap_warning"):
        lines += ["> {}".format(v["market_cap_warning"]), ""]
    lines += ["Full bridge, the share-count sources, and the footnote rows: `valuation.md`.", ""]

    lines += ttm_section(data.get("ttm"), v)

    if len(years) > 1:
        lines += ["## Revenue and EPS history", "", "| Year end | Revenue ($M) | EPS |", "|---|---|---|"]
        for y in years:
            rev = s["revenue"]["values"].get(y)
            eps = s["eps_diluted"]["values"].get(y)
            lines.append(
                "| {} | {} | {} |".format(
                    y,
                    "—" if rev is None else "{:,.0f}".format(rev / 1e6),
                    "—" if eps is None else "{:,.2f}".format(eps),
                )
            )
        lines.append("")

    lines += ["## Risk factors", ""]
    if risk_heads:
        lines.append("{} risk headlines extracted — see `risk_headings.md`.".format(len(risk_heads)))
    else:
        lines.append("No bolded risk headlines found; read `sections/item1a_risk_factors.md` directly.")
    if diff:
        lines.append("")
        lines.append(
            "**Versus last year: {} added, {} removed, {} reworded** — see `risk_diff.md`.".format(
                len(diff["added"]), len(diff["removed"]), len(diff["reworded"])
            )
        )
        if diff["wholesale_rewrite"]:
            lines.append(
                "This filer rewrote its risk headlines wholesale, so treat the added/removed "
                "split as rewriting rather than new risks. `risk_diff.md` pairs each one with "
                "its closest dropped counterpart."
            )
        for item in diff["added"][:5]:
            lines.append("- NEW: {}".format(item["risk"]))
    lines.append("")

    lines += ["## Files", "", "| File | What's in it |", "|---|---|"]
    descriptions = {
        "1": "What the company does, segments, customers, employees",
        "1A": "Risk factors, in full",
        "1B": "Unresolved SEC staff comments",
        "1C": "Cybersecurity risk management and governance",
        "2": "Properties — facilities, plants, offices",
        "3": "Legal proceedings",
        "4": "Mine safety disclosures",
        "5": "Market for the stock, holders, buybacks, performance graph",
        "7": "MD&A — management's own explanation of the results",
        "7A": "Market risk — rates, FX, commodities",
        "8": "Financial statements and the notes",
        "9A": "Controls and procedures, internal control over financial reporting",
    }
    for key, rel in files.items():
        lines.append(
            "| `{}` | Item {}: {} ({:,} chars) |".format(
                rel, key, descriptions.get(key, secs[key]["title"]), len(secs[key]["text"])
            )
        )
    for key, rel in summaries.items():
        lines.append(
            "| `{}` | Item {} condensed — the company's own sentences, boilerplate dropped |".format(
                rel, key
            )
        )
    for name in stmt_names:
        lines.append("| `statements/{}.md` | As-filed statement (`.json` alongside) |".format(name))
    lines.append("| `trends.md` | {}-year financial history and ratios |".format(len(years)))
    lines.append("| `valuation.md` | Share count, market cap, EV bridge, multiples (`.json` alongside) |")
    lines.append("| `risk_headings.md` | Index of every risk factor headline |")
    if diff:
        lines.append("| `risk_diff.md` | Risks added and removed vs last year |")
    lines.append("| `full_text.txt` | Entire filing as plain text |")
    lines.append("| `10-K.htm` | Raw filing exactly as submitted |")
    lines.append("| `metadata.json` | Identifiers and source URLs |")
    lines.append("")
    return "\n".join(lines)


def run(args):
    client = SECClient(refresh=args.refresh)
    ticker = client.normalize_ticker(args.ticker)
    cik = args.cik or client.resolve_cik(ticker)

    subs = client.submissions(cik)
    # Default only needs this year's and last year's (for the risk diff); asking
    # for a specific year means digging further back through the archives.
    filings = client.list_10ks(cik, subs, need=20 if args.year else 2)
    if args.year:
        matches = [f for f in filings if f["report_date"][:4] == str(args.year)]
        if not matches:
            raise SECError(
                "no 10-K with fiscal year {}. Available: {}".format(
                    args.year, ", ".join(f["report_date"][:4] for f in filings)
                )
            )
        filing = matches[0]
    else:
        filing = filings[0]

    out_dir = Path(args.out or DEFAULT_OUT) / ticker / filing["report_date"]
    if (out_dir / "SUMMARY.md").exists() and not args.refresh:
        print("Already extracted: {}".format(out_dir))
        print("Pass --refresh to re-download.")
        return out_dir

    source_url = EDGAR_FILING_INDEX.format(
        cik=cik, acc=filing["accession_plain"], doc=filing["primary_doc"]
    )
    print("{} — 10-K for FY ended {} (filed {})".format(ticker, filing["report_date"], filing["filing_date"]))
    print("  fetching filing...")
    html = client.filing_file(cik, filing["accession_plain"], filing["primary_doc"])
    write(out_dir / "10-K.htm", html)

    soup, text = sections.parse(html)
    write(out_dir / "full_text.txt", text)
    secs = sections.pick_sections(text)
    # The filing's own bold runs tell the condenser which lines are headings,
    # and the risk index which lines are headlines — one whole-document walk,
    # not two.
    bold = sections.bold_runs(soup)
    files, summaries = section_files(out_dir, secs, source_url, bold)
    print("  sections: {} extracted ({:,} chars of text), {} condensed".format(
        len(files), len(text), len(summaries)))

    risk_text = secs.get("1A", {}).get("text", "")
    risk_heads = sections.risk_headings(soup, risk_text, bold)
    if risk_heads:
        write(
            out_dir / "risk_headings.md",
            "# Risk factors — {} headlines\n\n_Full text: sections/item1a_risk_factors.md_\n\n".format(
                len(risk_heads)
            )
            + "\n".join("- {}".format(h) for h in risk_heads)
            + "\n",
        )
    print("  risk headlines: {}".format(len(risk_heads)))

    diff = None
    if not args.no_diff and len(filings) > 1:
        prior = filings[filings.index(filing) + 1] if filings.index(filing) + 1 < len(filings) else None
        if prior:
            print("  fetching prior 10-K ({}) for the risk diff...".format(prior["report_date"]))
            try:
                prior_html = client.filing_file(cik, prior["accession_plain"], prior["primary_doc"])
                prior_soup, prior_text = sections.parse(prior_html)
                prior_secs = sections.pick_sections(prior_text)
                prior_heads = sections.risk_headings(
                    prior_soup, prior_secs.get("1A", {}).get("text", "")
                )
                if risk_heads and prior_heads:
                    diff = sections.diff_risks(risk_heads, prior_heads)
                    write(
                        out_dir / "risk_diff.md",
                        risk_diff_markdown(diff, filing["report_date"], prior["report_date"]),
                    )
                    print(
                        "  risk diff: {} added, {} removed, {} reworded".format(
                            len(diff["added"]), len(diff["removed"]), len(diff["reworded"])
                        )
                    )
                else:
                    print("  risk diff: skipped (no bold headlines in one of the filings)")
            except SECError as e:
                print("  risk diff: skipped ({})".format(e))

    print("  fetching as-filed statements...")
    stmts, auditor = statements.fetch_statements(
        client, cik, filing["accession_plain"], include_disclosures=args.all_tables
    )
    for st in stmts:
        write(out_dir / "statements" / (st["name"] + ".md"), statements.to_markdown(st["title"], st["table"]))
        write(
            out_dir / "statements" / (st["name"] + ".json"),
            json.dumps({"title": st["title"], **st["table"]}, indent=2),
        )
    print("  statements: {}".format(", ".join(s["name"] for s in stmts) or "none"))

    print("  fetching 10-year XBRL history...")
    facts = client.companyfacts(cik)
    data = trends.build(facts, max_years=args.years, as_of=filing["report_date"],
                        with_ttm=not args.no_ttm)
    # Revenue lines the filer tags along a dimension are dropped by the
    # companyfacts API entirely, so they come off the rendered statement.
    income = next((s["table"] for s in stmts if s["name"] == "income_statement"), None)
    fee, fee_col = statements.line_value(income, MEMBERSHIP_RE)
    data["statement_lines"] = {
        "membership_fee_income": fee,
        "membership_fee_column": fee_col,
        "source": "as-filed income statement" if fee is not None else None,
    }
    write(out_dir / "trends.json", json.dumps(data, indent=2))
    write(out_dir / "trends.md", trends.to_markdown(data, subs["name"]))
    print("  trends: {} fiscal years, {} metrics not tagged".format(
        len(data["fiscal_year_ends"]), len(data["missing"])))
    if data.get("ttm"):
        t = data["ttm"]
        print("  TTM: rolled forward to {} ({}, filed {}) — {} days past the year end".format(
            t["quarter_end"], t["fiscal_period"], t["quarter_filed"], t["days_stale_at_fy_end"]))
    elif not args.no_ttm:
        print("  TTM: none — no 10-Q post-dates this 10-K with a comparable prior period")

    print("  share count and valuation...")
    cover, awards = valuation.gather(client, cik, filing, facts)
    if args.price:
        quote = {"price": args.price, "as_of": "supplied with --price", "source": "--price"}
    elif args.no_price:
        quote = {"price": None, "error": "skipped with --no-price"}
    else:
        quote = valuation.filing_price(facts, filing["accession"], cover)
    val = valuation.build(
        data["series"], data["derived"], filing["report_date"],
        cover, awards, quote, shares_override=args.shares,
        ttm=data.get("ttm"), companyfacts=facts,
    )
    write(out_dir / "valuation.json", json.dumps(val, indent=2))
    write(out_dir / "valuation.md", valuation.to_markdown(val, subs["name"], ticker))
    print("    shares: cover {}, fully diluted {}".format(
        "—" if not cover else "{:,.0f}".format(cover["total"]),
        "—" if not val["shares"]["fully_diluted"] else "{:,.0f}".format(val["shares"]["fully_diluted"])))
    if quote.get("error"):
        print("    price: unavailable ({}) — pass --price to supply one".format(quote["error"]))
    elif quote.get("price"):
        print("    price: ${:,.2f} — {}".format(quote["price"], quote["source"]))
    print("    market cap {} · EV {}".format(
        valuation._m(val["market_cap"]), valuation._m(val["bridge"]["enterprise_value"])))
    if val.get("ttm"):
        tv = val["ttm"]
        print("    TTM: sales {} · EV/EBITDA {} (FY {}) · P/E {} (FY {})".format(
            valuation._m(tv["operating"]["sales"]),
            valuation._x(tv["multiples"]["ev_ebitda"]), valuation._x(val["multiples"]["ev_ebitda"]),
            valuation._x(tv["multiples"]["pe"]), valuation._x(val["multiples"]["pe"])))

    meta = {
        "ticker": ticker,
        "company": subs["name"],
        "cik": cik,
        "sic": subs.get("sic", ""),
        "sic_description": subs.get("sicDescription", ""),
        "state_of_incorporation": subs.get("stateOfIncorporationDescription", "—"),
        "fiscal_year_end": subs.get("fiscalYearEnd", ""),
        "exchanges": subs.get("exchanges", []),
        "period": filing["report_date"],
        "filed": filing["filing_date"],
        "accession": filing["accession"],
        "source_url": source_url,
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
        "auditor_name": (auditor or {}).get("Auditor Name"),
        "auditor_location": (auditor or {}).get("Auditor Location"),
        "auditor_firm_id": (auditor or {}).get("Auditor Firm ID"),
    }
    write(out_dir / "metadata.json", json.dumps(meta, indent=2))
    write(
        out_dir / "SUMMARY.md",
        summary_markdown(meta, data, secs, files, summaries, risk_heads, diff,
                         [s["name"] for s in stmts], val),
    )

    print("\nDone: {}".format(out_dir))
    print("Start with SUMMARY.md. SEC requests: {requests_made}, cache hits: {cache_hits}".format(
        **client.stats()))
    return out_dir


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ticker", help="stock ticker, e.g. AAPL (BRK.B and BRK-B both work)")
    p.add_argument("--year", type=int, help="fiscal year to pull instead of the latest, e.g. 2023")
    p.add_argument("--cik", type=int, help="use this CIK directly (for tickers SEC's map doesn't list)")
    p.add_argument("--years", type=int, default=10, help="years of XBRL history (default 10)")
    p.add_argument("--all-tables", action="store_true",
                   help="also pull every note table (segments, geography, debt, leases) — ~70 more requests")
    p.add_argument("--price", type=float,
                   help="share price for the market cap, overriding the one implied by the "
                        "filing's cover page (public float / shares outstanding)")
    p.add_argument("--shares", type=float,
                   help="share count for the market cap, overriding the cover page "
                        "(needed for multi-class filers like BRK)")
    p.add_argument("--no-price", action="store_true",
                   help="skip the price — everything but market cap, EV and the multiples")
    p.add_argument("--no-diff", action="store_true", help="skip the prior-year risk factor diff")
    p.add_argument("--no-ttm", action="store_true",
                   help="skip the trailing-twelve-month roll-forward — the 10-K year only, "
                        "with nothing from the 10-Qs filed since")
    p.add_argument("--refresh", action="store_true", help="ignore the cache and re-download")
    p.add_argument("--out", help="output directory (default: ../filings)")
    args = p.parse_args()

    try:
        run(args)
    except SECError as e:
        print("ERROR: {}".format(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
