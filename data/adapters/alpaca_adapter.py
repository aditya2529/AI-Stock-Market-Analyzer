"""Alpaca Markets Data API v2 adapter — US stocks (NYSE / NASDAQ).

Free tier: IEX feed (15-min delayed).
Premium tier: SIP feed (real-time).

Credentials: set ALPACA_API_KEY + ALPACA_API_SECRET in .env.
Paper trading key works for historical data too.

Docs: https://docs.alpaca.markets/reference/stockbars
"""
import logging
import urllib.request
import urllib.parse
import json
from datetime import datetime, timezone, timedelta

import pandas as pd

from config import ALPACA_API_KEY, ALPACA_API_SECRET
from data.adapters.base import DataAdapter

logger = logging.getLogger(__name__)

_BASE = "https://data.alpaca.markets/v2"
_RESOLUTION_MAP = {
    "1d": "1Day",
    "1h": "1Hour",
    "15m": "15Min",
    "5m": "5Min",
    "1m": "1Min",
}


class AlpacaAdapter(DataAdapter):
    """Fetch US equity OHLCV from Alpaca Markets Data API v2."""

    def is_available(self) -> bool:
        return bool(ALPACA_API_KEY and ALPACA_API_SECRET)

    def _get(self, path: str, params: dict) -> dict:
        url = f"{_BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def fetch_ohlcv(self, symbol: str, years: int = 3,
                    resolution: str = "1d") -> pd.DataFrame:
        if not self.is_available():
            raise RuntimeError(
                "Alpaca API credentials not set. "
                "Add ALPACA_API_KEY and ALPACA_API_SECRET to .env"
            )

        tf = _RESOLUTION_MAP.get(resolution)
        if tf is None:
            raise ValueError(f"Unsupported resolution for Alpaca: {resolution}")

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=years * 365)

        bars = []
        page_token = None

        while True:
            params = {
                "timeframe": tf,
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": 10000,
                "adjustment": "split",   # corporate action adjusted
                "feed": "iex",           # free tier; change to "sip" with premium
            }
            if page_token:
                params["page_token"] = page_token

            data = self._get(f"/stocks/{symbol}/bars", params)
            chunk = data.get("bars", [])
            bars.extend(chunk)
            page_token = data.get("next_page_token")
            if not page_token:
                break

        if not bars:
            raise ValueError(f"No data returned from Alpaca for {symbol}")

        df = pd.DataFrame(bars)
        df = df.rename(columns={
            "t": "time", "o": "open", "h": "high",
            "l": "low",  "c": "close", "v": "volume",
        })
        df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
        df = df[["time", "open", "high", "low", "close", "volume"]].sort_values("time")
        df = df.reset_index(drop=True)
        logger.info("Alpaca: fetched %d bars for %s (%s)", len(df), symbol, resolution)
        return df

    def fetch_recent_bars(self, symbol: str, n_bars: int = 200,
                          resolution: str = "5m") -> "pd.DataFrame":
        """Fetch the most recent N bars — used by the intraday engine each tick."""
        if not self.is_available():
            raise RuntimeError("Alpaca API credentials not set.")

        tf = _RESOLUTION_MAP.get(resolution)
        if tf is None:
            raise ValueError(f"Unsupported resolution for Alpaca: {resolution}")

        end = datetime.now(timezone.utc)
        # Go back far enough to guarantee n_bars (markets closed on weekends)
        start = end - timedelta(days=7)

        params = {
            "timeframe": tf,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": n_bars,
            "adjustment": "split",
            "feed": "iex",
            "sort": "asc",
        }

        data = self._get(f"/stocks/{symbol}/bars", params)
        bars = data.get("bars", [])
        if not bars:
            return pd.DataFrame()

        df = pd.DataFrame(bars)
        df = df.rename(columns={
            "t": "time", "o": "open", "h": "high",
            "l": "low",  "c": "close", "v": "volume",
        })
        df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
        df = df[["time", "open", "high", "low", "close", "volume"]].sort_values("time")
        df = df.set_index("time")
        return df.tail(n_bars)
