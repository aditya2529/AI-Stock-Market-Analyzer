"""Market data and regime endpoints."""
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/market", tags=["market"])


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
