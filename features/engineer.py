"""Feature engineering: raw OHLCV → 19-column ML-ready DataFrame.

Features produced (no lookahead bias — all computed from past bars only):
    rsi, macd, macd_signal, macd_hist,
    bb_upper, bb_lower, bb_mid,
    atr, vwap, obv, adx,
    return_5, return_10, return_20,
    volatility_10, volatility_20,
    hour_of_day, day_of_week, minutes_to_close
"""
from __future__ import annotations
import pandas as pd
import numpy as np


# ── Low-level helpers ──────────────────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def _true_range(df: pd.DataFrame) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift(1)).abs()
    lc = (df["low"] - df["close"].shift(1)).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1)


# ── Individual indicator functions ─────────────────────────────────────────────

def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return 100 - (100 / (1 + rs))


def compute_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    fast_ema = _ema(close, fast)
    slow_ema = _ema(close, slow)
    macd_line = fast_ema - slow_ema
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_bollinger(close: pd.Series, period: int = 20, std_dev: float = 2.0):
    mid = _sma(close, period)
    std = close.rolling(window=period, min_periods=period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, lower, mid


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = _true_range(df)
    return tr.ewm(span=period, adjust=False).mean()


def _is_intraday_index(idx) -> bool:
    """Q4 fix: detect 5-min-style data so cumulative features can reset by date.
    Heuristic: more than 10 bars per calendar date (NSE intraday gives 75)."""
    try:
        if not isinstance(idx, pd.DatetimeIndex) or len(idx) < 20:
            return False
        return float(pd.Series(idx.date).value_counts().median()) > 10
    except Exception:
        return False


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP that resets at each calendar date when input is intraday.

    Q4 fix: the previous cumsum-from-series-start logic was meaningless on
    5-min bars (year-long cumulative average converges and stops moving).
    Daily-bar behaviour is unchanged.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3
    tpv = typical * df["volume"]
    if _is_intraday_index(df.index):
        date_key = pd.Series(df.index.date, index=df.index)
        cumvol = df["volume"].groupby(date_key).cumsum()
        cumtpv = tpv.groupby(date_key).cumsum()
    else:
        cumvol = df["volume"].cumsum()
        cumtpv = tpv.cumsum()
    return cumtpv / (cumvol + 1e-9)


def compute_obv(df: pd.DataFrame) -> pd.Series:
    """OBV that resets at each calendar date when input is intraday (Q4 fix)."""
    direction = df["close"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    signed_vol = direction * df["volume"]
    if _is_intraday_index(df.index):
        date_key = pd.Series(df.index.date, index=df.index)
        return signed_vol.groupby(date_key).cumsum()
    return signed_vol.cumsum()


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = _true_range(df)
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    atr = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / (atr + 1e-9))
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / (atr + 1e-9))

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9))
    return dx.ewm(span=period, adjust=False).mean()


def compute_rolling_returns(close: pd.Series, periods=(5, 10, 20)) -> dict:
    return {f"return_{p}": close.pct_change(p) for p in periods}


def compute_rolling_volatility(close: pd.Series, periods=(10, 20)) -> dict:
    log_ret = np.log(close / close.shift(1))
    return {f"volatility_{p}": log_ret.rolling(window=p, min_periods=p).std() for p in periods}


def compute_time_features(index: pd.DatetimeIndex) -> dict:
    hour_of_day = index.hour + index.minute / 60.0
    day_of_week = index.dayofweek  # 0=Mon … 4=Fri
    # NSE closes at 15:30 IST. For daily bars this is just a constant.
    close_hour, close_min = 15, 30
    minutes_to_close = (close_hour * 60 + close_min) - (index.hour * 60 + index.minute)
    minutes_to_close = np.clip(minutes_to_close.to_numpy(), 0, None)
    return {
        "hour_of_day": hour_of_day.to_numpy(),
        "day_of_week": day_of_week.to_numpy(),
        "minutes_to_close": minutes_to_close,
    }


# ── Main entry point ───────────────────────────────────────────────────────────

# P29: TTL cache for macro context.
# Without this cache, ``_load_macro_context`` fires twice (^NSEI, ^INDIAVIX)
# on every symbol tick. On a 50-symbol/8-worker tick that's up to ~800
# concurrent SQLite reads against the SAME daily-bar table per 5-min
# window. Even with the Round-2 per-call connection fix to ``load_ohlcv``,
# the aggregate concurrent pressure on those two macro symbols was the
# dominant heap-corruption trigger (see ``logs/faulthandler.log`` —
# May 18, 09:35-10:00 IST). Macro daily bars change at most once per
# trading day; a 5-minute TTL is effectively a session-wide cache while
# still picking up fresh ingests within the same engine run.
import threading
_MACRO_CACHE_LOCK = threading.Lock()
_MACRO_CACHE: dict = {"bucket": None, "value": None}
_MACRO_CACHE_TTL_SECONDS = 300  # 5 minutes


def _macro_bucket() -> int:
    """Current 5-minute bucket — cache key."""
    import time as _t
    return int(_t.time()) // _MACRO_CACHE_TTL_SECONDS


def _load_macro_context() -> tuple:
    """Load Nifty 50 returns and India VIX from the local DB.

    Returns a 4-tuple of pandas Series ``(nifty_ret, nifty_ma20, vix, vix_zscore)``
    on success, or ``(None, None, None, None)`` if the underlying read
    fails. Falls back gracefully so the feature pipeline never breaks.

    Thread-safe via a TTL'd process-wide cache + lock — see module-level
    comment for the P29 race rationale.
    """
    bucket = _macro_bucket()
    cached = _MACRO_CACHE.get("bucket")
    if cached == bucket and _MACRO_CACHE.get("value") is not None:
        return _MACRO_CACHE["value"]

    with _MACRO_CACHE_LOCK:
        # Re-check inside the lock — another thread may have populated.
        cached = _MACRO_CACHE.get("bucket")
        if cached == bucket and _MACRO_CACHE.get("value") is not None:
            return _MACRO_CACHE["value"]
        value = _read_macro_context_uncached()
        # Only cache successful reads; falling back to a None-tuple
        # should not poison the cache for the rest of the bucket.
        if value[0] is not None:
            _MACRO_CACHE["bucket"] = bucket
            _MACRO_CACHE["value"] = value
        return value


def _read_macro_context_uncached() -> tuple:
    """Uncached underlying read — opens an isolated SQLite connection per call.

    Skips ``data.database.load_ohlcv`` and reads the two macro symbols
    inline with one connection. Keeps the read path fully isolated from
    any shared module state and minimises wall-time the connection is
    held open.
    """
    try:
        import sqlite3
        from config import DB_PATH
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        try:
            sql = (
                "SELECT time, close FROM ohlcv "
                "WHERE symbol = ? AND resolution = ? ORDER BY time ASC"
            )
            nifty_df = pd.read_sql_query(
                sql, conn, params=["^NSEI", "1d"], parse_dates=["time"]
            )
            vix_df = pd.read_sql_query(
                sql, conn, params=["^INDIAVIX", "1d"], parse_dates=["time"]
            )
        finally:
            conn.close()
        if nifty_df.empty or vix_df.empty:
            return (None, None, None, None)
        nifty = nifty_df.set_index("time")["close"]
        vix = vix_df.set_index("time")["close"]
        # P44 fix (May 25 2026): macro DB occasionally has duplicate timestamps
        # (yfinance ingest race). Without dedup here, every downstream
        # series.reindex(out.index) raises "cannot reindex on an axis with
        # duplicate labels" and the symbol's feature engineering silently fails.
        # Keep LAST occurrence (most recent close wins).
        if nifty.index.has_duplicates:
            nifty = nifty[~nifty.index.duplicated(keep="last")]
        if vix.index.has_duplicates:
            vix = vix[~vix.index.duplicated(keep="last")]
        nifty_ret = nifty.pct_change().rename("nifty_return")
        nifty_ma20 = (nifty / nifty.rolling(20).mean() - 1).rename("nifty_vs_ma20")
        vix = vix.rename("india_vix")
        vix_zscore = (
            (vix - vix.rolling(60).mean()) / (vix.rolling(60).std() + 1e-9)
        ).rename("vix_zscore")
        return nifty_ret, nifty_ma20, vix, vix_zscore
    except Exception:
        return (None, None, None, None)


def compute_intraday_features(df: pd.DataFrame) -> pd.DataFrame:
    """Extra features that only make sense on intraday (5-min) bars."""
    out = df.copy()

    # Daily VWAP — reset each calendar day
    out["date"] = out.index.date
    out["vwap_intraday"] = (
        out.groupby("date")
        .apply(lambda g: (g["close"] * g["volume"]).cumsum() / (g["volume"].cumsum() + 1e-9),
               include_groups=False)
        .reset_index(level=0, drop=True)
    )
    out = out.drop(columns=["date"])

    # Opening range (first 3 bars = first 15 min of the day)
    def opening_range(g):
        first3 = g.iloc[:3]
        g = g.copy()
        g["or_high"] = first3["high"].max()
        g["or_low"]  = first3["low"].min()
        return g
    out["date2"] = out.index.date
    out = out.groupby("date2", group_keys=False).apply(opening_range, include_groups=False)
    # groupby drops the key col when include_groups=False — restore it from index
    if "date2" not in out.columns:
        out["date2"] = out.index.date
    out = out.drop(columns=["date2"])

    # Price vs opening range
    out["above_or"] = (out["close"] > out["or_high"]).astype(float)
    out["below_or"] = (out["close"] < out["or_low"]).astype(float)

    # Volume surge: current bar volume vs 20-bar rolling average
    out["volume_surge"] = out["volume"] / (out["volume"].rolling(20).mean() + 1e-9)

    # Minutes since market open (9:15 IST)
    market_open_minutes = 9 * 60 + 15
    out["mins_since_open"] = (out.index.hour * 60 + out.index.minute) - market_open_minutes
    out["mins_since_open"] = out["mins_since_open"].clip(lower=0)

    # Minutes to force-close (3:15 PM)
    force_close_minutes = 15 * 60 + 15
    out["mins_to_close"] = force_close_minutes - (out.index.hour * 60 + out.index.minute)
    out["mins_to_close"] = out["mins_to_close"].clip(lower=0)

    return out


def engineer_features(df: pd.DataFrame, add_macro: bool = True) -> pd.DataFrame:
    """Compute all features on an OHLCV DataFrame.

    Args:
        df:          DataFrame with DatetimeIndex and columns [open, high, low, close, volume].
        add_macro:   If True, join Nifty 50 returns and India VIX as market context features.

    Returns:
        Original DataFrame with feature columns appended.
        Rows with NaN features (warm-up period) are dropped.
    """
    out = df.copy()

    # Ensure DatetimeIndex — fetch_and_store returns RangeIndex with 'time' as column
    if "time" in out.columns and not isinstance(out.index, pd.DatetimeIndex):
        out = out.set_index("time")
    out.index = pd.to_datetime(out.index)

    # P44 fix (May 25 2026): yfinance occasionally returns duplicate 5-min timestamps.
    # Without this guard, the macro reindex at line ~345 raises
    # "cannot reindex on an axis with duplicate labels" and the entire symbol
    # is silently dropped from the tick. Keep the LAST occurrence of any
    # duplicated timestamp (most recent data wins).
    if out.index.has_duplicates:
        out = out[~out.index.duplicated(keep="last")]

    # RSI
    out["rsi"] = compute_rsi(out["close"])

    # MACD
    out["macd"], out["macd_signal"], out["macd_hist"] = compute_macd(out["close"])

    # Bollinger Bands
    out["bb_upper"], out["bb_lower"], out["bb_mid"] = compute_bollinger(out["close"])

    # ATR
    out["atr"] = compute_atr(out)

    # VWAP
    out["vwap"] = compute_vwap(out)

    # OBV (normalised to units of 1M shares to keep scale reasonable)
    out["obv"] = compute_obv(out) / 1e6

    # ADX
    out["adx"] = compute_adx(out)

    # Rolling returns
    for name, series in compute_rolling_returns(out["close"]).items():
        out[name] = series

    # Rolling volatility
    for name, series in compute_rolling_volatility(out["close"]).items():
        out[name] = series

    # Time features
    idx = out.index if isinstance(out.index, pd.DatetimeIndex) else pd.to_datetime(out.index)
    for name, values in compute_time_features(idx).items():
        out[name] = values

    # Macro context: Nifty 50 + India VIX — tells model what the whole market is doing
    macro_cols = []
    if add_macro:
        nifty_ret, nifty_ma20, vix, vix_zscore = _load_macro_context()
        if nifty_ret is not None:
            for series in [nifty_ret, nifty_ma20, vix, vix_zscore]:
                out[series.name] = series.reindex(out.index, method="ffill")
            macro_cols = ["nifty_return", "nifty_vs_ma20", "india_vix", "vix_zscore"]

    # Drop warm-up rows that have NaN in any feature column
    feature_cols = [
        "rsi", "macd", "macd_signal", "macd_hist",
        "bb_upper", "bb_lower", "bb_mid",
        "atr", "vwap", "obv", "adx",
        "return_5", "return_10", "return_20",
        "volatility_10", "volatility_20",
        "hour_of_day", "day_of_week", "minutes_to_close",
    ] + macro_cols
    out = out.dropna(subset=feature_cols)

    # Add intraday-specific features if this is 5-min data
    if isinstance(out.index, pd.DatetimeIndex) and len(out) > 20:
        bars_per_day = out.groupby(out.index.date).size().median()
        if bars_per_day > 10:   # more than 10 bars/day = intraday data
            out = compute_intraday_features(out)
            # Q4 fix: drop warm-up rows where the intraday-aware features
            # are NaN (volume_surge needs a 20-bar rolling window). Without
            # this, the LSTM picks up NaN at the head of each symbol.
            intraday_cols = [c for c in ["vwap_intraday", "volume_surge",
                                          "or_high", "or_low",
                                          "above_or", "below_or",
                                          "mins_since_open", "mins_to_close"]
                             if c in out.columns]
            if intraday_cols:
                out = out.dropna(subset=intraday_cols)

    return out
