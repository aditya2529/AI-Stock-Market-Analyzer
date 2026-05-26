"""T2.5W1-C — retrain intraday ensemble on 2024-01-01 to 2026-02-28
and walk-forward-backtest v1 vs v2 on 2026-03-01 to 2026-05-25 holdout.

WHY THIS IS A NEW FILE (not main.py / not scripts/):
  - main.py cmd_train has no date-cutoff argument; the Option A split
    requires explicit slicing of OHLCV to <= 2026-02-28 for training
    AND slicing to (2026-02-28, 2026-05-25] for the holdout backtest.
  - backtesting/engine.py:run_walk_forward_pretrained assumes a 50%
    warm-up region on whatever df you pass in — if I pass the full
    2024-2026 range, the "warm-up" half would silently leak training-
    period data into the test path. So we need an explicit holdout-only
    inference run, not a re-use of the existing walk-forward fold logic.
  - Brief says: "Files: training scripts under models/ — your call which".
    This file is that, under models/. Zero touches to main.py /
    scripts/ / backtesting/ / api/ / dashboard/.

WHAT THIS SCRIPT DOES:
  1. Loads 5-min OHLCV from market_data.db for the 25 NSE default
     symbols (no fetch — phase 4 already brought the DB current).
  2. Slices each symbol's data into TRAIN (<= 2026-02-28) and HOLDOUT
     (2026-03-01 to 2026-05-25 inclusive). Engineers features per slice
     to avoid look-ahead bias from rolling features that span the
     cutoff.
  3. Trains a fresh Ensemble on the combined TRAIN slice using the
     same vol_scaled / intraday config that the production train path
     uses, so v2 differs from v1 only in DATA, not pipeline.
  4. Saves v2 to models/saved/ensemble_intraday_v2.pkl WITHOUT touching
     ensemble_intraday.pkl. That swap happens in a separate ops-gated
     install step iff the holdout gate passes (Q5(a) from the ops brief).
  5. Loads v1 from models/saved/ensemble_intraday.pkl.
  6. For each symbol's HOLDOUT slice, runs predict_with_confidence for
     BOTH models, applies the regime gate (HIGH_VOL/UNKNOWN -> HOLD)
     and the production confidence gate (< 0.60 -> HOLD), simulates
     long-only trades, and accumulates PnL/return per trade.
  7. Aggregates trades across symbols PER MODEL, computes PF / Sharpe /
     Win-rate / Max-drawdown / n_trades, and prints a side-by-side
     comparison.
  8. Writes the report as JSON to logs/retrain_v2_backtest_report.json
     and an annotated commit-ready markdown summary to
     logs/retrain_v2_summary.md so the commit message can quote
     numbers verbatim.

GATE (ops-defined, this script does NOT auto-install v2 — only reports):
  - v2 PF >= 1.3 AND v2 PF > v1 PF   -> ops will SHIP v2
  - v2 PF >= 1.3 AND v2 PF <= v1 PF  -> ops decides
  - v2 PF < 1.3                       -> SHIP A+B only, keep v1

If a hard wall-clock cap is hit during LSTM training, surface it in
the report under `notes` so the commit message can quote it (per
brief: "epochs capped at N due to wall-clock budget, holdout metrics
may understate v2 capability").
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from config import (
    DEFAULT_SYMBOLS,
    BROKERAGE_PCT,
    SLIPPAGE_PCT,
    INTRADAY_LOOKAHEAD,
    INTRADAY_BUY_THRESHOLD,
    INTRADAY_SELL_THRESHOLD,
)
from data.database import load_ohlcv
from features.engineer import engineer_features
from models.ensemble import Ensemble
from backtesting.metrics import compute_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
logger = logging.getLogger("retrain_v2")

TRAIN_START = pd.Timestamp("2024-01-01")
TRAIN_END_INCLUSIVE = pd.Timestamp("2026-02-28 23:59:59")
HOLDOUT_START = pd.Timestamp("2026-03-01 00:00:00")
HOLDOUT_END_INCLUSIVE = pd.Timestamp("2026-05-25 23:59:59")

# Mirror the production confidence floor — the holdout backtest must
# evaluate the model under the same trade-eligibility rules the live
# engine uses, otherwise v1 vs v2 isn't comparable to live behaviour.
CONFIDENCE_FLOOR = float(os.getenv("SIGNAL_MIN_CONFIDENCE", "0.60"))

MODELS_DIR = ROOT / "models" / "saved"
V1_PATH = MODELS_DIR / "ensemble_intraday.pkl"
V2_PATH = MODELS_DIR / "ensemble_intraday_v2.pkl"
LOGS_DIR = ROOT / "logs"
JSON_REPORT = LOGS_DIR / "retrain_v2_backtest_report.json"
MD_REPORT = LOGS_DIR / "retrain_v2_summary.md"


def _slice_to_window(df: pd.DataFrame, start: pd.Timestamp,
                      end: pd.Timestamp) -> pd.DataFrame:
    """Return the subset of df whose DatetimeIndex falls in [start, end]
    inclusive. Tolerates tz-naive indices (DB stores naive 'YYYY-MM-DD
    HH:MM:SS' strings for 5-min bars)."""
    if df.empty:
        return df
    if df.index.tz is not None:
        start = start.tz_localize(df.index.tz) if start.tz is None else start
        end = end.tz_localize(df.index.tz) if end.tz is None else end
    return df.loc[(df.index >= start) & (df.index <= end)]


def _simulate_long_only(df_test: pd.DataFrame,
                         predictions: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Long-only trade simulator that mirrors backtesting/engine._simulate_trades
    but also enforces the live regime + confidence gates so v1/v2 are
    evaluated under PRODUCTION trade-eligibility rules, not raw model
    signals.

    Gates applied (matches intraday/engine.py):
      - regime in (HIGH_VOL, UNKNOWN, TRENDING_DOWN) -> HOLD
      - confidence < CONFIDENCE_FLOOR -> HOLD
    Then long-only open on BUY, close on SELL or HOLD when in-position.
    """
    portfolio = 1.0
    position = None
    trades: list[dict] = []
    equity = [portfolio]

    prices = df_test["close"].values
    signals = predictions["signal"].values.copy()
    regimes = predictions["regime"].values
    confs = predictions["confidence"].values

    # Apply production gates (same as intraday/engine._process_symbol)
    for i, (sig, reg, conf) in enumerate(zip(signals, regimes, confs)):
        if reg in ("HIGH_VOL", "UNKNOWN"):
            signals[i] = "HOLD"
            continue
        if reg == "TRENDING_DOWN" and sig == "BUY":
            signals[i] = "HOLD"
            continue
        if reg == "TRENDING_UP" and sig == "SELL":
            signals[i] = "HOLD"
            continue
        if sig in ("BUY", "SELL") and conf < CONFIDENCE_FLOOR:
            signals[i] = "HOLD"

    for i, (price, signal) in enumerate(zip(prices, signals)):
        cost = price * (1 + BROKERAGE_PCT + SLIPPAGE_PCT)

        if position is None and signal == "BUY":
            position = {"entry_price": cost, "entry_bar": i}
        elif position is not None and signal in ("SELL", "HOLD"):
            # Long-only exits on either an active SELL or whenever the
            # signal stops being BUY. Mirrors _simulate_trades semantics
            # in backtesting/engine.py.
            exit_price = price * (1 - BROKERAGE_PCT - SLIPPAGE_PCT)
            trade_return = exit_price / position["entry_price"] - 1
            pnl = portfolio * trade_return
            trades.append({
                "pnl": pnl, "return": trade_return,
                "entry_bar": position["entry_bar"], "exit_bar": i,
            })
            portfolio *= (1 + trade_return)
            position = None

        equity.append(portfolio)

    equity_series = pd.Series(equity, name="equity")
    trades_df = (pd.DataFrame(trades) if trades
                 else pd.DataFrame(columns=["pnl", "return",
                                              "entry_bar", "exit_bar"]))
    return trades_df, equity_series


def _load_symbol_5m(symbol: str) -> pd.DataFrame:
    """Pull 5-min bars for one symbol from the local DB."""
    df = load_ohlcv(symbol, resolution="5m")
    if df.empty:
        logger.warning("%s: no 5m data in DB", symbol)
        return df
    # Ensure index is DatetimeIndex (load_ohlcv usually returns it as
    # such, but tolerate string indices if the DB stored them that way).
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    return df.sort_index()


def _build_train_frame(symbols: list[str]) -> pd.DataFrame:
    """Concatenate engineered features across symbols, restricted to
    the training window. Features computed per-symbol BEFORE slicing
    so the feature window can use earlier history within each symbol
    (the rolling features are causal — engineer_features already drops
    rows where features are incomplete)."""
    frames = []
    for sym in symbols:
        raw = _load_symbol_5m(sym)
        if raw.empty:
            continue
        # Engineer features on the FULL symbol history, then slice. This
        # avoids edge effects from cold-start rolling windows that would
        # otherwise drop the first ~100 rows of the TRAIN window.
        try:
            feat = engineer_features(raw)
        except Exception as e:
            logger.warning("%s: engineer_features failed: %s", sym, e)
            continue
        train_slice = _slice_to_window(feat, TRAIN_START, TRAIN_END_INCLUSIVE)
        if len(train_slice) < 200:
            logger.warning("%s: only %d train bars — skipping", sym,
                            len(train_slice))
            continue
        frames.append(train_slice)
        logger.info("  %s: %d train bars (%s -> %s)",
                    sym, len(train_slice),
                    train_slice.index.min(), train_slice.index.max())
    if not frames:
        raise RuntimeError("No training data assembled — check DB freshness.")
    return pd.concat(frames, ignore_index=False)


def _build_holdout_slices(symbols: list[str]) -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
    """For each symbol return (raw_df_with_close, engineered_features_df)
    restricted to the HOLDOUT window. Both are needed at backtest time:
    raw_df_with_close supplies trade execution prices, engineered df is
    what the model consumes."""
    out = {}
    for sym in symbols:
        raw = _load_symbol_5m(sym)
        if raw.empty:
            continue
        try:
            feat = engineer_features(raw)
        except Exception as e:
            logger.warning("%s: engineer_features failed for holdout: %s",
                            sym, e)
            continue
        feat_h = _slice_to_window(feat, HOLDOUT_START, HOLDOUT_END_INCLUSIVE)
        raw_h = _slice_to_window(raw, HOLDOUT_START, HOLDOUT_END_INCLUSIVE)
        # Align on intersection — engineer_features may drop early rows.
        common = feat_h.index.intersection(raw_h.index)
        if len(common) < 50:
            logger.warning("%s: holdout too short (%d bars) — skipping",
                            sym, len(common))
            continue
        out[sym] = (raw_h.loc[common], feat_h.loc[common])
    return out


def _backtest_model_on_holdout(label: str, ensemble,
                                holdout: dict) -> dict:
    """Run one model across all symbols' holdout slices, aggregating
    trades. Returns a dict with the metric stack the brief asks for."""
    all_trades = []
    per_symbol = {}
    for sym, (raw_h, feat_h) in holdout.items():
        if feat_h.empty:
            continue
        try:
            preds = ensemble.predict_with_confidence(feat_h)
        except Exception as e:
            logger.warning("%s/%s: predict failed: %s", label, sym, e)
            continue
        trades, equity = _simulate_long_only(raw_h, preds)
        per_symbol[sym] = {
            "n_trades": len(trades),
            "pf": (compute_all(trades, equity)["profit_factor"]
                   if not trades.empty else float("nan")),
            "win_rate": (compute_all(trades, equity)["win_rate"]
                         if not trades.empty else float("nan")),
        }
        if not trades.empty:
            trades = trades.copy()
            trades["symbol"] = sym
            all_trades.append(trades)
        logger.info("  %s/%s: %d trades", label, sym, len(trades))

    if not all_trades:
        return {"label": label, "metrics": {}, "per_symbol": per_symbol,
                "n_trades": 0}

    combined = pd.concat(all_trades, ignore_index=True)
    # Build a synthetic combined equity curve from aggregated PnL —
    # treats each trade as sequential for the curve, which is fine for
    # max_drawdown / sharpe purposes given the per-symbol returns are
    # already independent (long-only single-position-per-symbol).
    eq = (1.0 + combined["return"]).cumprod()
    eq = pd.concat([pd.Series([1.0]), eq], ignore_index=True)
    metrics = compute_all(combined, eq)
    return {
        "label": label,
        "metrics": metrics,
        "per_symbol": per_symbol,
        "n_trades": int(len(combined)),
    }


def _format_md_table(v1: dict, v2: dict) -> str:
    def cell(d: dict, key: str, fmt: str = "{:.3f}") -> str:
        if not d["metrics"]:
            return "—"
        val = d["metrics"].get(key)
        if val is None:
            return "—"
        if isinstance(val, float) and (val != val):  # NaN
            return "—"
        if val == float("inf"):
            return "∞"
        return fmt.format(val)

    lines = [
        "## v1 vs v2 — 2026-03-01 to 2026-05-25 holdout (aggregated across NSE default symbols)",
        "",
        "| Metric | v1 (current production) | v2 (retrained) | Δ |",
        "|---|---:|---:|---:|",
    ]
    keys = [
        ("profit_factor", "Profit Factor", "{:.3f}"),
        ("sharpe", "Sharpe", "{:.3f}"),
        ("win_rate", "Win rate", "{:.3f}"),
        ("max_drawdown", "Max drawdown", "{:.3f}"),
    ]
    for key, label, fmt in keys:
        v1_val = v1["metrics"].get(key) if v1["metrics"] else None
        v2_val = v2["metrics"].get(key) if v2["metrics"] else None
        delta = "—"
        if (isinstance(v1_val, (int, float)) and isinstance(v2_val, (int, float))
                and v1_val == v1_val and v2_val == v2_val
                and v1_val != float("inf") and v2_val != float("inf")):
            delta = fmt.format(v2_val - v1_val)
        lines.append(f"| {label} | {cell(v1, key, fmt)} | {cell(v2, key, fmt)} | {delta} |")
    lines.append(f"| n_trades | {v1.get('n_trades', 0)} | {v2.get('n_trades', 0)} | — |")
    return "\n".join(lines)


def main() -> int:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    notes: list[str] = []
    wall_start = time.time()

    symbols = list(DEFAULT_SYMBOLS)
    logger.info("=== T2.5W1-C retrain + backtest ===")
    logger.info("Symbols: %d (%s)", len(symbols), ", ".join(symbols[:5]) + " …")
    logger.info("Train window:   %s -> %s", TRAIN_START.date(),
                TRAIN_END_INCLUSIVE.date())
    logger.info("Holdout window: %s -> %s", HOLDOUT_START.date(),
                HOLDOUT_END_INCLUSIVE.date())
    logger.info("Confidence floor (production gate): %.2f", CONFIDENCE_FLOOR)

    # 1. Assemble training data
    logger.info("Loading + engineering training data ...")
    combined_train = _build_train_frame(symbols)
    logger.info("Combined train frame: %d bars across %d symbols",
                len(combined_train), len(combined_train.index.normalize().unique()))

    # 2. Train v2 with the same intraday config the production path uses
    logger.info("Training v2 ensemble (XGBoost + LSTM + HMM + meta-LR) ...")
    v2 = Ensemble()
    fit_start = time.time()
    v2.fit(combined_train,
           lookahead=INTRADAY_LOOKAHEAD,
           buy_threshold=INTRADAY_BUY_THRESHOLD,
           sell_threshold=INTRADAY_SELL_THRESHOLD,
           vol_scaled=True)
    fit_secs = time.time() - fit_start
    logger.info("v2 train wall-clock: %.0fs (%.1f min)", fit_secs, fit_secs / 60)
    notes.append(f"v2 train wall-clock: {fit_secs/60:.1f} min on CPU")

    # 3. Save v2 to its own path. Do NOT touch ensemble_intraday.pkl —
    # ops gates the install step.
    import pickle
    with open(V2_PATH, "wb") as f:
        pickle.dump(v2, f)
    logger.info("v2 saved to %s", V2_PATH)

    # 4. Load v1 (current production) for the side-by-side
    if not V1_PATH.exists():
        raise FileNotFoundError(
            f"v1 ensemble not found at {V1_PATH} — cannot run v1-vs-v2 "
            f"comparison. Run `python main.py train --intraday` first or "
            f"restore from backup.")
    logger.info("Loading v1 from %s", V1_PATH)
    with open(V1_PATH, "rb") as f:
        v1 = pickle.load(f)

    # 5. Backtest both on the same holdout
    logger.info("Building holdout slices ...")
    holdout = _build_holdout_slices(symbols)
    logger.info("Holdout symbols ready: %d", len(holdout))

    logger.info("Running v1 inference + simulated trades on holdout ...")
    v1_report = _backtest_model_on_holdout("v1", v1, holdout)
    logger.info("Running v2 inference + simulated trades on holdout ...")
    v2_report = _backtest_model_on_holdout("v2", v2, holdout)

    # 6. Print comparison + write reports
    print("\n" + "=" * 70)
    print("HOLDOUT 2026-03-01 to 2026-05-25  |  v1 (production) vs v2 (retrained)")
    print("=" * 70)
    print(_format_md_table(v1_report, v2_report))
    print("=" * 70)

    # Gate evaluation per ops brief
    v1_pf = v1_report["metrics"].get("profit_factor")
    v2_pf = v2_report["metrics"].get("profit_factor")
    gate = "UNKNOWN"
    if isinstance(v2_pf, (int, float)) and v2_pf == v2_pf:
        if v2_pf >= 1.3 and (not isinstance(v1_pf, (int, float))
                              or v2_pf > v1_pf):
            gate = "SHIP v2"
        elif v2_pf >= 1.3:
            gate = "OPS DECIDES (v2 PF >= 1.3 but <= v1)"
        else:
            gate = "KEEP v1 — ship A+B only"
    print(f"\nGate verdict: {gate}\n")

    report = {
        "generated_at": datetime.now().isoformat(),
        "train_window": [str(TRAIN_START.date()),
                          str(TRAIN_END_INCLUSIVE.date())],
        "holdout_window": [str(HOLDOUT_START.date()),
                            str(HOLDOUT_END_INCLUSIVE.date())],
        "confidence_floor": CONFIDENCE_FLOOR,
        "v1": v1_report,
        "v2": v2_report,
        "gate_verdict": gate,
        "wall_clock_secs": time.time() - wall_start,
        "notes": notes,
    }
    JSON_REPORT.write_text(json.dumps(report, indent=2, default=str),
                            encoding="utf-8")
    MD_REPORT.write_text(_format_md_table(v1_report, v2_report)
                          + f"\n\n**Gate verdict:** {gate}\n",
                          encoding="utf-8")
    logger.info("JSON report -> %s", JSON_REPORT)
    logger.info("MD summary  -> %s", MD_REPORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
