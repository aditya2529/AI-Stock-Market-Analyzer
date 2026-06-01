"""R14 — retrain v3 ("scalper" attempt) with aggressive label-frequency
params on top of v2b's architecture.

WHY v3 EXISTS:
  R13 Stage 2 proved that lowering the engine's conf-floor (0.60 → 0.55)
  did NOT add trades — the bottleneck is NOT the inference gate. v2b
  structurally under-generates BUY signals because it was TRAINED to
  predict big 30-min moves (lookahead=6, ±0.5×sigma vol-scaled cutoff),
  which are rare. R14 retrains the MODEL itself to hunt smaller, more
  frequent moves.

WHAT'S DIFFERENT FROM v2b (architecture + features identical):
  lookahead:    6  → 3   (predict 15-min forward instead of 30-min)
  sigma_mult:   0.5 → 0.25   (R14 audit-fix kwarg added to make_labels +
                              Ensemble.fit — tightens vol-scaled cutoff
                              ~2-3× label density without dropping below
                              noise floor)
  vol_scaled:   True (UNCHANGED — preserves Q1 noise-floor protection)
  Train window: 2024-05-27 → 2025-12-31 (same as v2b — full available)

WHY THIS MIGHT WORK:
  Shorter horizon + tighter cutoff → many more BUY/SELL training labels
  → model learns to fire more often. If the new labels also generalise
  to engine-replay's OOS window with PF > 1.3 AND n_trades > 24, R14
  ships v3 to production. Otherwise, ops pivots to swing.

FORENSIC ONLY UNTIL SIGN-OFF:
  v3.pkl saves to a separate artifact path. Engine continues running
  v2b. ensemble_intraday.pkl stays bit-for-bit v2b. v3 SHIPs only after
  the engine-replay report + ops review confirm the SHIP gate
  (n_trades > 24 AND PF > 1.3).

OUTPUT:
  models/saved/ensemble_intraday_v3.pkl
  models/saved/ensemble_intraday_v3.ubj    (XGBoost-portable booster)
  logs/r14_v3_memory_trace.csv             (RAM trace, 30s cadence)

RAM MONITOR (kept from R12 retrain pattern — proven safe at 8 GB).
Known blocker: LSTM-phase silent crash on Windows after 15-25 min
sustained CPU (killed v2b#1 + v2c). RAM monitor + Telegram crash alert
preserved. Retry once if it dies; cap epochs if it dies twice at the
same spot.

NON-NEGOTIABLE:
  - DO NOT touch ensemble_intraday.pkl     (production v2b — LIVE engine reads this)
  - DO NOT touch ensemble_intraday_v2b.pkl (R12 v2b, preserved for forensics)
  - DO NOT touch ensemble_intraday_v2.pkl  (R9 v2, preserved for forensics)
  - DO NOT touch ensemble_intraday_v2c.pkl (R13 v2c attempt, preserved)
  - DO NOT change features/engineer.py, config.py risk constants
  - Use existing engineer_features() pipeline as-is
  - DO NOT auto-deploy v3 — ops decides after replay report lands
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

from config import DEFAULT_SYMBOLS
# R14 deliberately does NOT import INTRADAY_LOOKAHEAD /
# INTRADAY_BUY_THRESHOLD / INTRADAY_SELL_THRESHOLD — v3 uses scalper-
# specific overrides hardcoded below. config.py global constants stay
# untouched so the live engine's inference path keeps reading 0.60 etc.
from data.database import load_ohlcv
from features.engineer import engineer_features
from models.ensemble import Ensemble

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
logger = logging.getLogger("retrain_v3")

# Force utf-8 stdout so any progress prints with Rs / Δ symbols don't
# crash the Windows cp1252 console (same fix R9's recovery script
# applied; bug bit twice — now applied here proactively).
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── R14 v3 window + label-frequency params ──────────────────────────
TRAIN_START = pd.Timestamp("2024-01-01")           # effective ~2024-05-27 per DB
TRAIN_END_INCLUSIVE = pd.Timestamp("2025-12-31 23:59:59")  # same as v2b
# OOS for the post-retrain replay = 2026-01-01 → today

# v3 scalper params (vs v2b):
V3_LOOKAHEAD = 3       # 15-min forward (was 6 = 30-min for v2b)
V3_SIGMA_MULT = 0.25   # tighter vol-scaled cutoff (was 0.5 for v2b)
V3_VOL_SCALED = True   # UNCHANGED — keeps the Q1 noise-floor protection

MODELS_DIR = ROOT / "models" / "saved"
V3_PATH = MODELS_DIR / "ensemble_intraday_v3.pkl"

# RAM monitor config (kept from R12 v2b retrain pattern)
MEM_TRACE_PATH = ROOT / "logs" / "r14_v3_memory_trace.csv"
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
                msg = (f"[R14 v3 retrain] RAM ALERT: process RSS "
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

    logger.info("=== R14 v3 'scalper' retrain (forensic until SHIP-gate clears) ===")
    logger.info("Params: lookahead=%d, sigma_mult=%.2f, vol_scaled=%s",
                V3_LOOKAHEAD, V3_SIGMA_MULT, V3_VOL_SCALED)
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
        logger.info("Training v3 ensemble (XGBoost + LSTM + HMM + meta-LR) "
                    "with scalper params …")
        fit_start = time.time()
        v3 = Ensemble()
        v3.fit(combined,
                lookahead=V3_LOOKAHEAD,
                vol_scaled=V3_VOL_SCALED,
                sigma_mult=V3_SIGMA_MULT)
        fit_secs = time.time() - fit_start
        logger.info("v3 train wall-clock: %.0fs (%.1f min)",
                    fit_secs, fit_secs / 60)
        notes.append(f"v3 train wall-clock: {fit_secs/60:.1f} min on CPU")

        phase_ref["phase"] = "save"
        # Save v3 — DO NOT touch ensemble_intraday.pkl (LIVE v2b), OR
        # ensemble_intraday_v2b.pkl, OR ensemble_intraday_v2.pkl, OR
        # ensemble_intraday_v2c.pkl. v3 lives alongside all of them and
        # only deploys after ops approves the engine-replay report.
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        with open(V3_PATH, "wb") as f:
            pickle.dump(v3, f)
        logger.info("v3 saved to %s (%.2f MB)",
                    V3_PATH, V3_PATH.stat().st_size / 1024 / 1024)

        # Also export the XGBoost booster in UBJ format (version-portable;
        # mirrors what Ensemble.save() does for the production artifact).
        try:
            ubj_path = V3_PATH.with_suffix(".ubj")
            v3.signal_layer.model.get_booster().save_model(str(ubj_path))
            logger.info("UBJ export -> %s", ubj_path)
        except Exception as e:
            logger.warning("UBJ export failed (pickle still saved): %s", e)

        phase_ref["phase"] = "done"
        wall_total = time.time() - wall_start
        logger.info("=== R14 v3 retrain done in %.0fs (%.1f min) ===",
                    wall_total, wall_total / 60)
        _telegram_alert(
            f"[R14 v3 retrain] SUCCESS — wall-clock {wall_total/60:.1f} min. "
            f"v3.pkl saved. Next: engine-replay 2026-01-01 → 2026-05-28.")
        print(f"v3 artifact: {V3_PATH}")
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
        msg = (f"[R14 v3 retrain] CRASHED at phase={phase_at_death} "
                f"after {wall_at_death/60:.1f} min: "
                f"{type(e).__name__}: {str(e)[:200]}")
        logger.error(msg)
        _telegram_alert(msg)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
