"""Live market data feed for paper trading.

Polls yfinance for the latest OHLCV bar for each symbol.
NSE market hours: 09:15 – 15:30 IST (UTC+5:30).
"""
import logging
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
NSE_OPEN  = (9, 15)   # 09:15 IST
NSE_CLOSE = (15, 30)  # 15:30 IST


def market_is_open() -> bool:
    """Return True if NSE is currently in session (approximate — no holiday calendar)."""
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Saturday / Sunday
        return False
    open_dt  = now.replace(hour=NSE_OPEN[0],  minute=NSE_OPEN[1],  second=0, microsecond=0)
    close_dt = now.replace(hour=NSE_CLOSE[0], minute=NSE_CLOSE[1], second=0, microsecond=0)
    return open_dt <= now <= close_dt


def time_to_next_open() -> float:
    """Seconds until next NSE open. Returns 0 if market is open."""
    now = datetime.now(IST)
    if market_is_open():
        return 0.0
    # Next open: today if before market open, else tomorrow (skip weekends)
    candidate = now.replace(hour=NSE_OPEN[0], minute=NSE_OPEN[1], second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return (candidate - now).total_seconds()


def fetch_latest_bar(symbol: str, resolution: str = "1d") -> Optional[pd.Series]:
    """Fetch the most recent OHLCV bar for a symbol via yfinance."""
    try:
        period_map = {"1d": "5d", "1h": "7d"}
        period = period_map.get(resolution, "5d")
        df = yf.download(symbol, period=period, interval=resolution,
                         progress=False, auto_adjust=True, actions=False)
        if df.empty:
            logger.warning("No data returned for %s", symbol)
            return None
        df.columns = [c.lower() for c in df.columns]
        bar = df.iloc[-1]
        bar.name = df.index[-1]
        return bar
    except Exception as e:
        logger.warning("fetch_latest_bar(%s): %s", symbol, e)
        return None


def fetch_latest_prices(symbols: list[str]) -> dict:
    """Fetch the latest close price for a list of symbols. Returns {symbol: price}."""
    prices = {}
    for sym in symbols:
        bar = fetch_latest_bar(sym)
        if bar is not None:
            prices[sym] = float(bar["close"])
    return prices


def fetch_updated_ohlcv(symbol: str, resolution: str = "1d",
                        days_back: int = 60) -> Optional[pd.DataFrame]:
    """
    Fetch recent OHLCV bars and upsert into local DB.
    Used to refresh data before generating a signal.
    """
    from data.ingestion import fetch_and_store
    try:
        years = max(1, days_back // 365) if days_back >= 365 else 1
        df = fetch_and_store(symbol, years=years, resolution=resolution)
        return df
    except Exception as e:
        logger.warning("fetch_updated_ohlcv(%s): %s", symbol, e)
        return None


def poll_loop(symbols: list[str], callback, interval_sec: int = 60,
              resolution: str = "1d", run_once: bool = False):
    """
    Continuously poll the market and call callback(symbol, bar) for each symbol.

    Args:
        symbols:      List of ticker symbols to poll.
        callback:     Function(symbol: str, bar: pd.Series) called on each new bar.
        interval_sec: Polling interval in seconds.
        resolution:   Bar resolution ('1d' or '1h').
        run_once:     If True, poll once and return (useful for EOD cron-style runs).
    """
    seen = {}  # symbol → last bar timestamp

    logger.info("Poll loop started — %d symbols, interval=%ds", len(symbols), interval_sec)

    while True:
        if not market_is_open() and not run_once:
            wait = time_to_next_open()
            if wait > 60:
                logger.info("Market closed — sleeping %.0f minutes until next open",
                            wait / 60)
                time.sleep(min(wait - 30, 3600))
                continue

        for sym in symbols:
            bar = fetch_latest_bar(sym, resolution)
            if bar is None:
                continue
            ts = str(bar.name)
            if seen.get(sym) != ts:
                seen[sym] = ts
                try:
                    callback(sym, bar)
                except Exception as e:
                    logger.error("callback error for %s: %s", sym, e)

        if run_once:
            break

        time.sleep(interval_sec)
