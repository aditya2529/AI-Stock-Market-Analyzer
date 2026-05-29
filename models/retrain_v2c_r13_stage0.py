"""R13 Stage 0 — retrain v2c with TRAIN_END = 2025-06-30 so
2025-07-01 → today is a clean ~11-month OOS window for honest
per-symbol PF baseline.

WHY v2c EXISTS (forensic only, NEVER deploys):
  - v2b's R12 OOS window (2026-01-01 → today, 5 months) produced
    only 8 trades total across 25 symbols — too thin to anchor
    R13's per-symbol favorites filter on.
  - v2c is the SAME architecture + features as v2b but trained on
    less data, so it produces a parallel forensic artifact whose
    OOS window is wider (~11 months) → meaningful trade-count
    signal per symbol.
  - v2c is NOT a model candidate for production. Engine continues
    running v2b (deployed mid-session today, PID 3164). v2c lives
    at a separate artifact path; ensemble_intraday.pkl stays v2b
    bit-for-bit.
  - The Stage 0 replay output anchors Stages 1-4 of R13 (favorites
    filter, conf sweep, sector momentum) and the revised SHIP gate
    (Path D PF > v2c-baseline PF + 0.10).

OUTPUT:
  models/saved/ensemble_intraday_v2c.pkl
  models/saved/ensemble_intraday_v2c.ubj   (XGBoost-portable booster)
  logs/r13_v2c_memory_trace.csv            (RAM trace, 30s cadence)

RAM MONITOR (kept from R12 retrain pattern — proven safe at 8 GB).

NON-NEGOTIABLE:
  - DO NOT touch ensemble_intraday.pkl     (production v2b — LIVE engine reads this)
  - DO NOT touch ensemble_intraday_v2b.pkl (R12 v2b, preserved for forensics)
  - DO NOT touch ensemble_intraday_v2.pkl  (R9 v2, preserved for forensics)
  - DO NOT change feature_engineer.py, config.py risk constants
  - Use existing engineer_features() pipeline as-is
"""
from __future__ import annotations

import logging
import os
import pickle
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import psutil

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
logger = logging.getLogger("retrain_v2c")

# Force utf-8 stdout so any progress prints with Rs / Δ symbols don't
# crash the Windows cp1252 console (same fix R9's recovery script
# applied; bug bit twice — now applied here proactively).
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── R13 Stage 0 window (shorter train so ~11 months OOS for v2c) ─────
TRAIN_START = pd.Timestamp("2024-01-01")           # effective ~2024-05-27 per DB
TRAIN_END_INCLUSIVE = pd.Timestamp("2025-06-30 23:59:59")

MODELS_DIR = ROOT / "models" / "saved"
V2C_PATH = MODELS_DIR / "ensemble_intraday_v2c.pkl"

# RAM monitor config (kept from R12 v2b retrain pattern)
MEM_TRACE_PATH = ROOT / "logs" / "r13_v2c_memory_trace.csv"
MEM_POLL_SECS = 30
MEM_ALERT_RSS_GB = 6.5    # Telegram alert if process RSS > this


def _telegram_alert(text: str) -> None:
    """Fire-and-forget Telegram alert. Never raises — failure to alert
    must NOT crash the retrain."""
    try:
        from alerts import telegram_bot
        telegram_bot.send_message(text)
    except Exception as e:
        logger.warning("telegram alert failed (non-fatal): %s", e)


def _start_ram_monitor(phase_ref: dict) -> threading.Thread:
    """Spawn a daemon thread that polls process RSS every
    MEM_POLL_SECS and appends a row to MEM_TRACE_PATH. phase_ref is
    a dict the caller mutates to label the current phase (data-load,
    xgboost, lstm, hmm, meta, save) so the trace tags peaks to
    the right place.

    Fires a one-shot Telegram alert the first time RSS crosses
    MEM_ALERT_RSS_GB so the operator gets early warning.
    """
    MEM_TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEM_TRACE_PATH.write_text(
        "timestamp,elapsed_sec,rss_mb,available_mb,phase\n",
        encoding="utf-8",
    )
    pid = os.getpid()
    proc = psutil.Process(pid)
    start = time.time()
    alerted_high_rss = {"yes": False}

    def _poll():
        while True:
            try:
                rss = proc.memory_info().rss
                avail = psutil.virtual_memory().available
            except psutil.NoSuchProcess:
                return
            except Exception as e:
                logger.warning("ram monitor poll failed: %s", e)
                time.sleep(MEM_POLL_SECS)
                continue
            elapsed = time.time() - start
            rss_mb = rss / 1024 / 1024
            avail_mb = avail / 1024 / 1024
            line = (f"{time.strftime('%Y-%m-%dT%H:%M:%S')},"
                    f"{elapsed:.0f},"
                    f"{rss_mb:.1f},"
                    f"{avail_mb:.1f},"
                    f"{phase_ref.get('phase', 'unknown')}\n")
            try:
                with open(MEM_TRACE_PATH, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
            except Exception as e:
                logger.warning("ram monitor write failed: %s", e)

            if (not alerted_high_rss["yes"]
                    and rss_mb / 1024 > MEM_ALERT_RSS_GB):
                alerted_high_rss["yes"] = True
                msg = (f"[R13 v2c retrain] RAM ALERT: process RSS "
                        f"{rss_mb:.0f} MB > {MEM_ALERT_RSS_GB*1024:.0f} MB "
                        f"threshold. Phase: {phase_ref.get('phase')}. "
                        f"OOM-kill is imminent — consider freeing "
                        f"other apps NOW.")
                logger.warning(msg)
                _telegram_alert(msg)

            time.sleep(MEM_POLL_SECS)

    th = threading.Thread(target=_poll, daemon=True, name="ram_monitor")
    th.start()
    return th


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

    # ── Start RAM monitor ────────────────────────────────────────────
    # phase_ref is a dict the monitor reads; we mutate phase_ref["phase"]
    # before each stage so the CSV trace tags peaks to the right step.
    phase_ref = {"phase": "startup"}
    _start_ram_monitor(phase_ref)
    logger.info("RAM monitor running (trace -> %s, poll %ds, alert >%.1f GB)",
                MEM_TRACE_PATH, MEM_POLL_SECS, MEM_ALERT_RSS_GB)

    logger.info("=== R13 Stage 0 v2c retrain (forensic, never deploys) ===")
    logger.info("Symbols: %d", len(symbols))
    logger.info("Train window: %s -> %s",
                TRAIN_START.date(), TRAIN_END_INCLUSIVE.date())
    logger.info("Holdout (for engine-replay, NOT touched here): "
                "2026-01-01 -> today")

    try:
        phase_ref["phase"] = "data_load"
        logger.info("Loading + engineering training data …")
        combined = _build_train_frame(symbols)
        logger.info("Combined train frame: %d bars across %d symbols",
                    len(combined), len(symbols))

        phase_ref["phase"] = "ensemble_fit"
        logger.info("Training v2c ensemble (XGBoost + LSTM + HMM + meta-LR) …")
        fit_start = time.time()
        v2c = Ensemble()
        v2c.fit(combined,
                lookahead=INTRADAY_LOOKAHEAD,
                buy_threshold=INTRADAY_BUY_THRESHOLD,
                sell_threshold=INTRADAY_SELL_THRESHOLD,
                vol_scaled=True)
        fit_secs = time.time() - fit_start
        logger.info("v2c train wall-clock: %.0fs (%.1f min)",
                    fit_secs, fit_secs / 60)
        notes.append(f"v2c train wall-clock: {fit_secs/60:.1f} min on CPU")

        phase_ref["phase"] = "save"
        # Save v2c — DO NOT touch ensemble_intraday.pkl (LIVE v2b), OR
        # ensemble_intraday_v2b.pkl, OR ensemble_intraday_v2.pkl. v2c
        # lives alongside all of them, never deploys.
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        with open(V2C_PATH, "wb") as f:
            pickle.dump(v2c, f)
        logger.info("v2c saved to %s (%.2f MB)",
                    V2C_PATH, V2C_PATH.stat().st_size / 1024 / 1024)

        # Also export the XGBoost booster in UBJ format (version-portable;
        # mirrors what Ensemble.save() does for the production artifact).
        try:
            ubj_path = V2C_PATH.with_suffix(".ubj")
            v2c.signal_layer.model.get_booster().save_model(str(ubj_path))
            logger.info("UBJ export -> %s", ubj_path)
        except Exception as e:
            logger.warning("UBJ export failed (pickle still saved): %s", e)

        phase_ref["phase"] = "done"
        wall_total = time.time() - wall_start
        logger.info("=== R13 Stage 0 v2c retrain done in %.0fs (%.1f min) ===",
                    wall_total, wall_total / 60)
        _telegram_alert(
            f"[R13 Stage 0 v2c retrain] SUCCESS — wall-clock {wall_total/60:.1f} min. "
            f"v2c.pkl saved. Next: engine-replay 2025-07-01 → 2026-05-28.")
        print(f"v2c artifact: {V2C_PATH}")
        print(f"Train window: {TRAIN_START.date()} -> {TRAIN_END_INCLUSIVE.date()}")
        print(f"Notes: {notes}")
        return 0
    except BaseException as e:
        # Catch even KeyboardInterrupt / SystemExit so we get the
        # Telegram alert before the process dies. BaseException is
        # the right base for this — Exception alone misses SystemExit
        # / KeyboardInterrupt that propagate through PyTorch's training
        # loop.
        phase_at_death = phase_ref.get("phase", "unknown")
        wall_at_death = time.time() - wall_start
        msg = (f"[R13 v2c retrain] CRASHED at phase={phase_at_death} "
                f"after {wall_at_death/60:.1f} min: "
                f"{type(e).__name__}: {str(e)[:200]}")
        logger.error(msg)
        _telegram_alert(msg)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
