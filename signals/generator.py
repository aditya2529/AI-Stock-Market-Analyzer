"""Signal Generator — produces the full JSON signal payload with SHAP explainability."""
from __future__ import annotations
import time
import logging
import json
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import shap
from config import FEATURE_COLUMNS, GATE_SIGNAL_LATENCY_SEC
from signals.risk import calculate_stop_loss, calculate_target, risk_reward_ratio, position_size
from models.ensemble import Ensemble

logger = logging.getLogger(__name__)

_LABEL_IDX = {"BUY": 0, "HOLD": 1, "SELL": 2}


def _shap_reasons(ensemble: Ensemble, df_row: pd.DataFrame, signal: str, top_n: int = 3) -> list[str]:
    """Return top-N human-readable SHAP reasons for the XGBoost signal layer decision."""
    feature_cols = [c for c in FEATURE_COLUMNS if c in df_row.columns]
    try:
        explainer = shap.TreeExplainer(ensemble.signal_layer.model)
        shap_values = explainer.shap_values(df_row[feature_cols])
        # shap_values shape: (n_classes, n_samples, n_features) or (n_samples, n_features) for binary
        cls_idx = _LABEL_IDX.get(signal, 1)
        if isinstance(shap_values, list):
            vals = shap_values[cls_idx][0]
        else:
            vals = shap_values[0]
        top_idx = np.argsort(np.abs(vals))[::-1][:top_n]
        reasons = []
        for i in top_idx:
            feat = feature_cols[i]
            val = df_row[feat].iloc[0]
            direction = "bullish" if vals[i] > 0 else "bearish"
            reasons.append(f"{feat} = {val:.2f} ({direction})")
        return reasons
    except Exception as e:
        logger.debug("SHAP explanation failed: %s", e)
        return ["Model confidence based on pattern ensemble"]


def generate_signal(
    symbol: str,
    df: pd.DataFrame,
    ensemble: Ensemble,
    portfolio_value: float = 100_000.0,
) -> dict:
    """Generate the full signal payload for the latest bar.

    Args:
        symbol:          Ticker string.
        df:              Feature-engineered DataFrame (DatetimeIndex).
        ensemble:        Fitted Ensemble instance.
        portfolio_value: Current portfolio value for position sizing.

    Returns:
        Signal dict matching the PRD JSON schema.
    """
    t_start = time.perf_counter()

    latest = df.iloc[[-1]]  # keep as DataFrame for model compatibility
    result = ensemble.predict_with_confidence(df)
    latest_result = result.iloc[-1]

    signal = latest_result["signal"]
    confidence = float(latest_result["confidence"])
    regime = latest_result["regime"]

    price = float(latest["close"].iloc[-1])
    atr = float(latest["atr"].iloc[-1]) if "atr" in latest.columns else price * 0.01

    stop_loss = calculate_stop_loss(price, atr, signal)
    target = calculate_target(price, atr, signal)
    rr = risk_reward_ratio(price, stop_loss, target)
    shares = position_size(portfolio_value, price, stop_loss)

    reasons = _shap_reasons(ensemble, latest, signal)

    latency = time.perf_counter() - t_start
    if latency > GATE_SIGNAL_LATENCY_SEC:
        logger.warning("Signal latency %.2fs exceeds gate %.1fs", latency, GATE_SIGNAL_LATENCY_SEC)

    payload = {
        "symbol": symbol,
        "signal": signal,
        "confidence": round(confidence, 4),
        "price": round(price, 2),
        "stop_loss": stop_loss,
        "target": target,
        "risk_reward": rr,
        "regime": regime,
        "shares": shares,
        "reasons": reasons,
        "latency_sec": round(latency, 3),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return payload


def format_signal(payload: dict) -> str:
    """Pretty-print signal payload to console."""
    arrow = {"BUY": "▲", "SELL": "▼", "HOLD": "—"}.get(payload["signal"], "?")
    lines = [
        f"\n{'='*55}",
        f"  {arrow} {payload['signal']}  {payload['symbol']}  (confidence: {payload['confidence']:.1%})",
        f"{'='*55}",
        f"  Price      : ₹{payload['price']:,.2f}",
        f"  Stop-Loss  : ₹{payload['stop_loss']:,.2f}",
        f"  Target     : ₹{payload['target']:,.2f}",
        f"  R:R        : {payload['risk_reward']}x",
        f"  Shares     : {payload['shares']}",
        f"  Regime     : {payload['regime']}",
        f"  Reasons    :",
    ]
    for r in payload["reasons"]:
        lines.append(f"    • {r}")
    lines += [
        f"  Latency    : {payload['latency_sec']}s",
        f"  Time       : {payload['timestamp']}",
        f"{'='*55}\n",
    ]
    return "\n".join(lines)
