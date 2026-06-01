"""R15 — v3 conf-floor sweep.

Drives the engine-replay harness over the same OOS window as R14
(2026-01-01 -> 2026-05-28) for v3 at 4 lowered conf-floors:
``{0.50, 0.52, 0.54, 0.55}``.

WHY
===
R14 v3 replay produced 8 trades (same as v2b) but generated 83,151
conf_blocked signal candidates — 5.8x v2b. v3 conf distribution:
p50=0.405, p90=0.496, p99=0.578, max=0.600 — every additional
candidate strangled by the production 0.60 floor. At floor 0.55,
~2,449 candidates would pass the gate. This is a fundamentally
different shape from v2b (R13 Stage 2 showed v2b unlocked ~zero
extra trades at lower floors). Conf-floor is the v3 bottleneck;
this sweep quantifies it.

SHIP gate (unchanged from R14 brief):
    n_trades > 24  AND  PF > 1.3

If any floor clears, deploy is a TWO-axis change (ensemble.pkl swap
+ config.SIGNAL_MIN_CONFIDENCE change + matched rollback). Ops
makes that call; this script only runs the sweep + produces the
side-by-side report.

PREREQUISITES
=============
- ``models/saved/ensemble_intraday_v3.pkl`` (R14 retrain output).
- DB_PATH restore in run_replay finally (R13 hotfix, SHA efb9b3e) —
  required because this script calls run_replay 4 times in one
  process.

PRODUCTION SAFETY
=================
- ``ensemble_intraday.pkl`` (live v2b) UNTOUCHED.
- ``config.SIGNAL_MIN_CONFIDENCE`` UNCHANGED — sweep uses the
  per-call ``conf_floor_override`` kwarg.
- 4 isolated sandbox SQLite files under ``logs/``.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import DEFAULT_SYMBOLS  # noqa: E402
from models.engine_replay_backtest import run_replay  # noqa: E402

# ── Configuration ────────────────────────────────────────────────────

V3_PATH = PROJECT_ROOT / "models" / "saved" / "ensemble_intraday_v3.pkl"
LOG_PATH = PROJECT_ROOT / "logs" / "r15_conf_floor_sweep.log"
REPORT_PATH = PROJECT_ROOT / "logs" / "r15_v3_conf_floor_sweep.md"

HOLDOUT_START = pd.Timestamp("2026-01-01")
HOLDOUT_END = pd.Timestamp("2026-05-28")
PORTFOLIO_VALUE = 500_000.0

CONF_FLOORS = [0.50, 0.52, 0.54, 0.55]

SHIP_GATE_N_TRADES = 24
SHIP_GATE_PF = 1.3

# Cached baselines — DO NOT re-run; same window, same engine, deterministic.
# v1 + v2b from logs/r12_v1_vs_v2_engine_replay.md
# v3@0.60 from logs/r14_v3_scalper_replay.md
BASELINES = {
    "v1 @0.60 (R12)":  {"pf": 0.731,           "sharpe": -3.227, "win_rate": 0.500, "max_dd": 0.017,  "n_trades": 8},
    "v2b @0.60 (LIVE)": {"pf": float("inf"),    "sharpe": -2.602, "win_rate": 1.000, "max_dd": -0.000, "n_trades": 8},
    "v3 @0.60 (R14)":  {"pf": float("inf"),    "sharpe": -2.605, "win_rate": 1.000, "max_dd": -0.000, "n_trades": 8},
}

# ── Logging ──────────────────────────────────────────────────────────

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(name)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("r15_sweep")


def _telegram(msg: str) -> None:
    try:
        from alerts.telegram_bot import send_message  # type: ignore
        send_message(msg)
    except Exception as e:
        logger.warning("Telegram alert dropped: %s", e)


# ── Format helpers ───────────────────────────────────────────────────


def _fmt_pf(pf) -> str:
    if pf is None:
        return "-"
    if isinstance(pf, float) and pf != pf:
        return "-"
    if pf == float("inf"):
        return "infinity"
    return f"{pf:.3f}"


def _fmt(val, decimals: int = 3) -> str:
    if val is None:
        return "-"
    if isinstance(val, float) and val != val:
        return "-"
    return f"{val:.{decimals}f}"


def _is_ship(metrics: dict) -> bool:
    pf = metrics.get("profit_factor", 0.0)
    n = metrics.get("n_trades", 0)
    pf_ok = (pf == float("inf")) or (pf > SHIP_GATE_PF)
    return pf_ok and (n > SHIP_GATE_N_TRADES)


# ── Report ───────────────────────────────────────────────────────────


def _write_report(sweep_results: dict) -> tuple[bool, str | None]:
    """Returns (any_ship, best_floor_if_ship_else_None)."""
    ship_floors = [f for f, r in sweep_results.items() if _is_ship(r["metrics"])]
    any_ship = bool(ship_floors)
    best_floor = None
    if any_ship:
        def _key(f):
            m = sweep_results[f]["metrics"]
            pf = m["profit_factor"]
            pf_score = 1e9 if pf == float("inf") else pf
            return (pf_score, m["n_trades"])
        best_floor = max(ship_floors, key=_key)

    lines = []
    lines.append("# R15 — v3 conf-floor sweep report")
    lines.append("")
    lines.append(f"**Holdout window:** {HOLDOUT_START.date()} -> {HOLDOUT_END.date()} (same as R12/R14, OOS for v3)")
    lines.append(f"**Symbols:** {len(DEFAULT_SYMBOLS)} (DEFAULT_SYMBOLS)")
    lines.append("**Engine code:** R7-A+B + P30 + P28 + P50 (production as of R15)")
    lines.append(f"**Ensemble:** `ensemble_intraday_v3.pkl` (R14 scalper retrain)")
    lines.append(f"**Floors swept:** {CONF_FLOORS}")
    lines.append(f"**SHIP gate:** n_trades > {SHIP_GATE_N_TRADES} AND PF > {SHIP_GATE_PF}")
    lines.append("")
    lines.append("## TL;DR — SHIP VERDICT")
    lines.append("")
    if any_ship:
        m = sweep_results[best_floor]["metrics"]
        lines.append(f"**SHIP v3 @ floor {best_floor:.2f}**")
        lines.append("")
        lines.append(f"_n_trades={m['n_trades']}, PF={_fmt_pf(m['profit_factor'])}, win_rate={_fmt(m['win_rate'])}. Both gates cleared._")
    else:
        lines.append("**NO_SHIP — no floor cleared both gates**")
        lines.append("")
        gate_outcomes = []
        for f in CONF_FLOORS:
            m = sweep_results[f]["metrics"]
            pf = m["profit_factor"]
            n = m["n_trades"]
            gate_outcomes.append(f"floor {f:.2f}: n_trades={n}, PF={_fmt_pf(pf)}")
        lines.append("_" + " | ".join(gate_outcomes) + "_")
    lines.append("")

    lines.append("## Full comparison — same window, same engine, same gates")
    lines.append("")
    lines.append("| Model @ conf-floor | PF | Sharpe | Win rate | Max DD | n_trades |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for name, b in BASELINES.items():
        lines.append(
            f"| {name} | {_fmt_pf(b['pf'])} | {_fmt(b['sharpe'])} | "
            f"{_fmt(b['win_rate'])} | {_fmt(b['max_dd'])} | {b['n_trades']} |")
    for f in CONF_FLOORS:
        m = sweep_results[f]["metrics"]
        flag = " **← SHIP**" if _is_ship(m) and f == best_floor else (" (SHIP)" if _is_ship(m) else "")
        lines.append(
            f"| v3 @{f:.2f} (R15){flag} | {_fmt_pf(m['profit_factor'])} | "
            f"{_fmt(m['sharpe'])} | {_fmt(m['win_rate'])} | "
            f"{_fmt(m['max_drawdown'])} | {m['n_trades']} |")
    lines.append("")
    lines.append("_v1, v2b@0.60, v3@0.60 baselines pulled from R12/R14 reports — same window, same engine, deterministic._")
    lines.append("")

    lines.append("## Sweep metadata")
    lines.append("")
    lines.append("| Floor | ticks | symbol-evals | wall_secs | sandbox db | block log |")
    lines.append("|---:|---:|---:|---:|---|---|")
    for f in CONF_FLOORS:
        r = sweep_results[f]
        lines.append(
            f"| {f:.2f} | {r.get('n_ticks', '-')} | {r.get('n_symbol_evaluations', '-')} | "
            f"{_fmt(r.get('wall_clock_secs'), 1)} | "
            f"`{Path(r['sandbox_db_path']).name}` | "
            f"`r15_block_reasons_f{int(round(f*100)):02d}.csv` |")
    lines.append("")

    for f in CONF_FLOORS:
        r = sweep_results[f]
        lines.append(f"## v3 @ floor {f:.2f} — per-symbol PF")
        lines.append("")
        per = r.get("per_symbol", {})
        if per:
            lines.append("| Symbol | n_trades | PF | win_rate |")
            lines.append("|--------|---:|---:|---:|")
            for sym in sorted(per):
                s = per[sym]
                lines.append(f"| {sym} | {s.get('n_trades', 0)} | {_fmt_pf(s.get('pf'))} | {_fmt(s.get('win_rate'))} |")
        else:
            lines.append("_(per-symbol breakdown unavailable)_")
        lines.append("")

    lines.append("## Recommendation")
    lines.append("")
    if any_ship:
        m = sweep_results[best_floor]["metrics"]
        lines.append(f"v3 @ floor {best_floor:.2f} cleared both R14/R15 SHIP gates (n_trades={m['n_trades']} > {SHIP_GATE_N_TRADES}, PF={_fmt_pf(m['profit_factor'])} > {SHIP_GATE_PF}).")
        lines.append("")
        lines.append("This is a **two-axis deploy** (model swap + config change). Ops procedure (before 09:10 IST):")
        lines.append("```")
        lines.append("# 1. Backup live state")
        lines.append("cp models/saved/ensemble_intraday.pkl   models/saved/ensemble_intraday_v2b_pre_r15_backup.pkl")
        lines.append("cp config.py                            config.py.pre_r15_backup")
        lines.append("# 2. Swap ensemble")
        lines.append("cp models/saved/ensemble_intraday_v3.pkl models/saved/ensemble_intraday.pkl")
        lines.append(f"# 3. Edit config.py: SIGNAL_MIN_CONFIDENCE = {best_floor:.2f}")
        lines.append("```")
        lines.append("Rollback if v3@floor misbehaves in-session:")
        lines.append("```")
        lines.append("cp models/saved/ensemble_intraday_v2b_pre_r15_backup.pkl models/saved/ensemble_intraday.pkl")
        lines.append("cp config.py.pre_r15_backup config.py")
        lines.append("```")
        lines.append("(Watchdog auto-picks up at next 5-min cycle.)")
    else:
        lines.append("No swept floor cleared the SHIP gate. Both axes (label density + conf-floor) have now been ruled out as single-knob fixes for the trade-count under-generation observed since R12.")
        lines.append("")
        lines.append("Next R16 candidates worth ops discussion (not implemented):")
        lines.append("1. Regime-blocked dominates after conf is relaxed — investigate whether v3's lower-confidence signals are also regime-blocked (revisit `RegimeLayer` weights vs the scalper distribution).")
        lines.append("2. Reduce `INTRADAY_LOOKAHEAD` further (3 -> 2) to predict 10-min moves directly.")
        lines.append("3. Re-examine the daily_count_cap (P28) since lower-confidence sub-trades may now be hitting the cap earlier in-session.")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Report written -> %s (ANY_SHIP=%s, best=%s)",
                REPORT_PATH, any_ship, best_floor)
    return any_ship, best_floor


# ── Main ─────────────────────────────────────────────────────────────


def main() -> int:
    logger.info("=== R15 v3 conf-floor sweep ===")
    logger.info("Holdout: %s -> %s", HOLDOUT_START.date(), HOLDOUT_END.date())
    logger.info("Floors: %s", CONF_FLOORS)
    logger.info("Symbols: %d", len(DEFAULT_SYMBOLS))
    if not V3_PATH.exists():
        logger.error("v3.pkl missing at %s", V3_PATH)
        _telegram(f"[R15] ABORT — v3.pkl missing at {V3_PATH}")
        return 1

    _telegram(
        f"[R15 v3 conf-floor sweep] kickoff — floors={CONF_FLOORS}, "
        f"window {HOLDOUT_START.date()} -> {HOLDOUT_END.date()}, "
        f"ETA ~{len(CONF_FLOORS) * 30} min.")

    sweep_results: dict[float, dict] = {}
    wall_total = time.time()

    for i, floor in enumerate(CONF_FLOORS, start=1):
        floor_tag = f"f{int(round(floor * 100)):02d}"
        sandbox = PROJECT_ROOT / "logs" / f"r15_v3_sandbox_{floor_tag}.db"
        block_log = PROJECT_ROOT / "logs" / f"r15_block_reasons_{floor_tag}.csv"
        if sandbox.exists():
            sandbox.unlink()
        logger.info("--- [%d/%d] floor=%.2f ---", i, len(CONF_FLOORS), floor)
        t0 = time.time()
        try:
            result = run_replay(
                symbols=DEFAULT_SYMBOLS,
                holdout_start=HOLDOUT_START,
                holdout_end=HOLDOUT_END,
                ensemble_path=V3_PATH,
                sandbox_db_path=sandbox,
                portfolio_value=PORTFOLIO_VALUE,
                progress_every=400,
                block_log_path=block_log,
                conf_floor_override=floor,
            )
        except Exception as e:
            logger.exception("Floor %.2f CRASHED: %s", floor, e)
            _telegram(f"[R15] floor {floor:.2f} CRASHED: {type(e).__name__}: {e}")
            return 2
        elapsed = time.time() - t0
        logger.info("Floor %.2f done in %.0fs (%.1f min)  metrics=%s",
                    floor, elapsed, elapsed / 60, result["metrics"])
        sweep_results[floor] = result

    total_secs = time.time() - wall_total
    logger.info("=== sweep done in %.0fs (%.1f min) ===",
                total_secs, total_secs / 60)

    any_ship, best = _write_report(sweep_results)

    if any_ship:
        m = sweep_results[best]["metrics"]
        pf_str = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.3f}"
        _telegram(
            f"[R15 v3 conf-floor sweep] SHIP @ floor {best:.2f} — "
            f"n_trades={m['n_trades']}, PF={pf_str}. "
            f"Two-axis deploy; ops review report {REPORT_PATH.name}.")
    else:
        summary = ", ".join(
            f"{f:.2f}=n{sweep_results[f]['metrics']['n_trades']}"
            for f in CONF_FLOORS)
        _telegram(
            f"[R15 v3 conf-floor sweep] NO_SHIP — {summary}. "
            f"v2b stays live. Report {REPORT_PATH.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
