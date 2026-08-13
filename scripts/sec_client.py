#!/usr/bin/env python3
"""SEC EDGAR client — every network call in this project goes through here.

Free, no API key, no quota. SEC only asks for two things:
  1. A User-Agent header with a real contact email (a generic UA gets a 403).
  2. Max 10 requests/second.

Filings are immutable once accepted, so responses are cached to disk forever.
"""

import hashlib
import json
import re
import sys
import time
import warnings
from pathlib import Path

# macOS system Python links LibreSSL, so urllib3 warns on import. Nothing here
# can act on it and it fires on every single run.
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

import requests  # noqa: E402

UA = "Darren Yip darrenyip28@gmail.com"
RATE_LIMIT_DELAY = 0.15  # SEC allows 10 req/s; 0.15s leaves headroom
MAX_RETRIES = 3
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_OVERFLOW_FILES = 5  # how many archive pages to walk looking for older 10-Ks
TICKER_MAP_MAX_AGE = 86400  # SEC adds tickers through the day; filings never change
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{name}"

# Forms that mean "this company doesn't file a 10-K" — worth a clear message
# instead of an empty-list crash.
FOREIGN_FORMS = {"20-F", "40-F", "6-K"}


class SECError(Exception):
    """Anything the user can act on: unknown ticker, no 10-K, fetch failure."""


class SECClient:
    def __init__(self, refresh=False):
        self.refresh = refresh
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
        self.last_call = 0.0
        self.requests_made = 0
        self.cache_hits = 0
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # --- transport -------------------------------------------------------

    def _cache_path(self, url):
        # Keep a readable tail so the cache dir is greppable when debugging.
        tail = re.sub(r"[^A-Za-z0-9._-]", "_", url.rsplit("/", 1)[-1])[:40]
        return CACHE_DIR / f"{hashlib.sha256(url.encode()).hexdigest()[:16]}_{tail}"

    def get(self, url, binary=False, max_age=None):
        """Fetch a URL, using the disk cache unless --refresh was passed.

        `max_age` (seconds) treats an older cache entry as a miss. Nothing
        else here needs it — filings are immutable once accepted — but the
        ticker map isn't a filing: SEC adds tickers through the day, and
        without this a company that IPO'd after the map was first cached
        reads as "not in SEC's company_tickers.json" forever, with no hint
        that the fix is --refresh.
        """
        path = self._cache_path(url)
        fresh = path.exists() and not self.refresh
        if fresh and max_age is not None:
            fresh = (time.time() - path.stat().st_mtime) < max_age
        if fresh:
            self.cache_hits += 1
            # Match the decode below: a first fetch that hit a non-UTF-8 byte
            # (older filings, cp1252 punctuation) wrote fine with errors="ignore".
            # strict decode here would pass on that run and crash the next one —
            # the exact "re-run is a no-op" path this cache exists for.
            return path.read_bytes() if binary else path.read_bytes().decode("utf-8", "ignore")

        # SEC throws occasional 503s under load — a single one silently cost us
        # Tesla's balance sheet during testing. Retry with backoff.
        resp = None
        for attempt in range(MAX_RETRIES):
            elapsed = time.time() - self.last_call
            if elapsed < RATE_LIMIT_DELAY:
                time.sleep(RATE_LIMIT_DELAY - elapsed)
            try:
                resp = self.session.get(url, timeout=60)
            except requests.exceptions.RequestException as e:
                if attempt == MAX_RETRIES - 1:
                    raise SECError("network error fetching {}: {}".format(url, e))
                time.sleep(2 ** attempt)
                continue
            finally:
                self.last_call = time.time()
            self.requests_made += 1
            if resp.status_code in RETRY_STATUS and attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            break

        if resp.status_code == 403:
            raise SECError(
                "SEC returned 403 for {}. This is almost always the User-Agent — "
                "it must contain a real contact email (currently: {!r}).".format(url, UA)
            )
        if resp.status_code == 404:
            raise SECError("not found: {}".format(url))
        if resp.status_code != 200:
            raise SECError("HTTP {} for {}".format(resp.status_code, url))

        path.write_bytes(resp.content)
        return resp.content if binary else resp.content.decode("utf-8", "ignore")

    def get_json(self, url, max_age=None):
        return json.loads(self.get(url, max_age=max_age))

    # --- lookups ---------------------------------------------------------

    # Real tickers are short and alnum-with-dot/dash. Enforced here, not just
    # in resolve_cik, because --cik skips resolve_cik entirely — without this,
    # a ticker string built into a filesystem path ("../../etc") would ride
    # straight through to the output directory.
    TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

    @classmethod
    def normalize_ticker(cls, ticker):
        """SEC writes share classes with a dash: BRK.B -> BRK-B."""
        ticker = ticker.strip().upper()
        if not cls.TICKER_RE.match(ticker):
            raise SECError(
                "{!r} doesn't look like a ticker (letters/digits/dot/dash, max 10 chars).".format(ticker)
            )
        return ticker.replace(".", "-")

    def resolve_cik(self, ticker):
        """Ticker -> CIK int. ~10,400 tickers in one cached file."""
        ticker = self.normalize_ticker(ticker)
        data = self.get_json(TICKER_MAP_URL, max_age=TICKER_MAP_MAX_AGE)
        for entry in data.values():
            if entry["ticker"] == ticker:
                return int(entry["cik_str"])
        raise SECError(
            "ticker {!r} is not in SEC's company_tickers.json. It may be a foreign issuer, "
            "a fund, or delisted. If you know the CIK, pass --cik.".format(ticker)
        )

    def submissions(self, cik):
        return self.get_json(SUBMISSIONS_URL.format(cik=cik))

    def companyfacts(self, cik):
        """All XBRL facts the company ever tagged. One call, ~4MB for AAPL."""
        return self.get_json(FACTS_URL.format(cik=cik))

    @staticmethod
    def _collect(block, want):
        out = []
        for i, form in enumerate(block["form"]):
            # Exact form only. 10-K/A and 10-Q/A amendments usually restate a
            # fragment, not the whole document, so they'd produce half-empty
            # sections.
            if form != want:
                continue
            out.append(
                {
                    "form": form,
                    "filing_date": block["filingDate"][i],
                    "report_date": block["reportDate"][i],
                    "accession": block["accessionNumber"][i],
                    "accession_plain": block["accessionNumber"][i].replace("-", ""),
                    "primary_doc": block["primaryDocument"][i],
                }
            )
        return out

    def list_filings(self, cik, subs=None, form="10-K", need=2):
        """Return filings of one form, newest first.

        `filings.recent` holds the most recent filings of *any* type, so a heavy
        filer blows past its own 10-K history: JPMorgan's recent block has 25,613
        filings and exactly one 10-K, the rest being prospectuses. Older filings
        live in the paginated `filings.files` archives, so walk those until we
        have `need` filings (only the prior year is needed, for the risk diff).
        """
        subs = subs or self.submissions(cik)
        out = self._collect(subs["filings"]["recent"], form)

        for archive in (subs["filings"].get("files") or [])[:MAX_OVERFLOW_FILES]:
            if len(out) >= need:
                break
            page = self.get_json("https://data.sec.gov/submissions/{}".format(archive["name"]))
            out += self._collect(page, form)

        return sorted(out, key=lambda f: f["report_date"], reverse=True)

    def list_10ks(self, cik, subs=None, need=2):
        """The 10-Ks, newest first — with the message a filer that files none needs."""
        subs = subs or self.submissions(cik)
        out = self.list_filings(cik, subs, "10-K", need)
        if not out:
            forms = set(subs["filings"]["recent"]["form"])
            hint = ""
            if forms & FOREIGN_FORMS:
                hint = (
                    " This looks like a foreign private issuer — it files "
                    "{} instead of a 10-K.".format("/".join(sorted(forms & FOREIGN_FORMS)))
                )
            raise SECError("no 10-K filings found for CIK {}.{}".format(cik, hint))
        return out

    def filing_url(self, cik, accession_plain, name):
        return ARCHIVE_URL.format(cik=cik, acc=accession_plain, name=name)

    def filing_file(self, cik, accession_plain, name, binary=False):
        return self.get(self.filing_url(cik, accession_plain, name), binary=binary)

    def stats(self):
        return {"requests_made": self.requests_made, "cache_hits": self.cache_hits}


def demo():
    """Live smoke check: python3 sec_client.py"""
    c = SECClient()
    cik = c.resolve_cik("aapl")
    assert cik == 320193, cik
    assert c.normalize_ticker("brk.b") == "BRK-B"
    filings = c.list_10ks(cik)
    assert filings[0]["primary_doc"].endswith(".htm"), filings[0]
    assert filings[0]["report_date"] > filings[1]["report_date"], "not newest-first"
    print("ok: AAPL cik={} latest 10-K {} ({} total)".format(
        cik, filings[0]["report_date"], len(filings)))
    print("   ", c.stats())


if __name__ == "__main__":
    try:
        demo()
    except SECError as e:
        print("ERROR: {}".format(e), file=sys.stderr)
        sys.exit(1)
