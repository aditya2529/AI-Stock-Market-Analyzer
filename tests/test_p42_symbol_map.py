"""P42 — symbol_map: yfinance key → Upstox instrument token.

RED at commit #1, GREEN at commit #2.

Contracts:
  - lookup(symbol) returns the Upstox instrument key for a known yfinance symbol
  - lookup(unknown) raises a clear KeyError-style exception with the symbol in the message
  - The map covers at minimum the 25 NSE symbols listed in config.DEFAULT_SYMBOLS
    (parity with the paper engine — same universe addressable on both paths)
  - The Upstox keys are correctly formatted (NSE_EQ|ISIN pattern), not bare ticker
"""
from __future__ import annotations

import re

import pytest

# RED at commit #1 — see test_p42_kill_switch.py for the importorskip pattern.
symbol_map = pytest.importorskip(
    "live_trading.symbol_map",
    reason="P42 commit #2 lands the impl",
)

# Sample of well-known yfinance ↔ Upstox mappings. These ISINs are public
# and stable (they don't change unless a company's legal entity restructures).
EXPECTED_MAPPINGS = {
    "RELIANCE.NS":  "NSE_EQ|INE002A01018",
    "TCS.NS":       "NSE_EQ|INE467B01029",
    "HDFCBANK.NS":  "NSE_EQ|INE040A01034",
    "INFY.NS":      "NSE_EQ|INE009A01021",
    "ICICIBANK.NS": "NSE_EQ|INE090A01021",
}


@pytest.mark.parametrize("yf,expected", list(EXPECTED_MAPPINGS.items()))
def test_lookup_returns_upstox_key_for_known(yf, expected):
    assert symbol_map.lookup(yf) == expected


def test_lookup_raises_on_unknown_symbol():
    with pytest.raises(KeyError, match="NOPESYMBOL.NS"):
        symbol_map.lookup("NOPESYMBOL.NS")


def test_lookup_is_case_sensitive():
    """yfinance and Upstox both use uppercase keys; lowercase = unknown."""
    with pytest.raises(KeyError):
        symbol_map.lookup("reliance.ns")


def test_lookup_rejects_empty_string():
    with pytest.raises((KeyError, ValueError)):
        symbol_map.lookup("")


def test_all_default_universe_symbols_mapped():
    """Every symbol in config.DEFAULT_SYMBOLS must be resolvable.

    This guards against drift: if someone adds a stock to the paper universe
    but forgets to map it for live, this test fires.
    """
    from config import DEFAULT_SYMBOLS
    missing = [s for s in DEFAULT_SYMBOLS if not _has(s)]
    assert not missing, f"unmapped symbols: {missing}"


def test_upstox_keys_match_nse_eq_isin_pattern():
    """Every value should look like 'NSE_EQ|INExxxxxxxxxx' (the canonical
    Upstox instrument-key format for NSE cash equities). Catches typos
    where someone pasted a bare ISIN or a ticker by accident."""
    pattern = re.compile(r"^NSE_EQ\|INE[A-Z0-9]{7}[0-9]{2}$")
    bad = []
    for symbol, key in symbol_map.ALL_MAPPINGS.items():
        if not pattern.match(key):
            bad.append((symbol, key))
    assert not bad, f"malformed keys: {bad}"


def _has(symbol: str) -> bool:
    try:
        symbol_map.lookup(symbol)
        return True
    except KeyError:
        return False
