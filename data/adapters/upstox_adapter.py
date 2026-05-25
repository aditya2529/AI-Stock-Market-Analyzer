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

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import dotenv_values

from data.adapters.base import DataAdapter
from live_trading import symbol_map


UPSTOX_BASE_URL = "https://api.upstox.com/v2"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"
_TIMEOUT_SECONDS = 30


# Upstox historical-candle interval keys.
_INTERVAL_MAP = {
    "1d":   "day",
    "day":  "day",
    "1w":   "week",
    "week": "week",
    "1mo":  "month",
    "month": "month",
}


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


class UpstoxAdapter(DataAdapter):
    """Read-only OHLCV fetcher via Upstox v2 ``/historical-candle``."""

    def fetch_ohlcv(self, symbol: str, years: int = 3,
                     resolution: str = "1d") -> pd.DataFrame:
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

    def is_available(self) -> bool:
        """True iff an active access token is present in .env."""
        return bool(_active_access_token())
