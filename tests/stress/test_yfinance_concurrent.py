"""P34 stress test — yfinance fetch under 8 workers (covers P31 surface).

Concurrent ``yf.Ticker.history`` calls exercise curl_cffi (yfinance's libcurl
backend) under threadpool fan-out + tear-down — the same path that produced
P31's access-violation on shutdown. The test does NOT assert data correctness
(yfinance may rate-limit or return empty under load); it only asserts that
the Python interpreter survives the run. Heap corruption / access violation
would manifest as the worker pool failing to return.

Network is required. If the network is unavailable the test still passes
(empty results are tolerated), but in that case it is not exercising the
target race surface — that's acceptable for a smoke gate.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor


WORKERS = 8
ITERATIONS = 200
SYMBOLS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS",
    "ICICIBANK.NS", "SBIN.NS", "KOTAKBANK.NS", "AXISBANK.NS",
]


def _fetch(symbol: str):
    import yfinance as yf
    try:
        return yf.Ticker(symbol).history(period="1d", interval="5m", timeout=5)
    except Exception:
        return None


def test_concurrent_yfinance_history_no_crash():
    """N threads calling yf.Ticker.history concurrently must survive 200 calls."""
    pool = (SYMBOLS * ((ITERATIONS // len(SYMBOLS)) + 1))[:ITERATIONS]

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(_fetch, pool))

    assert len(results) == ITERATIONS
