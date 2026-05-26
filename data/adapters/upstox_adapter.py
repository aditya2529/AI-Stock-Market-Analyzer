"""Upstox API v2 adapter — OHLCV fetch.

OHLCV fetches via this adapter hit REAL production market data regardless
of ``UPSTOX_ENV`` (per Upstox 2026-05-24 addendum: only order endpoints
are sandbox-simulated). This means the adapter is safe to call from
sandbox-keyed sessions — the response is the same exchange data the
paper engine sees through yfinance.

NOT wired into the paper-engine feed. The paper flow stays on yfinance
(``DATA_ADAPTER=yfinance``). This adapter exists so future code paths
can opt in to Upstox-sourced data WITHOUT touching the paper flow.

No kill-switch gating: OHLCV is read-only and doesn't move money. The
kill switch fires for ORDER endpoints only — handled by
``live_trading.upstox_client``.

Active token resolution mirrors ``live_trading.upstox_client``: read
``UPSTOX_ENV`` from .env on every call, then pick the matching
``UPSTOX_{ENV}_ACCESS_TOKEN``. No dependency on the legacy
``config.UPSTOX_API_KEY`` single-key model.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import dotenv_values

from data.adapters.base import DataAdapter
from live_trading import symbol_map


UPSTOX_BASE_URL = "https://api.upstox.com/v2"
UPSTOX_BASE_URL_V3 = "https://api.upstox.com/v3"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"
_TIMEOUT_SECONDS = 30

# R8 — v3 5m chunking. Upstox v3 rejects "Invalid date range" on
# requests where the from/to span is too wide for the intraday unit.
# Empirically Upstox accepts a 5-day window for the minutes unit at
# the sandbox base URL; longer windows surface UDAPI1148. Using 5
# days as a conservative default; for a 1-year backfill this means
# ~73 requests per symbol, well within the 25/sec rate-limit budget.
_V3_INTRADAY_CHUNK_DAYS = 5

# Upstox historical-candle interval keys (v2, day/week/month).
_INTERVAL_MAP = {
    "1d":   "day",
    "day":  "day",
    "1w":   "week",
    "week": "week",
    "1mo":  "month",
    "month": "month",
}

# R8 — intraday resolution -> integer minutes (for the v3 path).
# Keeps both yfinance-style ("5m") and numeric-only ("5") accepted so
# the caller doesn't have to remember which adapter expects what.
_INTRADAY_MINUTES_MAP = {
    "1m": 1, "1": 1,
    "5m": 5, "5": 5,
    "15m": 15, "15": 15,
    "30m": 30, "30": 30,
    "60m": 60, "60": 60, "1h": 60,
}


def _resolution_to_minutes(resolution: str) -> int | None:
    """Return integer minutes for a yfinance-style intraday resolution
    string, or None if the resolution is daily/weekly/monthly (i.e.
    handled by the v2 path)."""
    if resolution is None:
        return None
    return _INTRADAY_MINUTES_MAP.get(resolution.lower())


def _active_access_token(env_path: str | None = None) -> str | None:
    """Return the active env's access token, or None if missing/invalid.

    NOT gated by kill_switch — OHLCV is data, not orders. A missing token
    raises a clear EnvironmentError at the fetch_ohlcv call site so the
    caller knows to set up .env (or switch back to yfinance).
    """
    env = dotenv_values(str(env_path or _DEFAULT_ENV_PATH))
    upstox_env = env.get("UPSTOX_ENV")
    if upstox_env not in ("sandbox", "prod"):
        return None
    token = env.get(f"UPSTOX_{upstox_env.upper()}_ACCESS_TOKEN")
    return token.strip() if token and token.strip() else None


class UpstoxRateLimiter:
    """Token-bucket triad for the Upstox API limits (per-sec / per-min /
    per-30min) — enforced CONCURRENTLY.

    Why three buckets, not one: a 200-symbol backfill at 25 req/sec
    finishes the per-second budget in ~80 sec but cumulatively touches
    the 30-min cap as soon as ops re-runs the backfill (incremental
    refresh, R9 retrain prep, periodic re-backfills). A single-bucket
    limiter would fly through 25/sec for 30 min straight and crash
    against the 1000/30min cap with no warning.

    Implementation — a deque of timestamps per bucket. On acquire:
      1. For each bucket, drop timestamps older than its window.
      2. If the deque is at capacity, sleep until the oldest entry
         falls off (the smallest wait across all three).
      3. Re-check (after sleep, another bucket might also need a wait).
      4. Once all three buckets have room, append the current monotonic
         time to each deque and return.

    Time source is ``time.monotonic`` (steady clock, immune to NTP
    adjustments). All three windows use the same clock so they cannot
    drift relative to each other.
    """

    # Defaults from Upstox public docs (May 2026):
    #   25 req/sec, 250 req/min, 1000 req/30 min.
    # Tests pass these explicitly to keep the rate limits visible at
    # the test call site.
    def __init__(self, per_sec: int = 25, per_min: int = 250,
                  per_30min: int = 1000) -> None:
        self._buckets = [
            (per_sec, 1.0, deque()),         # (capacity, window_secs, ts_deque)
            (per_min, 60.0, deque()),
            (per_30min, 30.0 * 60.0, deque()),
        ]

    def acquire(self) -> None:
        """Block until all three buckets have headroom, then record
        this acquisition in all three. Returns nothing — caller just
        proceeds with the request after this returns."""
        while True:
            now = time.monotonic()
            longest_wait = 0.0
            for cap, window, dq in self._buckets:
                # Drop timestamps outside this bucket's window.
                while dq and now - dq[0] >= window:
                    dq.popleft()
                if len(dq) >= cap:
                    # Wait until the oldest entry falls off the window.
                    wait_secs = window - (now - dq[0])
                    if wait_secs > longest_wait:
                        longest_wait = wait_secs
            if longest_wait <= 0:
                # All three have room — record + go.
                now = time.monotonic()
                for _, _, dq in self._buckets:
                    dq.append(now)
                return
            time.sleep(longest_wait)


class UpstoxAdapter(DataAdapter):
    """Read-only OHLCV fetcher via Upstox v2 ``/historical-candle``
    (day/week/month) AND v3 ``/historical-candle/.../minutes/N/...``
    (R8: intraday 5m / 15m / 30m / 60m support).
    """

    # Class-level limiter shared across all instance method calls so a
    # multi-symbol backfill loop respects the per-30min cap across
    # symbols, not just within one. Process-local — multi-process
    # backfill would need an IPC-backed limiter (out of scope today).
    _rate_limiter = UpstoxRateLimiter()

    def fetch_ohlcv(self, symbol: str, years: int = 3,
                     resolution: str = "1d") -> pd.DataFrame:
        # NOTE: this v2 day/week/month path is INTENTIONALLY unchanged
        # by R8. It still raises ValueError on intraday resolutions
        # (5m / 15m / 30m / 60m / 1m) — the P42 test
        # test_fetch_ohlcv_raises_on_unsupported_resolution depends
        # on that contract. R8 intraday dispatch happens at the
        # ingestion layer (data/ingestion.py:fetch_and_store), not
        # here, so the P42-era adapter surface stays bit-for-bit.
        # Operators who want 5m via Upstox call get_historical_intraday()
        # directly, or use `main.py fetch --source upstox --resolution 5m`
        # which routes via fetch_and_store.
        token = _active_access_token()
        if not token:
            raise EnvironmentError(
                "No active Upstox access token in .env. Set UPSTOX_ENV + "
                "the matching UPSTOX_{ENV}_ACCESS_TOKEN, or switch "
                "DATA_ADAPTER=yfinance for the paper flow."
            )

        try:
            instrument_key = symbol_map.lookup(symbol)
        except KeyError as exc:
            raise EnvironmentError(
                f"Symbol {symbol!r} not in live_trading.symbol_map. "
                f"Add the yfinance→Upstox mapping there to enable "
                f"Upstox-sourced OHLCV."
            ) from exc

        interval = _INTERVAL_MAP.get(resolution)
        if interval is None:
            raise ValueError(
                f"Unsupported resolution {resolution!r}. "
                f"Supported: {sorted(set(_INTERVAL_MAP))}"
            )

        to_date = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now()
                      - timedelta(days=int(years * 365.25))).strftime("%Y-%m-%d")
        url = (f"{UPSTOX_BASE_URL}/historical-candle/"
               f"{instrument_key}/{interval}/{to_date}/{from_date}")
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Upstox OHLCV fetch failed: HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )

        payload = resp.json()
        candles = (payload.get("data") or {}).get("candles") or []
        if not candles:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"]
            )

        # Upstox row order: [timestamp, open, high, low, close, volume, open_interest]
        df = pd.DataFrame(candles, columns=[
            "timestamp", "open", "high", "low", "close", "volume", "oi",
        ])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp").sort_index()
        df = df.drop(columns=["oi"])  # open-interest unused downstream
        return df

    def get_historical_intraday(self, symbol: str,
                                  interval_minutes: int,
                                  from_date: str,
                                  to_date: str) -> pd.DataFrame:
        """R8 — Fetch intraday OHLCV bars via Upstox v3.

        Args:
            symbol: yfinance ticker (e.g. "TCS.NS"). Resolved to an
                Upstox instrument key via the dynamic master-CSV
                cache in ``data.adapters.upstox_instruments``. The
                P42 static dict in ``live_trading.symbol_map`` is
                NOT touched.
            interval_minutes: 1 / 5 / 15 / 30 / 60. Upstox v3
                requires the unit (``minutes``) plus the integer
                interval in the path. Minute data is available from
                January 2022 (per Upstox docs).
            from_date: ``YYYY-MM-DD``, inclusive.
            to_date: ``YYYY-MM-DD``, inclusive.

        Returns:
            yfinance-shape DataFrame:
                index = DatetimeIndex, name='time', tz-naive
                columns = [open, high, low, close, volume]
            Empty DataFrame if no candles in range.

        Raises:
            EnvironmentError: no UPSTOX_{ENV}_ACCESS_TOKEN in .env.
            KeyError: symbol not found in Upstox instrument master.
            RuntimeError: HTTP non-200 from Upstox (includes status
                code + body excerpt for triage).
        """
        from data.adapters.upstox_instruments import lookup_instrument_key

        token = _active_access_token()
        if not token:
            raise EnvironmentError(
                "No active Upstox access token in .env. Set UPSTOX_ENV + "
                "the matching UPSTOX_{ENV}_ACCESS_TOKEN, or switch "
                "--source yfinance for the paper flow."
            )

        instrument_key = lookup_instrument_key(symbol)

        # Chunk the date range into 30-day windows. The v3 endpoint
        # response is capped (~5000 candles); a 30-day chunk at 5m =
        # ~1650 bars, leaving headroom for any extra fillers + multi-
        # session-day artefacts. Larger intervals (15m / 60m) would
        # also fit, so 30 days is the single-knob default.
        from_ts = pd.Timestamp(from_date)
        to_ts = pd.Timestamp(to_date)
        if from_ts > to_ts:
            raise ValueError(
                f"from_date {from_date!r} > to_date {to_date!r}")

        chunks: list[pd.DataFrame] = []
        chunk_from = from_ts
        while chunk_from <= to_ts:
            chunk_to = min(chunk_from + pd.Timedelta(days=_V3_INTRADAY_CHUNK_DAYS - 1),
                            to_ts)
            self._rate_limiter.acquire()
            df_chunk = self._fetch_v3_intraday_chunk(
                instrument_key=instrument_key,
                interval_minutes=int(interval_minutes),
                token=token,
                from_date=chunk_from.strftime("%Y-%m-%d"),
                to_date=chunk_to.strftime("%Y-%m-%d"),
            )
            if not df_chunk.empty:
                chunks.append(df_chunk)
            chunk_from = chunk_to + pd.Timedelta(days=1)

        if not chunks:
            return pd.DataFrame(
                columns=["time", "open", "high", "low", "close", "volume"])

        combined = pd.concat(chunks, ignore_index=True).sort_values("time")
        # De-dup any boundary candles that fell into two chunks
        # (shouldn't happen with the inclusive math above, but cheap
        # belt-and-suspenders — matches P44's dedup spirit).
        combined = combined.drop_duplicates(subset=["time"], keep="first")
        return combined.reset_index(drop=True)

    def _fetch_v3_intraday_chunk(self, instrument_key: str,
                                    interval_minutes: int,
                                    token: str,
                                    from_date: str,
                                    to_date: str) -> pd.DataFrame:
        """Single Upstox v3 historical-candle GET. Returns a yfinance-
        shape DataFrame (tz-naive DatetimeIndex named 'time', columns
        [open, high, low, close, volume]).

        Upstox v3 URL quirk preserved: to_date precedes from_date in
        the path, matching the v2 endpoint's ordering — operators
        toggling between v2 day/week/month and v3 minute paths see
        consistent ordering.
        """
        url = (f"{UPSTOX_BASE_URL_V3}/historical-candle/"
               f"{instrument_key}/minutes/{interval_minutes}/"
               f"{to_date}/{from_date}")
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Upstox v3 intraday fetch failed: HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Upstox v3 intraday fetch returned non-JSON body: "
                f"{resp.text[:300]}"
            ) from exc

        candles = (payload.get("data") or {}).get("candles") or []
        if not candles:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"])

        # Upstox row order: [timestamp, open, high, low, close, volume, open_interest]
        df = pd.DataFrame(candles, columns=[
            "time", "open", "high", "low", "close", "volume", "oi",
        ])
        df = df.drop(columns=["oi"])   # open_interest unused downstream
        # Match the yfinance adapter return contract: `time` is a
        # column (not index), tz-naive, default RangeIndex. The DB
        # WRITE path (validator + upsert_ohlcv) reads `df["time"]`
        # by name — the DB READ path (load_ohlcv) then promotes it
        # back to a DatetimeIndex named 'time'. Returning column-time
        # here keeps the contract uniform across adapters.
        df["time"] = pd.to_datetime(df["time"]).dt.tz_localize(None)
        # Coerce numeric columns — Upstox returns numbers but defensive
        # cast keeps dtype stable across pandas versions.
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("time").reset_index(drop=True)
        return df

    def is_available(self) -> bool:
        """True iff an active access token is present in .env."""
        return bool(_active_access_token())
