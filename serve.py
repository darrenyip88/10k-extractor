#!/usr/bin/env python3
"""Serve the site and run the 10-K extractor behind it.

    python3 serve.py            # http://localhost:4321
    python3 serve.py --port 8080

Stdlib only — no Flask, no new dependencies. The site is static files; the one
dynamic route shells out to the same extractor the CLI uses, so the page and
`./run_10k.sh AAPL` can never drift apart.

    POST /api/extract  {"ticker": "AAPL"}  -> structured JSON for the page
    GET  /api/cached                       -> tickers already on disk
    GET  /filings/...                      -> the extracted files themselves
"""

import argparse
import json
import re
import subprocess
import sys
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
FILINGS = ROOT / "filings"
HOME = "v4-icarus.html"

# A cold extraction is ~12 SEC requests and ~15s. Give it room, but never hang
# a browser connection forever if SEC stalls.
EXTRACT_TIMEOUT = 180

TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")

# One extraction at a time. SEC asks for =<10 req/s and the client already
# paces itself; letting a page-refresh spawn parallel runs would blow past that
# and race two writers into the same filings/ directory.
_extract_lock = threading.Lock()


def money(v):
    """SEC tags raw dollars. Humans read millions."""
    if v is None:
        return None
    return round(v / 1_000_000, 1)


def load_filing(out_dir):
    """Read the extractor's own output into the shape the page renders."""
    out_dir = Path(out_dir).resolve()
    meta = json.loads((out_dir / "metadata.json").read_text())
    trends = json.loads((out_dir / "trends.json").read_text())

    fyes = trends["fiscal_year_ends"]
    latest = fyes[-1] if fyes else None
    derived = trends.get("derived", {})
    d = derived.get(latest, {}) if latest else {}
    series = trends.get("series", {})

    def at(name, year):
        return series.get(name, {}).get("values", {}).get(year)

    history = [
        {
            "year": y,
            "revenue_m": money(at("revenue", y)),
            "net_income_m": money(at("net_income", y)),
            "eps": at("eps_diluted", y),
        }
        for y in fyes
    ]

    risk = {"summary": None, "added": []}
    diff_path = out_dir / "risk_diff.md"
    if diff_path.exists():
        text = diff_path.read_text()
        m = re.search(r"\*\*(\d+ added .*?)\*\*", text)
        if m:
            risk["summary"] = m.group(1)
        added = re.search(r"## Added\n(.*?)(?:\n## |\Z)", text, re.S)
        if added:
            # top-level bullets only — each added risk carries an indented
            # "closest dropped risk" sub-bullet that is not itself a new risk
            risk["added"] = [
                ln[2:].strip()
                for ln in added.group(1).splitlines()
                if ln.startswith("- ")
            ]

    # Written since the valuation pass was added; older extractions predate it.
    vpath = out_dir / "valuation.json"
    valuation = None
    if vpath.exists():
        v = json.loads(vpath.read_text())
        q = v.get("quote") or {}
        valuation = {
            "price": q.get("price"),
            "price_as_of": q.get("as_of"),
            "price_source": q.get("source"),
            "price_is_floor": bool(q.get("is_floor")),
            "price_is_live": bool(q.get("is_live")),
            "public_float_m": money(q.get("public_float")),
            "cover_shares": (v["shares"].get("cover_page") or {}).get("total"),
            "fully_diluted": v["shares"].get("fully_diluted"),
            "market_cap_m": money(v.get("market_cap")),
            "ev_m": money(v["bridge"].get("enterprise_value")),
            "ev_missing": v["bridge"].get("missing_components") or [],
            "ebit_m": money(v["operating"].get("ebit")),
            "ebitda_m": money(v["operating"].get("ebitda")),
            "warning": v.get("market_cap_warning"),
            **{k: v["multiples"].get(k) for k in ("ev_sales", "ev_ebit", "ev_ebitda", "pe")},
        }
        # Flattened for the TTM block below, which merges it with trends.json.
        # Underscored because it is plumbing between two blocks here, not a
        # field the page reads off `valuation`.
        tt = v.get("ttm")
        if tt:
            valuation["_ttm"] = {
                "market_cap_m": money(tt.get("market_cap")),
                "ev_m": money(tt["bridge"].get("enterprise_value")),
                "shares": tt.get("shares"),
                "shares_basis": tt.get("shares_basis"),
                **{k: tt["multiples"].get(k) for k in (
                    "ev_sales", "ev_ebit", "ev_ebitda", "pe", "fcf_yield_pct")},
            }

    # Trailing twelve months — the 10-K year rolled forward with the 10-Qs filed
    # since. Additive: it sits beside the annual block on the page, never over
    # it, so the 10-K-only material (risk diff, share-comp footnote, audited
    # statements) is untouched. None when no 10-Q post-dates the filing.
    t = trends.get("ttm")
    ttm = None
    if t:
        tv = (valuation or {}).pop("_ttm", None) or {}
        vals, fy_vals, growth = t["values"], t["fiscal_year"], t["ytd_growth_pct"]
        ttm = {
            "quarter_end": t["quarter_end"],
            "quarter_filed": t["quarter_filed"],
            "fiscal_period": t["fiscal_period"],
            "basis": t["basis"],
            "days_past_year_end": t["days_stale_at_fy_end"],
            "weeks_ytd": t["weeks_year_to_date"],
            "prior_quarter_end": t["prior_quarter_end"],
            "not_tagged": t.get("not_tagged") or [],
            # Each line as (fiscal year, TTM, year-to-date growth) so the page
            # can show the gap rather than just the newer number.
            "rows": [
                {"label": label, "fy": fy_vals.get(k), "ttm": vals.get(k),
                 "growth_pct": growth.get(k), "money": money_row}
                for label, k, money_row in (
                    ("Revenue", "revenue", True),
                    ("Operating income", "operating_income", True),
                    ("Net income", "net_income", True),
                    ("EPS diluted", "eps_diluted", False),
                    ("Cash from ops", "operating_cash_flow", True),
                    ("Capex", "capex", True),
                    ("D&A", "d_and_a", True),
                )
            ],
            "fcf_m": money(t["derived"].get("free_cash_flow")),
            "ebit_m": money(t["derived"].get("ebit")),
            "ebitda_m": money(t["derived"].get("ebitda")),
            "cash_m": money(t["balance"].get("cash")),
            "total_debt_m": money(t["derived"].get("total_debt")),
            **{k: tv.get(k) for k in (
                "market_cap_m", "ev_m", "shares", "shares_basis",
                "ev_sales", "ev_ebit", "ev_ebitda", "pe", "fcf_yield_pct")},
        }

    # The most recent quarter and the balance sheet behind it. Both read the
    # 10-Qs and both stand on their own — a filer the TTM roll can't handle
    # (no comparable prior-year period) still has a latest balance sheet.
    mrq = trends.get("mrq")
    s = trends.get("snapshot")
    snapshot = None
    if s:
        sv, sh = s["values"], s["shares"]
        snapshot = {
            "as_of": s["as_of"],
            "form": s["form"],
            "filed": s["filed"],
            "cash_st_inv_m": money(s.get("cash_and_st_investments")),
            "total_debt_m": money(s.get("total_debt")),
            "net_cash_m": money(s.get("net_cash")),
            "total_assets_m": money(sv.get("total_assets")),
            "inventory_m": money(sv.get("inventory")),
            "total_equity_m": money(sv.get("total_equity")),
            "cover_shares": sh.get("cover_page"),
            "cover_shares_as_of": sh.get("cover_page_as_of"),
            "shares_diluted": sh.get("weighted_average_diluted"),
        }

    rel = out_dir.relative_to(ROOT).as_posix()
    files = []
    for p in sorted(out_dir.rglob("*")):
        if p.is_file():
            files.append({
                "name": p.relative_to(out_dir).as_posix(),
                "href": "/" + p.relative_to(ROOT).as_posix(),
                "kb": round(p.stat().st_size / 1024, 1),
            })

    return {
        "ticker": meta.get("ticker"),
        "company": meta.get("company"),
        "cik": meta.get("cik"),
        "sic": meta.get("sic_description"),
        "exchanges": meta.get("exchanges") or [],
        "auditor": meta.get("auditor_name"),
        "period": meta.get("period"),
        "filed": meta.get("filed"),
        "source_url": meta.get("source_url"),
        "dir": rel,
        "latest": {
            "revenue_m": money(at("revenue", latest)),
            "revenue_growth_pct": d.get("revenue_growth_pct"),
            "gross_margin_pct": d.get("gross_margin_pct"),
            "operating_margin_pct": d.get("operating_margin_pct"),
            "net_income_m": money(at("net_income", latest)),
            "net_margin_pct": d.get("net_margin_pct"),
            "eps": at("eps_diluted", latest),
            "eps_growth_pct": d.get("eps_growth_pct"),
            "fcf_m": money(d.get("free_cash_flow")),
            "fcf_margin_pct": d.get("fcf_margin_pct"),
            "roe_pct": d.get("roe_pct"),
            "total_debt_m": money(d.get("total_debt")),
            "net_cash_m": money(d.get("net_cash")),
            "shares_change_pct": d.get("shares_change_pct"),
            # The rest of the figures a valuation model asks for, all as filed.
            "operating_income_m": money(at("operating_income", latest)),
            "d_and_a_m": money(at("d_and_a", latest)),
            "cash_st_inv_m": money(d.get("cash_and_st_investments")),
            "shares_diluted": at("shares_diluted", latest),
            "total_assets_m": money(at("total_assets", latest)),
            "inventory_m": money(at("inventory", latest)),
            "operating_cash_flow_m": money(at("operating_cash_flow", latest)),
            "capex_m": money(at("capex", latest)),
            "membership_fee_m": money(
                trends.get("statement_lines", {}).get("membership_fee_income")),
            # The quarter comes off the latest 10-Q, falling back to a fourth
            # quarter out of the 10-K itself. `d["mrq_revenue"]` is the 10-K-only
            # figure and is blank for almost every modern filer.
            "mrq_revenue_m": money((mrq or {}).get("revenue", d.get("mrq_revenue"))),
            "mrq_revenue_prior_m": money(
                (mrq or {}).get("prior_revenue", d.get("mrq_revenue_prior"))),
            "mrq_quarter_end": (mrq or {}).get("quarter_end"),
            "mrq_prior_quarter_end": (mrq or {}).get("prior_quarter_end"),
            "mrq_growth_pct": (mrq or {}).get("growth_pct"),
        },
        # Stock items as of the newest filing — no LTM arithmetic, one date.
        "snapshot": snapshot,
        "valuation": valuation,
        "ttm": ttm,
        "cagr": derived.get("cagr", {}),
        "history": history,
        "risk": risk,
        # trends.json records these as {"metric":..., "tried":[concepts]};
        # the page only needs the metric name
        "missing": [
            m.get("metric") if isinstance(m, dict) else str(m)
            for m in trends.get("missing", [])
        ],
        "files": files,
    }


def newest_dir(ticker):
    """Latest already-extracted filing for a ticker, if any."""
    base = FILINGS / ticker.upper().replace(".", "-")
    if not base.is_dir():
        return None
    periods = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name)
    return periods[-1] if periods else None


def run_extractor(ticker):
    """Shell out to the same script the CLI uses. Returns (out_dir, log)."""
    proc = subprocess.run(
        [sys.executable, "extract_10k.py", ticker],
        cwd=str(ROOT / "scripts"),
        capture_output=True, text=True, timeout=EXTRACT_TIMEOUT,
    )
    log = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(log.strip() or "extractor failed with no output")

    # a fresh run ends "Done: <dir>"; an already-extracted one short-circuits
    # with "Already extracted: <dir>" and never reaches the Done line
    m = re.search(r"^(?:Done|Already extracted): (.+)$", proc.stdout or "", re.M)
    out_dir = Path(m.group(1).strip()) if m else newest_dir(ticker)
    if not out_dir or not Path(out_dir).is_dir():
        raise RuntimeError("extractor finished but no output directory was found")
    return Path(out_dir), log


# Stdlib's default error page is unstyled black-on-white, which in an all-
# #222222 site reads as the browser breaking rather than the path being wrong.
# error_message_format is the handler's own hook, so this costs no new route.
# Every literal % is doubled — this string goes through %-formatting, and a
# bare "100%" in the CSS would raise instead of render.
ERROR_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(code)d — Filed</title>
<meta name="theme-color" content="#222222">
<link rel="icon" href="/assets/favicon-icarus.png" type="image/png">
<style>
@font-face{font-family:"Libre Caslon Display";src:url("/assets/fonts/libre-caslon-display.woff2") format("woff2");font-display:swap}
body{margin:0;min-height:100dvh;display:grid;align-content:center;
  padding:2rem clamp(1.15rem,4.5vw,3.25rem);background:#222;color:#B79C8D;
  font:15px/1.7 -apple-system,Helvetica,Arial,sans-serif}
h1{margin:0 0 .6rem;font-family:"Libre Caslon Display",Baskerville,serif;
  font-weight:400;font-size:clamp(2rem,5vw,3.4rem);letter-spacing:-.015em;color:#EFD5C8}
p{margin:0;max-width:52ch;font-size:.8125rem}
a{color:#EFD5C8;text-decoration:none;border-bottom:1px solid rgba(239,213,200,.15)}
a:hover{border-bottom-color:#EFD5C8}
.n{font-family:ui-monospace,Menlo,monospace;font-size:.75rem;letter-spacing:.16em;
  text-transform:uppercase;color:#A98D7D;margin:0 0 1rem}
</style></head><body>
<p class="n">%(code)d %(message)s</p>
<h1>Nothing at that path</h1>
<p>%(explain)s. Every other error on this server answers as JSON, so if you are
seeing this page you asked for a file that isn't here.
<a href="/">Back to the ticker box</a>.</p>
</body></html>
"""


class Handler(SimpleHTTPRequestHandler):
    error_message_format = ERROR_PAGE

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(SITE), **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers ---------------------------------------------------------
    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            self.path = "/" + HOME
            return SimpleHTTPRequestHandler.do_GET(self)

        if path == "/api/cached":
            out = []
            if FILINGS.is_dir():
                for t in sorted(p.name for p in FILINGS.iterdir() if p.is_dir()):
                    d = newest_dir(t)
                    if d:
                        out.append({"ticker": t, "period": d.name})
            return self.send_json({"cached": out})

        # the extracted filings themselves, so the file list is clickable
        if path.startswith("/filings/"):
            rel = unquote(path.lstrip("/"))
            target = (ROOT / rel).resolve()
            try:
                target.relative_to(FILINGS.resolve())
            except ValueError:
                return self.send_json({"error": "forbidden"}, 403)
            if not target.is_file():
                return self.send_json({"error": "not found"}, 404)
            data = target.read_bytes()
            ctype = "text/plain; charset=utf-8"
            if target.suffix == ".json":
                ctype = "application/json; charset=utf-8"
            elif target.suffix in (".htm", ".html"):
                # the raw filing is untrusted third-party HTML; hand it over as
                # text so the browser never executes anything inside it
                ctype = "text/plain; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        if urlparse(self.path).path != "/api/extract":
            return self.send_json({"error": "not found"}, 404)

        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                return self.send_json({"error": "request too large"}, 413)
            payload = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "bad request"}, 400)

        ticker = str(payload.get("ticker", "")).strip().upper()
        if not TICKER_RE.match(ticker):
            return self.send_json(
                {"error": "That doesn't look like a ticker. Try AAPL, BRK.B, JPM."}, 400)

        started = datetime.now()
        cached_before = newest_dir(ticker)

        if not _extract_lock.acquire(blocking=False):
            return self.send_json(
                {"error": "Another extraction is already running. Give it a few seconds."},
                429)
        try:
            out_dir, log = run_extractor(ticker)
            data = load_filing(out_dir)
        except subprocess.TimeoutExpired:
            return self.send_json(
                {"error": "SEC didn't answer in time. Try again in a moment."}, 504)
        except RuntimeError as e:
            msg = str(e)
            # the browser gets one line; the whole traceback goes to the log,
            # or a failure that isn't an ERROR: line is undiagnosable from the
            # 400 characters the page can show
            sys.stderr.write("  extractor failed for %s:\n%s\n" % (ticker, msg))
            tail = [ln for ln in msg.splitlines() if ln.strip().startswith("ERROR:")]
            return self.send_json({"error": (tail[-1][6:].strip() if tail else msg)[:400]}, 502)
        except Exception as e:  # noqa: BLE001 - surface the real cause, don't 500 blindly
            return self.send_json({"error": "{}: {}".format(type(e).__name__, e)[:400]}, 500)
        finally:
            _extract_lock.release()

        m = re.search(r"SEC requests: (\d+), cache hits: (\d+)", log)
        data["run"] = {
            "seconds": round((datetime.now() - started).total_seconds(), 1),
            "sec_requests": int(m.group(1)) if m else None,
            "cache_hits": int(m.group(2)) if m else None,
            "was_cached": bool(cached_before),
        }
        return self.send_json(data)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=4321)
    args = p.parse_args()

    if not SITE.is_dir():
        sys.exit("site/ not found next to serve.py")

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Filed  ->  http://localhost:{}".format(args.port))
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
