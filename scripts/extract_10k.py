#!/usr/bin/env python3
"""Pull a company's 10-K and everything useful inside it.

    ./run_10k.sh AAPL

Writes filings/<TICKER>/<fiscal-year-end>/ containing the raw filing, the plain
text, each Item as its own file, the as-filed financial statements, a ten-year
financial history, the risk-factor index, and a diff of this year's risks
against last year's. Then read SUMMARY.md.

Everything comes from SEC EDGAR. No API key, no quota, no cost.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import sections
import statements
import trends
from sec_client import SECClient, SECError

EDGAR_FILING_INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "filings"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def section_files(out_dir, secs, source_url):
    """One markdown file per Item we care about. Returns {key: relative path}."""
    written = {}
    for key, filename in sections.ITEM_FILES.items():
        sec = secs.get(key)
        if not sec:
            continue
        body = "# Item {}. {}\n\n_Source: {}_\n\n{}\n".format(
            key, sec["title"], source_url, sec["text"]
        )
        write(out_dir / "sections" / (filename + ".md"), body)
        written[key] = "sections/{}.md".format(filename)
    return written


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


def summary_markdown(meta, data, secs, files, risk_heads, diff, stmt_names):
    years = data["fiscal_year_ends"]
    latest = years[-1] if years else None
    d = data["derived"].get(latest, {}) if latest else {}
    s = data["series"]

    def money(key):
        v = s.get(key, {}).get("values", {}).get(latest)
        return "—" if v is None else "${:,.0f}M".format(v / 1e6)

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
        "| Gross margin | {} |".format(pct("gross_margin_pct")),
        "| Operating margin | {} |".format(pct("operating_margin_pct")),
        "| Net income | {} ({} YoY) |".format(money("net_income"), pct("net_income_growth_pct")),
        "| Net margin | {} |".format(pct("net_margin_pct")),
        "| EPS (diluted) | {} ({} YoY) |".format(
            "—"
            if s["eps_diluted"]["values"].get(latest) is None
            else "${:,.2f}".format(s["eps_diluted"]["values"][latest]),
            pct("eps_growth_pct"),
        ),
        "| Operating cash flow | {} |".format(money("operating_cash_flow")),
        "| Free cash flow | {} ({} margin) |".format(
            "—" if d.get("free_cash_flow") is None else "${:,.0f}M".format(d["free_cash_flow"] / 1e6),
            pct("fcf_margin_pct"),
        ),
        "| ROE | {} |".format(pct("roe_pct")),
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
        "",
        "Ratios are computed from the company's tagged XBRL values, not reported by the company. "
        "Full series and the concepts used: `trends.md`.",
        "",
    ]

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
    for name in stmt_names:
        lines.append("| `statements/{}.md` | As-filed statement (`.json` alongside) |".format(name))
    lines.append("| `trends.md` | {}-year financial history and ratios |".format(len(years)))
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
    files = section_files(out_dir, secs, source_url)
    print("  sections: {} extracted ({:,} chars of text)".format(len(files), len(text)))

    risk_text = secs.get("1A", {}).get("text", "")
    risk_heads = sections.risk_headings(soup, risk_text)
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
    data = trends.build(
        client.companyfacts(cik), max_years=args.years, as_of=filing["report_date"]
    )
    write(out_dir / "trends.json", json.dumps(data, indent=2))
    write(out_dir / "trends.md", trends.to_markdown(data, subs["name"]))
    print("  trends: {} fiscal years, {} metrics not tagged".format(
        len(data["fiscal_year_ends"]), len(data["missing"])))

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
        summary_markdown(meta, data, secs, files, risk_heads, diff, [s["name"] for s in stmts]),
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
    p.add_argument("--no-diff", action="store_true", help="skip the prior-year risk factor diff")
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
