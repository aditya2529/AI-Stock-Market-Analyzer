"""R12 — retrain v2b with shorter train end so 2026-01-01 → today
is a clean out-of-sample window for the engine-replay v1-vs-v2b test.

WHY THIS FILE EXISTS (not just an edit of retrain_and_backtest_v2.py):
  - Keep the R9 harness (commit b935c21) untouched as the forensic
    record of how v2 was originally trained (TRAIN_END 2026-02-28).
  - R12 needs v2b with TRAIN_END 2025-12-31 and artifact name
    ensemble_intraday_v2b.pkl, preserving v2.pkl alongside.
  - The actual v1-vs-v2b backtest will be done via engine-replay
    (R11 harness), NOT via the R9-style shortcut backtest. So this
    file is train-only — no metrics computation, no in-sample
    backtest. Engine-replay produces the trustworthy numbers.

OUTPUT:
  models/saved/ensemble_intraday_v2b.pkl
  models/saved/ensemble_intraday_v2b.ubj   (XGBoost-portable booster)

NON-NEGOTIABLE:
  - DO NOT touch ensemble_intraday.pkl (production v1)
  - DO NOT touch ensemble_intraday_v2.pkl (R9 v2, preserved for forensics)
  - DO NOT change feature_engineer.py, config.py risk constants
  - Use existing engineer_features() pipeline as-is
"""
from __future__ import annotations

import logging
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from config import (
    DEFAULT_SYMBOLS,
    INTRADAY_LOOKAHEAD,
    INTRADAY_BUY_THRESHOLD,
    INTRADAY_SELL_THRESHOLD,
)
from data.database import load_ohlcv
from features.engineer import engineer_features
from models.ensemble import Ensemble

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
logger = logging.getLogger("retrain_v2b")

# Force utf-8 stdout so any progress prints with Rs / Δ symbols don't
# crash the Windows cp1252 console (same fix R9's recovery script
# applied; bug bit twice — now applied here proactively).
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── R12 window (shorter train so 2026-01-01 onward is clean OOS) ─────
TRAIN_START = pd.Timestamp("2024-01-01")           # effective ~2024-05-27 per DB
TRAIN_END_INCLUSIVE = pd.Timestamp("2025-12-31 23:59:59")

MODELS_DIR = ROOT / "models" / "saved"
V2B_PATH = MODELS_DIR / "ensemble_intraday_v2b.pkl"


def _slice_to_window(df: pd.DataFrame, start: pd.Timestamp,
                      end: pd.Timestamp) -> pd.DataFrame:
    """Inclusive [start, end] slice on a DatetimeIndex (tz-naive)."""
    if df.empty:
        return df
    return df.loc[(df.index >= start) & (df.index <= end)]


def _build_train_frame(symbols: list[str]) -> pd.DataFrame:
    """Same pattern as the R9 harness — engineer features per symbol
    on FULL history first (so rolling windows are warm), then slice
    to the TRAIN window."""
    frames = []
    for sym in symbols:
        raw = load_ohlcv(sym, resolution="5m")
        if raw.empty:
            logger.warning("%s: no 5m data — skipping", sym)
            continue
        if not isinstance(raw.index, pd.DatetimeIndex):
            raw.index = pd.to_datetime(raw.index)
        try:
            feat = engineer_features(raw.sort_index())
        except Exception as e:
            logger.warning("%s: engineer_features failed: %s", sym, e)
            continue
        train_slice = _slice_to_window(feat, TRAIN_START, TRAIN_END_INCLUSIVE)
        if len(train_slice) < 200:
            logger.warning("%s: only %d train bars — skipping", sym, len(train_slice))
            continue
        frames.append(train_slice)
        logger.info("  %s: %d train bars (%s -> %s)",
                    sym, len(train_slice),
                    train_slice.index.min(), train_slice.index.max())
    if not frames:
        raise RuntimeError(
            "no symbols had usable 5m data in the TRAIN window — "
            "check market_data.db population")
    return pd.concat(frames, ignore_index=False)


def main() -> int:
    notes = []
    wall_start = time.time()
    symbols = list(DEFAULT_SYMBOLS)

    logger.info("=== R12 v2b retrain ===")
    logger.info("Symbols: %d", len(symbols))
    logger.info("Train window: %s -> %s",
                TRAIN_START.date(), TRAIN_END_INCLUSIVE.date())
    logger.info("Holdout (for engine-replay, NOT touched here): "
                "2026-01-01 -> today")

    logger.info("Loading + engineering training data …")
    combined = _build_train_frame(symbols)
    logger.info("Combined train frame: %d bars across %d symbols",
                len(combined), len(symbols))

    logger.info("Training v2b ensemble (XGBoost + LSTM + HMM + meta-LR) …")
    fit_start = time.time()
    v2b = Ensemble()
    v2b.fit(combined,
            lookahead=INTRADAY_LOOKAHEAD,
            buy_threshold=INTRADAY_BUY_THRESHOLD,
            sell_threshold=INTRADAY_SELL_THRESHOLD,
            vol_scaled=True)
    fit_secs = time.time() - fit_start
    logger.info("v2b train wall-clock: %.0fs (%.1f min)",
                fit_secs, fit_secs / 60)
    notes.append(f"v2b train wall-clock: {fit_secs/60:.1f} min on CPU")

    # Save v2b — DO NOT touch ensemble_intraday.pkl OR
    # ensemble_intraday_v2.pkl. v2b lives alongside both.
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with open(V2B_PATH, "wb") as f:
        pickle.dump(v2b, f)
    logger.info("v2b saved to %s (%.2f MB)",
                V2B_PATH, V2B_PATH.stat().st_size / 1024 / 1024)

    # Also export the XGBoost booster in UBJ format (version-portable;
    # mirrors what Ensemble.save() does for the production artifact).
    try:
        ubj_path = V2B_PATH.with_suffix(".ubj")
        v2b.signal_layer.model.get_booster().save_model(str(ubj_path))
        logger.info("UBJ export -> %s", ubj_path)
    except Exception as e:
        logger.warning("UBJ export failed (pickle still saved): %s", e)

    wall_total = time.time() - wall_start
    logger.info("=== R12 v2b retrain done in %.0fs (%.1f min) ===",
                wall_total, wall_total / 60)
    print(f"v2b artifact: {V2B_PATH}")
    print(f"Train window: {TRAIN_START.date()} -> {TRAIN_END_INCLUSIVE.date()}")
    print(f"Notes: {notes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
