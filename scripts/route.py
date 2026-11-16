#!/usr/bin/env python3
"""Which extractor a ticker goes to. `run_10k.sh` and `serve.py` both come here.

    AAPL, SHOP      files a 10-K                      -> extract_10k.py
    DOL, ATD        EDGAR has never heard of it       -> extract_tsx.py
    RY, CNQ         on EDGAR, but files a 40-F        -> extract_tsx.py
    TSX:L, L.TO     says Toronto in so many words     -> extract_tsx.py

So a TSX company is typed like any other ticker. The one case that still needs
the exchange is a collision: bare L is Loews and bare T is AT&T, because a
10-K always wins, so Loblaw is TSX:L and Telus is TSX:T.

Learned the hard way:

  - "No 10-K in the recent filings" is not "files no 10-K". JPMorgan's recent
    block is 25,000 prospectuses and one 10-K; a bank can push it out
    entirely. A filer is only called foreign here when a 40-F, 20-F or 6-K is
    actually in view, and otherwise the full 10-K search runs.
  - Same letters, different company. SAP on EDGAR is SAP SE (a 20-F filer this
    tool can't read); SAP in Toronto is Saputo. A bare SAP gets Saputo, since
    it is the only one of the two there is anything to extract for.
"""

import os
import re
import sys

from sec_client import FOREIGN_FORMS, SECClient, SECError

SAYS_TORONTO = re.compile(r"^(TSX|TSE)[:.]|[.:-](TO|CA)$", re.I)


def on_tmx(ticker):
    """Does TMX Money list this symbol? One call, no key."""
    import extract_tsx as tsx  # here: the 10-K path shouldn't pay for this import

    try:
        quote = tsx.tmx(tsx.QUOTE_QUERY, symbol=tsx.parse_ticker(ticker)[1], locale="en")["q"]
    except SECError:
        return False
    return bool(quote and quote.get("name"))


def market(ticker, client=None, on_tmx=on_tmx):
    """'us' or 'tsx'. Raises SECError for something that isn't a ticker."""
    if SAYS_TORONTO.search(ticker.strip()):
        return "tsx"
    client = client or SECClient()
    client.normalize_ticker(ticker)  # garbage stops here, before any lookup
    try:
        cik = client.resolve_cik(ticker)
    except SECError:
        cik = None
    if cik:
        subs = client.submissions(cik)
        forms = set(subs["filings"]["recent"]["form"])
        if "10-K" in forms:
            return "us"
        if not forms & FOREIGN_FORMS and client.list_filings(cik, subs, "10-K", need=1):
            return "us"
    # Not a 10-K filer. Toronto if Toronto knows it; otherwise the 10-K path,
    # whose error message is the one that explains what went wrong.
    return "tsx" if on_tmx(ticker) else "us"


def main():
    args = sys.argv[1:]
    script = "extract_10k.py"
    if args and not args[0].startswith("-"):
        try:
            script = "extract_tsx.py" if market(args[0]) == "tsx" else script
        except SECError as e:
            print("ERROR: {}".format(e), file=sys.stderr)
            sys.exit(1)
    here = os.path.dirname(os.path.abspath(__file__))
    os.execv(sys.executable, [sys.executable, os.path.join(here, script)] + args)


def demo():
    """python3 route.py --self-check — offline, with a fake EDGAR and a fake TMX."""
    class Fake(SECClient):
        def __init__(self, known):
            self.known = known  # ticker -> the forms in its recent block

        def resolve_cik(self, ticker):
            if ticker.upper() not in self.known:
                raise SECError("not in the map")
            return 1

        def submissions(self, cik):
            return {"filings": {"recent": {"form": self.forms}}}

        def list_filings(self, cik, subs=None, form="10-K", need=2):
            return [{"form": "10-K"}] if self.buried else []

    def route(ticker, known=(), forms=(), buried=False, tmx=False):
        fake = Fake(set(known))
        fake.forms, fake.buried = list(forms), buried
        return market(ticker, fake, on_tmx=lambda t: tmx)

    assert route("TSX:L") == route("l.to") == route("RY-TO") == route("ry:ca") == "tsx"
    assert route("AAPL", known={"AAPL"}, forms=["10-K", "8-K"]) == "us"
    assert route("L", known={"L"}, forms=["10-K"], tmx=True) == "us"        # Loews, not Loblaw
    assert route("JPM", known={"JPM"}, forms=["424B2"], buried=True) == "us"  # 10-K pushed out of view
    assert route("RY", known={"RY"}, forms=["40-F", "6-K", "424B2"], tmx=True) == "tsx"
    assert route("DOL", tmx=True) == "tsx"                                  # EDGAR never heard of it
    assert route("ZZZZ") == "us"                                            # nobody has: 10-K path explains
    try:
        route("../etc")
        raise AssertionError("a path is not a ticker")
    except SECError:
        pass
    print("ok: route — explicit Toronto, 10-K wins a collision, 40-F and unknown go to TSX")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        demo()
    else:
        main()
