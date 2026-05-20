"""Signal Generator — produces the full JSON signal payload with SHAP explainability."""
from __future__ import annotations
import time
import logging
import json
import threading
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import shap
from config import FEATURE_COLUMNS, GATE_SIGNAL_LATENCY_SEC
from signals.risk import calculate_stop_loss, calculate_target, risk_reward_ratio, position_size
from models.ensemble import Ensemble

logger = logging.getLogger(__name__)

_LABEL_IDX = {"BUY": 0, "HOLD": 1, "SELL": 2}

# P33 (Round 4 final, May 20 — DISABLE SHAP):
#
# After three variants all failed, the honest engineering call is to skip
# SHAP entirely in production:
#
# Variant 1 (89b7eaf hot-patch): module-level cache + lock around
# shap_values() only. Insufficient — concurrent cache-miss construction
# raced on the booster. Caused May 20 09:35 + 09:40 crashes.
#
# Variant 2 (per-thread cache via threading.local): also insufficient.
# Each thread got its own TreeExplainer, but all wrapped the SAME
# xgboost.Booster from ensemble.signal_layer.model; booster.feature_names
# / predict still raced at the C level. Stress test crashed immediately.
#
# Variant 3 (cache + double-checked lock around BOTH construction and
# shap_values): also crashes. The booster's C-level state is mutated
# during data prep (_from_pandas_df) even when the SHAP layer is locked.
#
# Root conclusion: shap.TreeExplainer + xgboost.Booster on this stack
# (Python 3.11.8, xgboost 3.2.0, shap latest, Windows) is fundamentally
# not thread-safe at any lock granularity short of "1 thread total". This
# is an upstream issue, not something this codebase can patch.
#
# Production trade-off: alerts lose the per-feature SHAP explanation
# ("rsi=55 bullish, macd=14 bullish, ..."). They show a generic reason
# instead. The strategy itself does NOT use SHAP — explanations are
# cosmetic display only in Telegram/email/dashboard alerts.
#
# Filed as P37 in PENDING_AUDIT_FIXES.md for proper revival when:
# - Python is upgraded past 3.11
# - xgboost or shap ships a thread-safe-on-Windows variant
# - OR a subprocess-isolation wrapper is built (heavy but bulletproof)


def _shap_reasons(ensemble: Ensemble, df_row: pd.DataFrame, signal: str, top_n: int = 3) -> list[str]:
    """P33: SHAP disabled in production due to xgboost+shap thread-unsafety.

    See module-level comment for the diagnostic chain. The strategy doesn't
    use these reasons — they're cosmetic only. Return a single generic
    string so downstream code (alert formatters) gets a non-empty list.
    """
    # Intentionally unconditional. See P33 entry in PENDING_AUDIT_FIXES.md.
    return ["Model confidence based on pattern ensemble"]
    # Original SHAP path retained below for future revival.
    # Reachable only by removing the early-return above.
    feature_cols = [c for c in FEATURE_COLUMNS if c in df_row.columns]
    try:
        import shap
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
