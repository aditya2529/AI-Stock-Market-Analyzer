"""Signal endpoints — generate live signals for one or many symbols."""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
import logging

router = APIRouter(prefix="/api/signals", tags=["signals"])
logger = logging.getLogger(__name__)

_ensemble = None


def _get_ensemble():
    global _ensemble
    if _ensemble is None:
        from models.ensemble import Ensemble
        try:
            _ensemble = Ensemble.load()
        except FileNotFoundError:
            raise HTTPException(503, "Model not found — run: python main.py train --all")
    return _ensemble


@router.get("/{symbol}")
def get_signal(symbol: str, portfolio: float = Query(100_000.0)):
    """Generate a live signal for the given symbol."""
    from data.ingestion import get_ohlcv
    from features.engineer import engineer_features
    from signals.generator import generate_signal

    try:
        df = get_ohlcv(symbol)
        featured = engineer_features(df)
        payload = generate_signal(symbol, featured, _get_ensemble(),
                                  portfolio_value=portfolio)
        return payload
    except Exception as e:
        logger.error("Signal error for %s: %s", symbol, e)
        raise HTTPException(500, str(e))


@router.get("")
def get_signals_batch(
    symbols: str = Query(..., description="Comma-separated symbols"),
    portfolio: float = Query(100_000.0),
):
    """Generate signals for multiple symbols. Returns list of signal payloads."""
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
    results = []
    ens = _get_ensemble()
    from data.ingestion import get_ohlcv
    from features.engineer import engineer_features
    from signals.generator import generate_signal

    for sym in sym_list:
        try:
            df = get_ohlcv(sym)
            featured = engineer_features(df)
            payload = generate_signal(sym, featured, ens, portfolio_value=portfolio)
            results.append(payload)
        except Exception as e:
            results.append({"symbol": sym, "error": str(e)})
    return results
