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


@pytest.mark.network
def test_sec_client_live():
    """Hits SEC (cached after the first run). Skip with: -m 'not network'"""
    import sec_client

    sec_client.demo()
