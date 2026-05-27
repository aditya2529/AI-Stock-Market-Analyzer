"""Recover the R9 backtest report after the 17:16 IST crash.

The audit team's models/retrain_and_backtest_v2.py completed the full
retrain (v2.pkl saved) AND ran the v1/v2 holdout backtests, but crashed
on the FINAL stdout print because Windows cp1252 can't encode Δ. The
report files (retrain_v2_summary.md, retrain_v2_backtest_report.json)
were never written because that code came AFTER the broken print.

This recovery script:
  - Loads v1.pkl + v2.pkl (both already exist)
  - Re-runs ONLY the holdout backtest (~5-10 min CPU)
  - Writes the JSON + MD reports using utf-8 (which handles Δ fine)
  - Skips the broken stdout print

After this finishes, scripts/auto_ship_r9.py can read the report
normally and make the SHIP decision.
"""
from __future__ import annotations
import json
import pickle
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from models.retrain_and_backtest_v2 import (
    _build_holdout_slices,
    _backtest_model_on_holdout,
    _format_md_table,
    DEFAULT_SYMBOLS,
    V1_PATH,
    V2_PATH,
    JSON_REPORT,
    MD_REPORT,
    TRAIN_START,
    TRAIN_END_INCLUSIVE,
    HOLDOUT_START,
    HOLDOUT_END_INCLUSIVE,
    CONFIDENCE_FLOOR,
)


def main() -> int:
    wall_start = time.time()
    print("R9 report recovery — loading models + re-running holdout backtest")

    if not V1_PATH.exists() or not V2_PATH.exists():
        print(f"ABORT: v1 ({V1_PATH.exists()}) or v2 ({V2_PATH.exists()}) missing")
        return 1

    with open(V1_PATH, "rb") as f:
        v1 = pickle.load(f)
    with open(V2_PATH, "rb") as f:
        v2 = pickle.load(f)
    print("  v1 + v2 loaded")

    holdout = _build_holdout_slices(DEFAULT_SYMBOLS)
    print(f"  holdout slices: {len(holdout)} symbols ready")

    print("  running v1 backtest ...")
    v1_report = _backtest_model_on_holdout("v1", v1, holdout)
    print("  running v2 backtest ...")
    v2_report = _backtest_model_on_holdout("v2", v2, holdout)

    v1_pf = v1_report["metrics"].get("profit_factor")
    v2_pf = v2_report["metrics"].get("profit_factor")
    gate = "UNKNOWN"
    if isinstance(v2_pf, (int, float)) and v2_pf == v2_pf:
        if v2_pf >= 1.3 and (not isinstance(v1_pf, (int, float)) or v2_pf > v1_pf):
            gate = "SHIP v2"
        elif v2_pf >= 1.3:
            gate = "OPS DECIDES (v2 PF >= 1.3 but <= v1)"
        else:
            gate = "KEEP v1 — ship A+B only"

    report = {
        "generated_at": datetime.now().isoformat(),
        "recovery_run": True,
        "train_window": [str(TRAIN_START.date()), str(TRAIN_END_INCLUSIVE.date())],
        "holdout_window": [str(HOLDOUT_START.date()), str(HOLDOUT_END_INCLUSIVE.date())],
        "confidence_floor": CONFIDENCE_FLOOR,
        "v1": v1_report,
        "v2": v2_report,
        "gate_verdict": gate,
        "wall_clock_secs": time.time() - wall_start,
        "notes": ["Recovered after crash in retrain_and_backtest_v2.py main() "
                  "print() at line 412 (Windows cp1252 Δ encoding error)."],
    }
    JSON_REPORT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    MD_REPORT.write_text(_format_md_table(v1_report, v2_report)
                          + f"\n\n**Gate verdict:** {gate}\n",
                          encoding="utf-8")
    print(f"\n  JSON -> {JSON_REPORT}")
    print(f"  MD   -> {MD_REPORT}")
    print(f"\n  v1 PF: {v1_pf}")
    print(f"  v2 PF: {v2_pf}")
    print(f"  Gate : {gate}")
    print(f"\nDONE in {time.time() - wall_start:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
