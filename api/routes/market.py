"""Market data and regime endpoints."""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Query
import time

router = APIRouter(prefix="/api/market", tags=["market"])

# Simple in-process cache — refresh every 60s
_indices_cache: dict = {}
_indices_ts: float = 0.0

# US stocks snapshot cache
_us_stocks_cache: list = []
_us_stocks_ts: float = 0.0

INDICES = {
    "NIFTY 50":   "^NSEI",
    "SENSEX":     "^BSESN",
    "BANK NIFTY": "^NSEBANK",
    "NIFTY IT":   "^CNXIT",
    "INDIA VIX":  "^INDIAVIX",
    "S&P 500":    "^GSPC",
    "NASDAQ":     "^IXIC",
    "DOW JONES":  "^DJI",
}

@router.get("/indices")
def get_indices():
    """Live index quotes — cached 60s to avoid hammering yfinance.

    Uses a 5d lookup window so that if today's bar is missing for a
    slow-publishing index (BSE often lags NSE by 5-15 min after market
    open), we still surface yesterday's close with an `is_stale=True` tag
    rather than silently dropping the symbol from the response.
    """
    global _indices_cache, _indices_ts
    if time.time() - _indices_ts < 60 and _indices_cache:
        return _indices_cache

    import yfinance as yf
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(IST).date()

    tickers = list(INDICES.values())
    try:
        data = yf.download(tickers, period="5d", interval="1d",
                           progress=False, auto_adjust=True, group_by="ticker")
        result = []
        for name, sym in INDICES.items():
            try:
                if isinstance(data.columns, __import__('pandas').MultiIndex):
                    df = data[sym].dropna()
                else:
                    df = data.dropna()
                if len(df) < 2:
                    continue
                curr  = float(df["Close"].iloc[-1])
                prev  = float(df["Close"].iloc[-2])
                chg   = curr - prev
                chgPct = chg / prev
                # Tag stale when the last bar isn't today (yfinance hasn't
                # published today's bar yet for this index — common for BSE)
                last_bar_date = df.index[-1].date() if hasattr(df.index[-1], "date") else None
                is_stale = last_bar_date is not None and last_bar_date != today_ist
                result.append({
                    "name": name, "symbol": sym,
                    "price": round(curr, 2),
                    "change": round(chg, 2),
                    "change_pct": round(chgPct, 4),
                    "is_stale": is_stale,
                    "last_bar_date": last_bar_date.isoformat() if last_bar_date else None,
                })
            except Exception:
                continue
        _indices_cache = result
        _indices_ts = time.time()
        return result
    except Exception as e:
        return _indices_cache or []


@router.get("/us/stocks")
def get_us_stocks():
    """Live US stock quotes via Alpaca IEX feed — cached 60s."""
    global _us_stocks_cache, _us_stocks_ts
    if time.time() - _us_stocks_ts < 60 and _us_stocks_cache:
        return _us_stocks_cache

    from config import DEFAULT_US_SYMBOLS, ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_BASE_URL
    if not (ALPACA_API_KEY and ALPACA_API_SECRET):
        return []

    import urllib.request, urllib.parse, json as _json
    symbols_str = ",".join(DEFAULT_US_SYMBOLS)
    url = f"https://data.alpaca.markets/v2/stocks/bars/latest?symbols={urllib.parse.quote(symbols_str)}&feed=iex"
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        bars = data.get("bars", {})

        # Previous close — Alpaca's batch bars endpoint is unreliable (drops symbols
        # silently in batch responses), so use yfinance per-symbol which works reliably.
        import yfinance as yf
        prev_closes = {}
        for sym in DEFAULT_US_SYMBOLS:
            try:
                hist = yf.Ticker(sym).history(period="5d", interval="1d", auto_adjust=False)
                if len(hist) >= 2:
                    prev_closes[sym] = float(hist["Close"].iloc[-2])
            except Exception:
                pass

        result = []
        for sym in DEFAULT_US_SYMBOLS:
            bar = bars.get(sym)
            if not bar:
                continue
            curr = float(bar.get("c", 0))
            prev = prev_closes.get(sym, curr)
            chg = curr - prev
            chg_pct = chg / prev if prev else 0
            result.append({
                "symbol": sym,
                "price": round(curr, 2),
                "change": round(chg, 2),
                "change_pct": round(chg_pct, 4),
            })
        _us_stocks_cache = result
        _us_stocks_ts = time.time()
        return result
    except Exception as e:
        return _us_stocks_cache or []


@router.get("/symbols")
def list_symbols():
    """All symbols stored in the local DB."""
    from data.database import list_symbols
    return list_symbols()


@router.get("/regime/{symbol}")
def get_regime(symbol: str):
    """Current HMM regime for a symbol."""
    from data.ingestion import get_ohlcv
    from features.engineer import engineer_features
    from models.ensemble import Ensemble
    try:
        ens = Ensemble.load()
        df = get_ohlcv(symbol)
        featured = engineer_features(df)
        result = ens.predict_with_confidence(featured)
        latest = result.iloc[-1]
        history = result["regime"].value_counts().to_dict()
        return {
            "symbol": symbol,
            "current_regime": latest["regime"],
            "signal": latest["signal"],
            "confidence": float(latest.get("confidence", 0)),
            "regime_history": history,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/ohlcv/{symbol}")
def get_ohlcv(symbol: str, limit: int = Query(120, le=1000)):
    """Last N OHLCV bars for charting."""
    from data.database import load_ohlcv
    df = load_ohlcv(symbol)
    if df.empty:
        raise HTTPException(404, f"No data for {symbol}")
    df = df.tail(limit).reset_index()
    df["time"] = df["time"].astype(str)
    return df.to_dict(orient="records")
