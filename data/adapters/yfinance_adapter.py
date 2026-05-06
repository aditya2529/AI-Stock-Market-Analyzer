import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from data.adapters.base import DataAdapter


_RESOLUTION_MAP = {
    "1d": "1d",
    "1h": "1h",
    "5m": "5m",
}

# yfinance limits on intraday history
_MAX_DAYS = {
    "1h": 730,
    "5m": 60,
    "1d": 99999,
}


class YFinanceAdapter(DataAdapter):

    def fetch_ohlcv(self, symbol: str, years: int = 3, resolution: str = "1d") -> pd.DataFrame:
        interval = _RESOLUTION_MAP.get(resolution, "1d")
        end = datetime.utcnow()
        max_days = _MAX_DAYS.get(interval, 99999)
        delta_days = min(years * 365, max_days)
        start = end - timedelta(days=delta_days)

        ticker = yf.Ticker(symbol)
        raw = ticker.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"), interval=interval)

        if raw.empty:
            raise ValueError(f"yfinance returned no data for {symbol!r} ({interval})")

        raw = raw.reset_index()
        # Normalise column names (yfinance uses 'Datetime' for intraday, 'Date' for daily)
        time_col = "Datetime" if "Datetime" in raw.columns else "Date"
        df = raw[[time_col, "Open", "High", "Low", "Close", "Volume"]].copy()
        df.columns = ["time", "open", "high", "low", "close", "volume"]
        df["time"] = pd.to_datetime(df["time"]).dt.tz_localize(None)
        df = df.dropna(subset=["open", "close"])
        df = df.reset_index(drop=True)
        return df

    def is_available(self) -> bool:
        try:
            t = yf.Ticker("RELIANCE.NS")
            info = t.fast_info
            return info.last_price is not None
        except Exception:
            return False
