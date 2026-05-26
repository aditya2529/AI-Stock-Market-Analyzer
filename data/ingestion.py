import logging
from config import DATA_ADAPTER, DEFAULT_YEARS
from data.validator import validate_and_clean
from data.database import init_db, upsert_ohlcv, load_ohlcv
import pandas as pd

logger = logging.getLogger(__name__)

# Known crypto quote currencies — used to detect crypto symbols
_CRYPTO_SUFFIXES = ("USDT", "BTC", "ETH", "BUSD", "USDC")


def _classify_symbol(symbol: str) -> str:
    """Return 'NSE', 'CRYPTO', or 'US' based on symbol format."""
    s = symbol.upper()
    if s.endswith(".NS") or s.endswith(".BO"):
        return "NSE"
    if any(s.endswith(sfx) for sfx in _CRYPTO_SUFFIXES):
        return "CRYPTO"
    # Index symbols used as macro context
    if s.startswith("^"):
        return "NSE"
    return "US"


def _get_adapter(market: str, source: str | None = None):
    """Select adapter based on market type, credentials, and optional
    R8 ``source`` override.

    When ``source`` is provided, it forces the adapter regardless of
    market / ``config.DATA_ADAPTER`` / available credentials. This is
    the CLI escape hatch for ``main.py fetch --source {yfinance,upstox}``.
    When ``source`` is None (the legacy call shape), behaviour is
    bit-for-bit unchanged.
    """
    if source == "yfinance":
        from data.adapters.yfinance_adapter import YFinanceAdapter
        return YFinanceAdapter()
    if source == "upstox":
        from data.adapters.upstox_adapter import UpstoxAdapter
        return UpstoxAdapter()

    # Legacy auto-selection — unchanged from pre-R8.
    if market == "CRYPTO":
        from data.adapters.binance_adapter import BinanceAdapter
        return BinanceAdapter()
    if market == "US":
        from config import ALPACA_API_KEY
        if ALPACA_API_KEY:
            from data.adapters.alpaca_adapter import AlpacaAdapter
            return AlpacaAdapter()
        # Fall back to yfinance if no Alpaca credentials
        logger.info("Alpaca not configured — using yfinance for US symbol")
        from data.adapters.yfinance_adapter import YFinanceAdapter
        return YFinanceAdapter()
    # NSE / default
    if DATA_ADAPTER == "upstox":
        from data.adapters.upstox_adapter import UpstoxAdapter
        return UpstoxAdapter()
    from data.adapters.yfinance_adapter import YFinanceAdapter
    return YFinanceAdapter()


def fetch_and_store(symbol: str, years: int = DEFAULT_YEARS,
                    resolution: str = "1d",
                    source: str | None = None) -> pd.DataFrame:
    """Fetch OHLCV for symbol, validate, persist to SQLite, return cleaned DataFrame.

    ``source`` (R8): optional explicit adapter selector
    (``"yfinance"`` / ``"upstox"``). When None, uses the legacy
    auto-selection by market + credentials. Plumbed in from the
    ``main.py fetch --source`` CLI flag.

    R8 intraday dispatch: when ``source == "upstox"`` AND the
    resolution maps to an Upstox v3 intraday interval (5m / 15m / 30m
    / 60m / 1m), the call routes to ``UpstoxAdapter.get_historical_intraday``
    instead of ``fetch_ohlcv`` — the v2 day/week/month endpoint does
    not support those intervals, and the P42 test contract requires
    ``fetch_ohlcv("5m")`` to raise ValueError. Routing here (at the
    ingestion layer) keeps the adapter's v2 surface unchanged.
    """
    init_db()
    market = _classify_symbol(symbol)
    adapter = _get_adapter(market, source=source)
    adapter_name = type(adapter).__name__.replace("Adapter", "")
    logger.info("Fetching %s | %s | %d years via %s …", symbol, resolution, years, adapter_name)

    raw = _adapter_fetch(adapter, symbol, years, resolution, source)
    cleaned = validate_and_clean(raw, symbol)
    upsert_ohlcv(cleaned, symbol=symbol, market=market, resolution=resolution)
    logger.info("Stored %d bars for %s.", len(cleaned), symbol)
    return cleaned


def _adapter_fetch(adapter, symbol: str, years: int, resolution: str,
                    source: str | None) -> pd.DataFrame:
    """R8 — dispatch to the right adapter method based on (source,
    resolution). For ``source=="upstox"`` + intraday resolution, call
    ``get_historical_intraday``; otherwise fall through to the legacy
    ``fetch_ohlcv``.

    Imported lazily inside the branch so non-upstox callers don't pay
    the cost of loading the upstox adapter helper.
    """
    if source == "upstox":
        from data.adapters.upstox_adapter import _resolution_to_minutes
        minutes = _resolution_to_minutes(resolution)
        if minutes is not None:
            from datetime import datetime, timedelta
            to_dt = datetime.now()
            from_dt = to_dt - timedelta(days=int(max(1, years) * 365.25))
            return adapter.get_historical_intraday(
                symbol=symbol,
                interval_minutes=minutes,
                from_date=from_dt.strftime("%Y-%m-%d"),
                to_date=to_dt.strftime("%Y-%m-%d"),
            )
    return adapter.fetch_ohlcv(symbol, years=years, resolution=resolution)


def get_ohlcv(symbol: str, resolution: str = "1d") -> pd.DataFrame:
    """Load OHLCV from the local database (fetch first if empty)."""
    init_db()
    df = load_ohlcv(symbol, resolution=resolution)
    if df.empty:
        logger.info("No local data for %s — fetching now.", symbol)
        fetch_and_store(symbol, resolution=resolution)
        df = load_ohlcv(symbol, resolution=resolution)
    return df
