"""Upstox API v2 adapter — stub.

Wire this up once API credentials are set in .env:
    UPSTOX_API_KEY=...
    UPSTOX_API_SECRET=...

Steps to activate:
1. pip install upstox-python-sdk
2. Complete OAuth flow to get access_token (run once, store in .env as UPSTOX_ACCESS_TOKEN)
3. Replace the NotImplementedError below with the real SDK calls.

Upstox historical data endpoint:
    GET /v2/historical-candle/{instrumentKey}/{interval}/{toDate}/{fromDate}

Instrument key format for NSE:  NSE_EQ|INE002A01018  (use instrument master CSV to map symbol → key)
"""

import pandas as pd
from data.adapters.base import DataAdapter
from config import UPSTOX_API_KEY


class UpstoxAdapter(DataAdapter):

    def fetch_ohlcv(self, symbol: str, years: int = 3, resolution: str = "1d") -> pd.DataFrame:
        if not UPSTOX_API_KEY:
            raise EnvironmentError(
                "UPSTOX_API_KEY is not set. Add it to .env or switch DATA_ADAPTER=yfinance."
            )
        # TODO: implement using upstox-python-sdk
        # Example:
        #   from upstox_client import HistoryApi, Configuration
        #   config = Configuration(); config.access_token = os.getenv("UPSTOX_ACCESS_TOKEN")
        #   api = HistoryApi(ApiClient(config))
        #   resp = api.get_historical_candle_data(instrument_key, interval, to_date, from_date)
        #   df = pd.DataFrame(resp.data.candles, columns=["time","open","high","low","close","volume","oi"])
        raise NotImplementedError("Upstox adapter not yet implemented. Set DATA_ADAPTER=yfinance in .env.")

    def is_available(self) -> bool:
        return bool(UPSTOX_API_KEY)
