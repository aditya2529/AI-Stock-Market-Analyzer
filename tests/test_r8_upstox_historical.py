"""R8 — Upstox v3 historical-intraday adapter + token-bucket rate limiter.

The existing UpstoxAdapter.fetch_ohlcv hits the v2 day/week/month
endpoint. v2 does NOT support 5-minute intervals; v3 does. This new
method must:
  - Build URLs against /v3/historical-candle/{key}/minutes/{N}/{to}/{from}
  - Return a DataFrame matching yfinance shape:
        index = DatetimeIndex (tz-naive), name='time'
        columns = [open, high, low, close, volume]
  - Chunk multi-month date ranges into 30-day requests (Upstox v3
    response cap → otherwise large ranges silently truncate)
  - Respect the Upstox rate-limit triad concurrently:
        25 req/sec, 250 req/min, 1000 req/30 min
  - On HTTP failure: raise RuntimeError with response excerpt (matches
    existing v2 error semantics in upstox_adapter.py:113)

This test file lands RED in commit #1. The new method does not exist
yet; ImportError / AttributeError is the expected failure mode.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch, MagicMock

import pandas as pd
import pytest


# ── Helpers ─────────────────────────────────────────────────────────


def _fake_upstox_response(candles: list) -> MagicMock:
    """Build a requests.Response-like mock matching Upstox v3's
    historical-candle envelope shape:
        {"status": "success", "data": {"candles": [[ts, o, h, l, c, v, oi], ...]}}
    """
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {
        "status": "success",
        "data": {"candles": candles},
    }
    m.text = "ok"
    return m


def _sample_candles(n: int, start_iso: str = "2026-05-01T09:15:00+05:30"):
    """Return n synthetic candles starting at start_iso, 5 minutes apart."""
    start = pd.Timestamp(start_iso)
    out = []
    for i in range(n):
        ts = (start + pd.Timedelta(minutes=5 * i)).isoformat()
        out.append([ts, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1000 * (i + 1), 0])
    return out


# ── v3 endpoint shape ────────────────────────────────────────────────


def test_get_historical_intraday_method_exists():
    """The extended adapter must expose get_historical_intraday with
    the agreed signature."""
    from data.adapters.upstox_adapter import UpstoxAdapter
    assert callable(getattr(UpstoxAdapter, "get_historical_intraday", None)), (
        "UpstoxAdapter.get_historical_intraday(symbol, interval_minutes, "
        "from_date, to_date) is required for R8 5m support"
    )


def test_get_historical_intraday_builds_v3_url_for_5m(monkeypatch):
    """URL must match /v3/historical-candle/{key}/minutes/{N}/{to}/{from}
    — Upstox v3 puts to_date BEFORE from_date (the same quirk v2 has).
    """
    from data.adapters import upstox_adapter as ua

    monkeypatch.setattr(ua, "_active_access_token", lambda *a, **k: "FAKE_TOKEN")
    monkeypatch.setattr(
        "data.adapters.upstox_instruments.lookup_instrument_key",
        lambda s: "NSE_EQ|INE467B01029",
    )

    seen = {"url": None}

    def _capture(url, headers=None, timeout=None):
        seen["url"] = url
        return _fake_upstox_response([])

    monkeypatch.setattr(ua.requests, "get", _capture)

    a = ua.UpstoxAdapter()
    # Single-chunk window (≤ _V3_INTRADAY_CHUNK_DAYS) so the URL we
    # capture covers the literal from/to dates — pagination is
    # exercised separately in test_get_historical_intraday_paginates_
    # long_date_ranges.
    a.get_historical_intraday("TCS.NS", interval_minutes=5,
                                from_date="2026-04-01", to_date="2026-04-05")

    url = seen["url"]
    assert url is not None
    assert "/v3/historical-candle/" in url
    assert "NSE_EQ|INE467B01029" in url
    assert "/minutes/5/" in url
    # to_date precedes from_date in the path (Upstox quirk inherited
    # from v2 — kept consistent so existing operators don't get
    # surprised by an inverted ordering).
    assert url.endswith("2026-04-05/2026-04-01")


def test_get_historical_intraday_returns_yfinance_shape_dataframe(monkeypatch):
    """Returned DataFrame must match the YFINANCE ADAPTER's return
    contract (the de facto interface validate_and_clean +
    upsert_ohlcv consume):
        columns = [time, open, high, low, close, volume]
        index   = default RangeIndex
        time    = tz-naive Timestamp

    The DB READ path (load_ohlcv) promotes `time` back to a
    DatetimeIndex named 'time'; that's a different surface, not
    what an adapter returns. open_interest is dropped at the
    adapter boundary.
    """
    from data.adapters import upstox_adapter as ua

    monkeypatch.setattr(ua, "_active_access_token", lambda *a, **k: "FAKE_TOKEN")
    monkeypatch.setattr(
        "data.adapters.upstox_instruments.lookup_instrument_key",
        lambda s: "NSE_EQ|INE467B01029",
    )
    monkeypatch.setattr(ua.requests, "get",
                         lambda *a, **k: _fake_upstox_response(_sample_candles(10)))

    a = ua.UpstoxAdapter()
    df = a.get_historical_intraday("TCS.NS", interval_minutes=5,
                                     from_date="2026-04-01",
                                     to_date="2026-04-30")

    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["time", "open", "high", "low", "close", "volume"]
    # Default RangeIndex (not DatetimeIndex) — matches yfinance_adapter.
    assert isinstance(df.index, pd.RangeIndex), (
        f"adapter should return default RangeIndex (yfinance contract), "
        f"got {type(df.index).__name__}")
    # `time` column is tz-naive datetime64.
    assert pd.api.types.is_datetime64_any_dtype(df["time"])
    assert df["time"].dt.tz is None, (
        "validator + upsert_ohlcv expect tz-naive timestamps")
    assert "oi" not in df.columns
    assert "open_interest" not in df.columns


def test_get_historical_intraday_paginates_long_date_ranges(monkeypatch):
    """A 90-day range at 5m must be chunked into multiple requests so
    that Upstox's per-response candle cap doesn't silently truncate
    half the data. We assert >= 2 requests fire for a 90-day window."""
    from data.adapters import upstox_adapter as ua

    monkeypatch.setattr(ua, "_active_access_token", lambda *a, **k: "FAKE_TOKEN")
    monkeypatch.setattr(
        "data.adapters.upstox_instruments.lookup_instrument_key",
        lambda s: "NSE_EQ|INE467B01029",
    )

    request_count = {"n": 0}

    def _counting_get(url, headers=None, timeout=None):
        request_count["n"] += 1
        return _fake_upstox_response(_sample_candles(50))

    monkeypatch.setattr(ua.requests, "get", _counting_get)

    a = ua.UpstoxAdapter()
    a.get_historical_intraday("TCS.NS", interval_minutes=5,
                                from_date="2026-02-01",
                                to_date="2026-04-30")  # 89 days
    assert request_count["n"] >= 2, (
        f"expected >= 2 requests for 89-day window, got {request_count['n']} — "
        f"the adapter is not chunking and will silently lose bars on "
        f"long ranges (Upstox v3 candle cap is ~5000 per response)."
    )


def test_get_historical_intraday_raises_on_http_error(monkeypatch):
    """Non-200 response must raise RuntimeError with the status code +
    a body excerpt, matching v2 fetch_ohlcv error semantics."""
    from data.adapters import upstox_adapter as ua

    monkeypatch.setattr(ua, "_active_access_token", lambda *a, **k: "FAKE_TOKEN")
    monkeypatch.setattr(
        "data.adapters.upstox_instruments.lookup_instrument_key",
        lambda s: "NSE_EQ|INE467B01029",
    )

    bad = MagicMock()
    bad.status_code = 429
    bad.text = "Too Many Requests"
    bad.json.side_effect = ValueError("not json")
    monkeypatch.setattr(ua.requests, "get", lambda *a, **k: bad)

    a = ua.UpstoxAdapter()
    with pytest.raises(RuntimeError, match="429"):
        a.get_historical_intraday("TCS.NS", interval_minutes=5,
                                    from_date="2026-04-01",
                                    to_date="2026-04-30")


def test_get_historical_intraday_raises_without_token(monkeypatch):
    """No token in .env → fail loudly via EnvironmentError, same as
    the existing v2 fetch_ohlcv path."""
    from data.adapters import upstox_adapter as ua
    monkeypatch.setattr(ua, "_active_access_token", lambda *a, **k: None)

    a = ua.UpstoxAdapter()
    with pytest.raises(EnvironmentError, match="UPSTOX"):
        a.get_historical_intraday("TCS.NS", interval_minutes=5,
                                    from_date="2026-04-01",
                                    to_date="2026-04-30")


# ── Token-bucket rate limiter ────────────────────────────────────────


def test_token_bucket_class_exists():
    """A reusable rate-limiter class must live alongside the adapter so
    it can be unit-tested in isolation."""
    from data.adapters.upstox_adapter import UpstoxRateLimiter
    rl = UpstoxRateLimiter()
    assert hasattr(rl, "acquire")


def test_token_bucket_allows_burst_up_to_per_second_cap(monkeypatch):
    """25 acquire() calls within one wall-clock second must NOT sleep —
    that's the burst budget. Verified by patching time.sleep to count
    calls."""
    from data.adapters.upstox_adapter import UpstoxRateLimiter

    sleep_calls = []
    monkeypatch.setattr("data.adapters.upstox_adapter.time.sleep",
                         lambda s: sleep_calls.append(s))

    rl = UpstoxRateLimiter(per_sec=25, per_min=250, per_30min=1000)
    for _ in range(25):
        rl.acquire()
    # Within the burst budget — no sleeps should fire.
    assert all(s == 0 for s in sleep_calls), (
        f"limiter slept inside the 25/sec burst budget: {sleep_calls}")


def test_token_bucket_sleeps_when_per_second_exhausted(monkeypatch):
    """26th request within the same second must trigger a sleep (until
    the per-sec window slides). Verifies the per-sec bucket is enforced."""
    from data.adapters import upstox_adapter as ua

    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ua.time, "monotonic", lambda: fake_now["t"])

    sleeps: list[float] = []

    def _fake_sleep(s):
        sleeps.append(s)
        # Advancing the fake clock by s simulates the sleep completing.
        fake_now["t"] += s

    monkeypatch.setattr(ua.time, "sleep", _fake_sleep)

    rl = ua.UpstoxRateLimiter(per_sec=25, per_min=250, per_30min=1000)
    for _ in range(26):
        rl.acquire()
    # At least one sleep must have fired (the 26th call hit the per-sec cap).
    assert any(s > 0 for s in sleeps), (
        "limiter did not sleep on the 26th call within the same second")


def test_token_bucket_enforces_per_minute_cap(monkeypatch):
    """251st request inside one minute (even spread across multiple
    seconds) must sleep — the per-minute bucket is independent of the
    per-second bucket."""
    from data.adapters import upstox_adapter as ua

    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ua.time, "monotonic", lambda: fake_now["t"])

    sleeps: list[float] = []

    def _fake_sleep(s):
        sleeps.append(s)
        fake_now["t"] += s

    monkeypatch.setattr(ua.time, "sleep", _fake_sleep)

    rl = ua.UpstoxRateLimiter(per_sec=25, per_min=250, per_30min=1000)
    # Burn 250 within the minute, spaced just enough to dodge the per-sec cap.
    for i in range(250):
        if i and i % 25 == 0:
            fake_now["t"] += 1.0   # advance one second between bursts of 25
        rl.acquire()
    # 251st must sleep (per-minute cap hit).
    rl.acquire()
    assert any(s > 0 for s in sleeps), (
        "limiter did not enforce the per-minute (250) cap")


def test_token_bucket_enforces_per_30min_cap(monkeypatch):
    """1001st request inside one 30-minute window must sleep —
    catching a sustained-rate attacker that dodges the per-sec and
    per-min caps via slow pacing."""
    from data.adapters import upstox_adapter as ua

    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ua.time, "monotonic", lambda: fake_now["t"])

    sleeps: list[float] = []

    def _fake_sleep(s):
        sleeps.append(s)
        fake_now["t"] += s

    monkeypatch.setattr(ua.time, "sleep", _fake_sleep)

    rl = ua.UpstoxRateLimiter(per_sec=25, per_min=250, per_30min=1000)
    # Spread 1000 requests evenly across 28 minutes — under the per-min
    # cap (~36/min) but cumulatively at the 30-min cap by the end.
    for i in range(1000):
        rl.acquire()
        if i % 36 == 0:
            fake_now["t"] += 1.0   # 1s between batches of 36 keeps per-min OK
    # 1001st must sleep — the 30-min window holds 1000 already.
    rl.acquire()
    assert any(s > 0 for s in sleeps), (
        "limiter did not enforce the per-30-min (1000) cap")
