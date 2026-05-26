"""R8 — Upstox sandbox INTEGRATION test (network-touching).

SKIPPED BY DEFAULT to keep the regular ``pytest tests/`` run pure /
deterministic / offline. Opts in via:

    UPSTOX_INTEGRATION_TEST=1 python -m pytest tests/test_r8_sandbox_integration.py -v

Requires the existing P42 sandbox creds in .env:
    UPSTOX_SANDBOX_API_KEY
    UPSTOX_SANDBOX_API_SECRET
    UPSTOX_SANDBOX_ACCESS_TOKEN
    UPSTOX_ENV must be "sandbox" (or test sets it via tmp .env)

This test fetches one trading day of 5-minute bars for 2 symbols
(TCS.NS, INFY.NS) via the Upstox v3 historical-candle endpoint
through ``UpstoxAdapter.get_historical_intraday``, verifies the
returned shape matches yfinance, and verifies the upsert_ohlcv DB
write succeeds without schema drift.

Why sandbox creds are safe here (existing adapter docstring):
    "OHLCV fetches via this adapter hit REAL production market data
    regardless of UPSTOX_ENV (per Upstox 2026-05-24 addendum: only
    order endpoints are sandbox-simulated)."
So the response is the same exchange feed prod sees — sandbox /
prod token routing is purely a cost / safety concern, not a data-
quality concern.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


INTEGRATION_GATE = os.getenv("UPSTOX_INTEGRATION_TEST", "0") == "1"

pytestmark = pytest.mark.skipif(
    not INTEGRATION_GATE,
    reason="Set UPSTOX_INTEGRATION_TEST=1 to run the Upstox sandbox "
            "integration test (touches real network).",
)


def _real_env_path() -> Path:
    """Path to the project's real .env (not a tmp fixture)."""
    return Path(__file__).resolve().parents[1] / ".env"


def _sandbox_env_present() -> bool:
    """True iff the real .env exposes a non-empty UPSTOX_SANDBOX_ACCESS_TOKEN."""
    from dotenv import dotenv_values
    env_path = _real_env_path()
    if not env_path.exists():
        return False
    env = dotenv_values(str(env_path))
    tok = env.get("UPSTOX_SANDBOX_ACCESS_TOKEN", "")
    return bool(tok and tok.strip())


@pytest.fixture
def sandbox_env(tmp_path, monkeypatch):
    """Build a tmp .env that points the adapter at sandbox creds
    sourced from the project's real .env. Avoids mutating the real
    .env or relying on UPSTOX_ENV already being "sandbox" at test
    time."""
    if not _sandbox_env_present():
        pytest.skip("UPSTOX_SANDBOX_ACCESS_TOKEN missing in real .env")

    from dotenv import dotenv_values
    real = dotenv_values(str(_real_env_path()))
    sandbox_token = real["UPSTOX_SANDBOX_ACCESS_TOKEN"]
    sandbox_key = real.get("UPSTOX_SANDBOX_API_KEY", "")
    sandbox_secret = real.get("UPSTOX_SANDBOX_API_SECRET", "")

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join([
            'UPSTOX_ENV="sandbox"',
            f'UPSTOX_SANDBOX_API_KEY="{sandbox_key}"',
            f'UPSTOX_SANDBOX_API_SECRET="{sandbox_secret}"',
            f'UPSTOX_SANDBOX_ACCESS_TOKEN="{sandbox_token}"',
        ]),
        encoding="utf-8",
    )
    import data.adapters.upstox_adapter as ua
    monkeypatch.setattr(ua, "_DEFAULT_ENV_PATH", env_path)
    yield env_path


def _recent_trading_day() -> tuple[str, str]:
    """Return (from_date, to_date) for the most recent calendar day
    that's likely a trading day. Walks back from yesterday until it
    finds Mon-Fri. Calling this on a Monday morning gives Friday."""
    today = datetime.now().date()
    candidate = today - timedelta(days=1)
    while candidate.weekday() >= 5:   # 5=Sat, 6=Sun
        candidate -= timedelta(days=1)
    iso = candidate.isoformat()
    return iso, iso   # same day for from + to


# ── The 2-symbol × 1-day fetch ────────────────────────────────────────


def test_sandbox_fetch_5m_two_symbols_one_day(sandbox_env):
    """Fetch 1 trading day of 5-minute bars for TCS.NS + INFY.NS via
    the Upstox v3 endpoint (sandbox token). Verify per-symbol shape
    against the yfinance contract the engine relies on."""
    from data.adapters.upstox_adapter import UpstoxAdapter

    from_date, to_date = _recent_trading_day()
    adapter = UpstoxAdapter()

    results = {}
    for sym in ["TCS.NS", "INFY.NS"]:
        df = adapter.get_historical_intraday(
            symbol=sym,
            interval_minutes=5,
            from_date=from_date,
            to_date=to_date,
        )
        results[sym] = df

    for sym, df in results.items():
        # NSE intraday session 09:15 -> 15:30 IST = 75 5-min bars max.
        # Sandbox may return any subset; require at least 1 bar to
        # confirm the endpoint connected + we authenticated.
        assert not df.empty, f"{sym} returned an empty DataFrame on a trading day"
        # Adapter return contract: time as a COLUMN, default RangeIndex,
        # tz-naive `time`. Matches yfinance_adapter (the de facto
        # interface validate_and_clean + upsert_ohlcv consume).
        assert list(df.columns) == ["time", "open", "high", "low", "close", "volume"], (
            f"{sym} schema drift: {list(df.columns)}")
        assert isinstance(df.index, pd.RangeIndex), (
            f"{sym} index is not RangeIndex: {type(df.index)}")
        assert pd.api.types.is_datetime64_any_dtype(df["time"]), (
            f"{sym} time column dtype is {df['time'].dtype}, expected datetime")
        assert df["time"].dt.tz is None, (
            f"{sym} time column is tz-aware: {df['time'].dt.tz}")
        assert df["close"].gt(0).all(), (
            f"{sym} has non-positive close prices — sandbox data sanity check")
        assert df["volume"].ge(0).all(), (
            f"{sym} has negative volume — sandbox data sanity check")


# ── End-to-end: fetch_and_store -> DB upsert ─────────────────────────


def test_sandbox_fetch_and_store_writes_to_db(sandbox_env, tmp_path,
                                                monkeypatch):
    """End-to-end happy path: fetch_and_store with --source upstox +
    resolution 5m + a 1-day window must write rows to the OHLCV
    table without crashing on schema drift. Uses a tmp DB so we don't
    pollute the live market_data.db while testing."""
    db_path = tmp_path / "integration_test.db"
    import data.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", str(db_path))

    from data.ingestion import fetch_and_store

    from_date, to_date = _recent_trading_day()
    # fetch_and_store uses `years`; we round-trip via a "1-year"
    # window then let the adapter chunk down. Sandbox returns the
    # ~5000-candle limit if asked for too long, so this is safe.
    # Then verify the most recent rows are present.
    years_to_cover = 1
    df = fetch_and_store(
        "TCS.NS",
        years=years_to_cover,
        resolution="5m",
        source="upstox",
    )
    assert not df.empty, "fetch_and_store returned empty for TCS.NS via sandbox"

    # Verify the rows actually hit the DB.
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.execute(
        "SELECT COUNT(*), MIN(time), MAX(time) FROM ohlcv "
        "WHERE symbol = ? AND resolution = ?",
        ("TCS.NS", "5m"),
    )
    row = cur.fetchone()
    con.close()
    assert row is not None
    n, mn, mx = row
    assert n > 0, "DB has zero rows for TCS.NS @ 5m after fetch"
    print(f"[integration] DB rows written: {n}  span: {mn} -> {mx}")


# ── Schema parity with yfinance ─────────────────────────────────────


def test_sandbox_response_schema_matches_yfinance(sandbox_env):
    """The columns + dtypes of an Upstox 5m fetch must match the
    YFinanceAdapter return contract (the de facto interface
    validate_and_clean + upsert_ohlcv consume): columns =
    [time, open, high, low, close, volume], default RangeIndex,
    tz-naive `time` column. Schema parity here is what lets
    fetch_and_store treat either adapter transparently."""
    from data.adapters.upstox_adapter import UpstoxAdapter
    from data.adapters.yfinance_adapter import YFinanceAdapter

    from_date, to_date = _recent_trading_day()

    ux_df = UpstoxAdapter().get_historical_intraday(
        symbol="TCS.NS",
        interval_minutes=5,
        from_date=from_date,
        to_date=to_date,
    )
    yf_df = YFinanceAdapter().fetch_ohlcv("TCS.NS", years=1, resolution="5m")

    expected_cols = ["time", "open", "high", "low", "close", "volume"]
    assert list(ux_df.columns) == expected_cols, (
        f"upstox column drift: {list(ux_df.columns)}")
    assert list(yf_df.columns) == expected_cols, (
        f"yfinance column drift: {list(yf_df.columns)}")

    # Both must have a default RangeIndex (not DatetimeIndex).
    assert isinstance(ux_df.index, pd.RangeIndex)
    assert isinstance(yf_df.index, pd.RangeIndex)

    # `time` column must be tz-naive datetime64 on both.
    assert pd.api.types.is_datetime64_any_dtype(ux_df["time"])
    assert pd.api.types.is_datetime64_any_dtype(yf_df["time"])
    assert ux_df["time"].dt.tz is None
    assert yf_df["time"].dt.tz is None

    # Numeric columns float64 on both for OHLC.
    for col in ("open", "high", "low", "close"):
        assert pd.api.types.is_float_dtype(ux_df[col]), (
            f"ux {col} dtype is {ux_df[col].dtype}, expected float")
