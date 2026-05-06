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


def _get_adapter(market: str):
    """Select adapter based on market type and available credentials."""
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
                    resolution: str = "1d") -> pd.DataFrame:
    """Fetch OHLCV for symbol, validate, persist to SQLite, return cleaned DataFrame."""
    init_db()
    market = _classify_symbol(symbol)
    adapter = _get_adapter(market)
    adapter_name = type(adapter).__name__.replace("Adapter", "")
    logger.info("Fetching %s | %s | %d years via %s …", symbol, resolution, years, adapter_name)

    raw = adapter.fetch_ohlcv(symbol, years=years, resolution=resolution)
    cleaned = validate_and_clean(raw, symbol)
    upsert_ohlcv(cleaned, symbol=symbol, market=market, resolution=resolution)
    logger.info("Stored %d bars for %s.", len(cleaned), symbol)
    return cleaned


def get_ohlcv(symbol: str, resolution: str = "1d") -> pd.DataFrame:
    """Load OHLCV from the local database (fetch first if empty)."""
    init_db()
    df = load_ohlcv(symbol, resolution=resolution)
    if df.empty:
        logger.info("No local data for %s — fetching now.", symbol)
        fetch_and_store(symbol, resolution=resolution)
        df = load_ohlcv(symbol, resolution=resolution)
    return df
