"""P42 — yfinance symbol -> Upstox instrument key.

Static dict covering the 25 ``config.DEFAULT_SYMBOLS`` NSE universe.

WHY STATIC, NOT RUNTIME-FETCH
-----------------------------
Upstox publishes a full instruments master (~5 MB CSV) at
https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz —
the "right" way is to fetch + cache + look up by name. We deliberately don't:

  - Network call at import / startup time = new failure mode for a path
    we want to keep deterministic
  - Master file is ~120k rows. Parsing it is fast but adds dependency on
    pandas/csv at import — out of scope for a single-trade demo path
  - Demo universe is bounded (25 symbols, all liquid NSE equities)

THE TRADE-OFF
-------------
ISINs are stable identifiers (they don't churn unless a company restructures
its legal entity). The 25 listed below were correct as of 2026-05. Operator
should re-verify before any **prod** live trade via the Upstox web app's
instrument-lookup or the master CSV. Sandbox usage carries no money risk
even if an ISIN here is wrong — the sandbox simulator rejects unknown
instrument keys with a clear error.

ADDING A SYMBOL
---------------
1. Get the ISIN from NSE (https://www.nseindia.com/get-quotes/equity).
2. Add the entry to ``ALL_MAPPINGS`` below.
3. Re-run ``pytest tests/test_p42_symbol_map.py`` — the format check
   (``test_upstox_keys_match_nse_eq_isin_pattern``) catches typos.
"""
from __future__ import annotations


# yfinance ticker (uppercase) -> Upstox instrument key (NSE_EQ|ISIN)
ALL_MAPPINGS: dict[str, str] = {
    # IT
    "TCS.NS":         "NSE_EQ|INE467B01029",
    "INFY.NS":        "NSE_EQ|INE009A01021",
    "WIPRO.NS":       "NSE_EQ|INE075A01022",
    "HCLTECH.NS":     "NSE_EQ|INE860A01027",
    # Banking
    "HDFCBANK.NS":    "NSE_EQ|INE040A01034",
    "ICICIBANK.NS":   "NSE_EQ|INE090A01021",
    "KOTAKBANK.NS":   "NSE_EQ|INE237A01028",
    "AXISBANK.NS":    "NSE_EQ|INE238A01034",
    "SBIN.NS":        "NSE_EQ|INE062A01020",
    # Energy
    "RELIANCE.NS":    "NSE_EQ|INE002A01018",
    "ONGC.NS":        "NSE_EQ|INE213A01029",
    "BPCL.NS":        "NSE_EQ|INE029A01011",
    # Auto
    "MARUTI.NS":      "NSE_EQ|INE585B01010",
    "M&M.NS":         "NSE_EQ|INE101A01026",
    "BAJAJ-AUTO.NS":  "NSE_EQ|INE917I01010",
    # Pharma
    "SUNPHARMA.NS":   "NSE_EQ|INE044A01036",
    "DRREDDY.NS":     "NSE_EQ|INE089A01023",
    "CIPLA.NS":       "NSE_EQ|INE059A01026",
    # FMCG
    "HINDUNILVR.NS":  "NSE_EQ|INE030A01027",
    "NESTLEIND.NS":   "NSE_EQ|INE239A01016",
    "BRITANNIA.NS":   "NSE_EQ|INE216A01030",
    # Metals
    "TATASTEEL.NS":   "NSE_EQ|INE081A01020",
    "HINDALCO.NS":    "NSE_EQ|INE038A01020",
    # Infra / Telecom
    "BHARTIARTL.NS":  "NSE_EQ|INE397D01024",
    "LT.NS":          "NSE_EQ|INE018A01030",
}


def lookup(symbol: str) -> str:
    """Return the Upstox instrument key for a yfinance symbol.

    Case-sensitive. Raises ``KeyError`` with the symbol embedded in the
    message so failures are easy to grep / read in tracebacks. Empty input
    is treated as unknown (same KeyError path).
    """
    if not symbol:
        raise KeyError(f"empty symbol (got {symbol!r})")
    if symbol not in ALL_MAPPINGS:
        raise KeyError(
            f"unknown symbol {symbol!r}; add it to "
            f"live_trading.symbol_map.ALL_MAPPINGS"
        )
    return ALL_MAPPINGS[symbol]
