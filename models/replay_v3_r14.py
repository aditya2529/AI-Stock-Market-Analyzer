"""R14 — v3 'scalper' engine-replay driver.

Runs the freshly-trained v3 ensemble (lookahead=3, sigma_mult=0.25,
vol_scaled=True) through the engine-replay harness over the same OOS
window used for R12's v1/v2b comparison: 2026-01-01 -> 2026-05-28.

SHIP gate (per ops R14 brief):
    n_trades > 24  AND  PF > 1.3

If both clear, v3 is a SHIP candidate (deploy decision still ops-only).
Otherwise v3 stays forensic; v2b remains live.

v1 and v2b baseline numbers are pulled verbatim from
``logs/r12_v1_vs_v2_engine_replay.md`` — same window, same engine, same
gates, deterministic. Re-running them would burn ~80 min for zero
information gain.

Production safety:
  * v3 lives at ``models/saved/ensemble_intraday_v3.pkl`` — not the
    path the live engine reads.
  * Sandbox SQLite DB is ``logs/r14_v3_sandbox.db`` — isolated from
    market_data.db and paper_trading.db.
  * No edits to config.py, intraday/engine.py, features/engineer.py,
    or the live ensemble.pkl.
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
SANDBOX_DB = PROJECT_ROOT / "logs" / "r14_v3_sandbox.db"
BLOCK_LOG = PROJECT_ROOT / "logs" / "r14_v3_block_reasons.csv"
REPORT_PATH = PROJECT_ROOT / "logs" / "r14_v3_scalper_replay.md"
LOG_PATH = PROJECT_ROOT / "logs" / "r14_v3_replay.log"

HOLDOUT_START = pd.Timestamp("2026-01-01")
HOLDOUT_END = pd.Timestamp("2026-05-28")
PORTFOLIO_VALUE = 500_000.0

# SHIP gate (R14 brief)
SHIP_GATE_N_TRADES = 24
SHIP_GATE_PF = 1.3

# R12 baselines — verbatim from logs/r12_v1_vs_v2_engine_replay.md
R12_BASELINES = {
    "v1":  {"pf": 0.731,     "sharpe": -3.227, "win_rate": 0.500, "max_dd": 0.017,  "n_trades": 8},
    "v2b": {"pf": float("inf"), "sharpe": -2.602, "win_rate": 1.000, "max_dd": -0.000, "n_trades": 8},
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
logger = logging.getLogger("replay_v3")


def _telegram_alert(msg: str) -> None:
    """Best-effort Telegram alert; never raises into the replay loop."""
    try:
        from alerts.dispatcher import send_text  # type: ignore
        send_text(msg)
    except Exception as e:
        logger.warning("Telegram alert dropped: %s", e)


# ── Report ───────────────────────────────────────────────────────────


def _fmt_pf(pf) -> str:
    if pf is None:
        return "-"
    if isinstance(pf, float) and pf != pf:  # NaN
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


def _ship_verdict(metrics: dict) -> tuple[bool, str]:
    n_trades = metrics.get("n_trades", 0)
    pf = metrics.get("profit_factor", 0.0)
    pf_pass = (pf == float("inf")) or (pf > SHIP_GATE_PF)
    trades_pass = n_trades > SHIP_GATE_N_TRADES
    ship = pf_pass and trades_pass
    if ship:
        verdict = "**SHIP v3**"
    elif trades_pass and not pf_pass:
        verdict = "**NO_SHIP — trade-count gate cleared but PF gate failed**"
    elif pf_pass and not trades_pass:
        verdict = "**NO_SHIP — PF gate cleared but trade-count gate failed**"
    else:
        verdict = "**NO_SHIP — both gates failed**"
    return ship, verdict


def _write_report(result: dict) -> None:
    metrics = result["metrics"]
    ship, verdict_line = _ship_verdict(metrics)

    n_trades_v3 = metrics.get("n_trades", 0)
    pf_v3 = metrics.get("profit_factor", float("nan"))

    lines = []
    lines.append("# R14 — v3 'scalper' engine-replay report")
    lines.append("")
    lines.append(f"**Holdout window:** {HOLDOUT_START.date()} -> {HOLDOUT_END.date()} (OOS for v3; same window as R12 v1/v2b)")
    lines.append(f"**Symbols:** {len(DEFAULT_SYMBOLS)} (DEFAULT_SYMBOLS)")
    lines.append("**Engine code:** R7-A+B + P30 + P28 + P50 (production as of R14)")
    lines.append("**v3 training params:** lookahead=3, sigma_mult=0.25, vol_scaled=True, train 2024-05-27 -> 2025-12-31")
    lines.append("")
    lines.append("## TL;DR — SHIP VERDICT")
    lines.append("")
    lines.append(verdict_line)
    lines.append("")
    lines.append(f"_SHIP gate (R14 brief): n_trades > {SHIP_GATE_N_TRADES} AND PF > {SHIP_GATE_PF}._")
    lines.append(f"_v3 result: n_trades={n_trades_v3}, PF={_fmt_pf(pf_v3)}._")
    lines.append("")
    lines.append("## v1 vs v2b vs v3 — same holdout, same engine code, same gates")
    lines.append("")
    lines.append("| Metric        | v1 (production) | v2b (R12 LIVE) | v3 (R14 scalper) |")
    lines.append("|---------------|----------------:|---------------:|-----------------:|")
    lines.append(f"| Profit Factor | {_fmt_pf(R12_BASELINES['v1']['pf'])} | {_fmt_pf(R12_BASELINES['v2b']['pf'])} | {_fmt_pf(pf_v3)} |")
    lines.append(f"| Sharpe        | {_fmt(R12_BASELINES['v1']['sharpe'])} | {_fmt(R12_BASELINES['v2b']['sharpe'])} | {_fmt(metrics.get('sharpe'))} |")
    lines.append(f"| Win rate      | {_fmt(R12_BASELINES['v1']['win_rate'])} | {_fmt(R12_BASELINES['v2b']['win_rate'])} | {_fmt(metrics.get('win_rate'))} |")
    lines.append(f"| Max drawdown  | {_fmt(R12_BASELINES['v1']['max_dd'])} | {_fmt(R12_BASELINES['v2b']['max_dd'])} | {_fmt(metrics.get('max_drawdown'))} |")
    lines.append(f"| n_trades      | {R12_BASELINES['v1']['n_trades']} | {R12_BASELINES['v2b']['n_trades']} | {n_trades_v3} |")
    lines.append("")
    lines.append("_v1/v2b numbers are pulled verbatim from logs/r12_v1_vs_v2_engine_replay.md (same window, same engine, deterministic — re-running would burn ~80 min for zero information gain)._")
    lines.append("")
    lines.append("## v3 replay metadata")
    lines.append("")
    lines.append("|  | value |")
    lines.append("|---|---:|")
    lines.append(f"| ticks | {result.get('n_ticks', '-')} |")
    lines.append(f"| symbol-evals | {result.get('n_symbol_evaluations', '-')} |")
    lines.append(f"| wall_clock_secs | {_fmt(result.get('wall_clock_secs'), 1)} |")
    lines.append(f"| ensemble pkl | `ensemble_intraday_v3.pkl` |")
    lines.append(f"| sandbox db | `{SANDBOX_DB.name}` |")
    lines.append(f"| block log | `{BLOCK_LOG.name}` |")
    lines.append("")
    lines.append("## v3 per-symbol PF")
    lines.append("")
    per_sym = result.get("per_symbol", {})
    if per_sym:
        lines.append("| Symbol | n_trades | PF | win_rate |")
        lines.append("|--------|---:|---:|---:|")
        for sym in sorted(per_sym):
            s = per_sym[sym]
            lines.append(f"| {sym} | {s.get('n_trades', 0)} | {_fmt_pf(s.get('pf'))} | {_fmt(s.get('win_rate'))} |")
    else:
        lines.append("_(per-symbol breakdown unavailable)_")
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    if ship:
        lines.append(f"v3 cleared the R14 SHIP gate (n_trades={n_trades_v3} > {SHIP_GATE_N_TRADES}; PF={_fmt_pf(pf_v3)} > {SHIP_GATE_PF}).")
        lines.append("")
        lines.append("Deploy procedure (ops runs before 09:10 IST):")
        lines.append("```")
        lines.append("cp models/saved/ensemble_intraday.pkl     models/saved/ensemble_intraday_v2b_pre_r14_backup.pkl")
        lines.append("cp models/saved/ensemble_intraday_v3.pkl  models/saved/ensemble_intraday.pkl")
        lines.append("```")
        lines.append("Rollback if v3 misbehaves in-session:")
        lines.append("```")
        lines.append("cp models/saved/ensemble_intraday_v2b_pre_r14_backup.pkl models/saved/ensemble_intraday.pkl")
        lines.append("```")
    else:
        lines.append(f"v3 did NOT clear the R14 SHIP gate (n_trades={n_trades_v3} vs >{SHIP_GATE_N_TRADES}; PF={_fmt_pf(pf_v3)} vs >{SHIP_GATE_PF}).")
        lines.append("")
        lines.append("v2b remains live. v3.pkl is preserved alongside for diagnostic comparison; no deploy.")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Report written -> %s (SHIP=%s)", REPORT_PATH, ship)
    return ship


# ── Main ─────────────────────────────────────────────────────────────


def main() -> int:
    logger.info("=== R14 v3 engine-replay ===")
    logger.info("Holdout: %s -> %s", HOLDOUT_START.date(), HOLDOUT_END.date())
    logger.info("Symbols: %d", len(DEFAULT_SYMBOLS))
    logger.info("Ensemble: %s (exists=%s)", V3_PATH, V3_PATH.exists())
    if not V3_PATH.exists():
        logger.error("v3.pkl missing — run models/retrain_v3_r14.py first.")
        return 1

    SANDBOX_DB.parent.mkdir(parents=True, exist_ok=True)
    if SANDBOX_DB.exists():
        SANDBOX_DB.unlink()

    wall = time.time()
    try:
        result = run_replay(
            symbols=DEFAULT_SYMBOLS,
            holdout_start=HOLDOUT_START,
            holdout_end=HOLDOUT_END,
            ensemble_path=V3_PATH,
            sandbox_db_path=SANDBOX_DB,
            portfolio_value=PORTFOLIO_VALUE,
            progress_every=200,
            block_log_path=BLOCK_LOG,
        )
    except Exception as e:
        logger.exception("v3 replay CRASHED: %s", e)
        _telegram_alert(f"[R14 v3 replay] CRASHED: {type(e).__name__}: {e}")
        return 2

    elapsed = time.time() - wall
    logger.info("Replay done in %.0fs (%.1f min)", elapsed, elapsed / 60)
    logger.info("Metrics: %s", result["metrics"])

    ship = _write_report(result)

    n_trades = result["metrics"].get("n_trades", 0)
    pf = result["metrics"].get("profit_factor", 0.0)
    pf_str = "inf" if pf == float("inf") else f"{pf:.3f}"
    if ship:
        _telegram_alert(
            f"[R14 v3 replay] SHIP — n_trades={n_trades}, PF={pf_str}. "
            f"Report: {REPORT_PATH.name}. Ops review before deploy."
        )
    else:
        _telegram_alert(
            f"[R14 v3 replay] NO_SHIP — n_trades={n_trades}, PF={pf_str}. "
            f"v2b stays live. Report: {REPORT_PATH.name}."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
