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
            "mrq_revenue_m": money(d.get("mrq_revenue")),
            "mrq_revenue_prior_m": money(d.get("mrq_revenue_prior")),
        },
        "valuation": valuation,
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


class Handler(SimpleHTTPRequestHandler):
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
            payload = json.loads(self.rfile.read(n) or b"{}")
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
    print("10K AI  ->  http://localhost:{}".format(args.port))
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
