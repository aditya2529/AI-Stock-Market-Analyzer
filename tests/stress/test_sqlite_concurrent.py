"""P34 stress test — SQLite reads under 8 workers (replays P29 territory).

Inherits the test body from the old ``tests/test_p24_buy_path.py`` (deleted
in this commit) and bumps iterations to the brief-mandated 200. Catches
regressions in ``data.database.load_ohlcv`` if the per-call connection
isolation introduced in the P29 fix is ever weakened.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor


WORKERS = 8
ITERATIONS = 200


def test_concurrent_load_ohlcv_no_crash():
    """N threads loading OHLCV data concurrently must survive 200 calls."""
    from data.database import load_ohlcv, list_tradeable_symbols
    available = list_tradeable_symbols()
    base = available[:8] if available else [
        "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS",
        "ICICIBANK.NS", "SBIN.NS", "KOTAKBANK.NS", "AXISBANK.NS",
    ]
    pool = (base * ((ITERATIONS // len(base)) + 1))[:ITERATIONS]

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(load_ohlcv, pool))

    assert len(results) == ITERATIONS
    assert all(r is not None for r in results)
