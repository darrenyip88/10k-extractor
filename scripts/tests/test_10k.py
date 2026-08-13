"""pytest entry point.

The real assertions live in each module's `demo()` so that `python3 sections.py`
is a working self-check on its own. This file just makes them discoverable by
pytest rather than restating the same assertions in two places.

    python3 -m pytest tests/ -q      (from the scripts/ directory)

Only test_sec_client_live touches the network.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sections  # noqa: E402
import statements  # noqa: E402
import trends  # noqa: E402
import valuation  # noqa: E402


def test_sections():
    sections.demo()


def test_statements():
    statements.demo()


def test_trends():
    trends.demo()


def test_valuation():
    valuation.demo()


def test_extract():
    import extract_10k

    extract_10k.demo()


@pytest.mark.network
def test_live_quote():
    """The one non-SEC call in the project. Undocumented endpoint, so this is
    the thing that tells you it changed — a failure must still be a dict."""
    q = valuation.live_quote("AAPL")
    assert isinstance(q, dict), q
    assert q.get("price") or q.get("error"), q
    if q.get("price"):
        assert q["price"] > 0 and q["is_live"] and q["as_of"], q


@pytest.mark.network
def test_sec_client_live():
    """Hits SEC (cached after the first run). Skip with: -m 'not network'"""
    import sec_client

    sec_client.demo()
