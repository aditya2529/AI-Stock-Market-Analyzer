"""Binance Public REST API adapter — Crypto spot (USDT pairs).

No API key required for historical klines — all public data.

Docs: https://binance-docs.github.io/apidocs/spot/en/#kline-candlestick-data
"""
import logging
import urllib.request
import urllib.parse
import json
from datetime import datetime, timezone, timedelta

import pandas as pd

from data.adapters.base import DataAdapter

logger = logging.getLogger(__name__)

_BASE = "https://api.binance.com/api/v3/klines"
_RESOLUTION_MAP = {
    "1d": "1d",
    "1h": "1h",
    "15m": "15m",
    "5m":  "5m",
    "1m":  "1m",
}
_MAX_BARS = 1000   # Binance hard limit per request


def _to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class BinanceAdapter(DataAdapter):
    """Fetch crypto OHLCV from Binance public klines endpoint (no auth needed)."""

    def is_available(self) -> bool:
        return True  # public endpoint, always available

    def _fetch_chunk(self, symbol: str, interval: str,
                     start_ms: int, end_ms: int) -> list:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": _MAX_BARS,
        }
        url = f"{_BASE}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def fetch_ohlcv(self, symbol: str, years: int = 3,
                    resolution: str = "1d") -> pd.DataFrame:
        interval = _RESOLUTION_MAP.get(resolution)
        if interval is None:
            raise ValueError(f"Unsupported resolution for Binance: {resolution}")

        # Normalise symbol — accept BTC, BTC-USDT, BTCUSDT
        sym = symbol.replace("-", "").upper()
        if not sym.endswith("USDT") and not sym.endswith("BTC") and not sym.endswith("ETH"):
            sym = sym + "USDT"

        end_dt   = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=years * 365)
        start_ms = _to_ms(start_dt)
        end_ms   = _to_ms(end_dt)

        all_rows = []
        cursor = start_ms

        while cursor < end_ms:
            chunk = self._fetch_chunk(sym, interval, cursor, end_ms)
            if not chunk:
                break
            all_rows.extend(chunk)
            # last candle open time + 1 ms
            cursor = chunk[-1][0] + 1
            if len(chunk) < _MAX_BARS:
                break

        if not all_rows:
            raise ValueError(f"No data returned from Binance for {sym}")

        # Kline format: [open_time, open, high, low, close, volume, close_time, ...]
        df = pd.DataFrame(all_rows, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "ignore",
        ])
        df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.tz_localize(None)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        df = df[["time", "open", "high", "low", "close", "volume"]].sort_values("time")
        df = df.drop_duplicates("time").reset_index(drop=True)
        logger.info("Binance: fetched %d bars for %s (%s)", len(df), sym, resolution)
        return df
