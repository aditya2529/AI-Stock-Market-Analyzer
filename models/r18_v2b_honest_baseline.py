"""R18 — v2b honest baseline (post-fix re-run).

Re-runs v2b @ floor 0.60 cap=8 on the same OOS window as R17 with the
R18 strict-less-than replay slicing fix (SHA 36b9eff) in place. Goal:
produce the first honest backtest PF number — the one that should
finally match live behavior.

Expected outcome:
  * PF drops from R17's 8.126 to something in the 0.7-1.5 range
    (matching live PF 0.76 within sample noise) — confirms R18 fix.
  * If PF stays >> 1.5, there's another bug we haven't caught
    (more audit needed — possibly features/engineer.py opening_range
    training leakage, but that affects training not test).

Production safety:
  * intraday/engine.py UNCHANGED.
  * config.py UNCHANGED.
  * ensemble_intraday.pkl UNCHANGED (live v2b reads this).
  * Sandbox SQLite at logs/r18_v2b_honest_sandbox.db (isolated).
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

V2B_PATH = PROJECT_ROOT / "models" / "saved" / "ensemble_intraday_v2b.pkl"
SANDBOX = PROJECT_ROOT / "logs" / "r18_v2b_honest_sandbox.db"
BLOCK_LOG = PROJECT_ROOT / "logs" / "r18_v2b_honest_block_reasons.csv"
LOG_PATH = PROJECT_ROOT / "logs" / "r18_v2b_honest_baseline.log"

HOLDOUT_START = pd.Timestamp("2026-01-01")
HOLDOUT_END = pd.Timestamp("2026-05-28")
PORTFOLIO_VALUE = 500_000.0
CONF_FLOOR = 0.60

# For the diff
R17_NUMBERS = {
    "n_trades": 197,
    "profit_factor": 8.126,
    "win_rate": 0.888,
    "sharpe": 12.626,
    "max_drawdown": 0.035,
    "cagr": 4.115,
}
LIVE_NUMBERS = {
    "n_trades": 16,
    "profit_factor": 0.764,
    "win_rate": 0.438,
    "since": "2026-05-29",
}

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(name)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("r18_v2b_honest")


def _telegram(msg: str) -> None:
    try:
        from alerts.telegram_bot import send_message  # type: ignore
        send_message(msg)
    except Exception as e:
        logger.warning("Telegram dropped: %s", e)


def _fmt_pf(pf):
    if pf is None or (isinstance(pf, float) and pf != pf):
        return "-"
    if pf == float("inf"):
        return "inf"
    return f"{pf:.3f}"


def main() -> int:
    logger.info("=== R18 v2b honest baseline (post-fix) ===")
    logger.info("Holdout: %s -> %s", HOLDOUT_START.date(), HOLDOUT_END.date())
    logger.info("Conf floor: %.2f, cap: 8 (default)", CONF_FLOOR)
    if not V2B_PATH.exists():
        logger.error("v2b.pkl missing at %s", V2B_PATH)
        _telegram("[R18] ABORT — v2b.pkl missing")
        return 1

    _telegram(
        f"[R18 v2b honest baseline] kickoff — fix-applied replay of "
        f"v2b @ floor {CONF_FLOOR}, ETA ~35 min.")

    if SANDBOX.exists():
        SANDBOX.unlink()

    t0 = time.time()
    try:
        result = run_replay(
            symbols=DEFAULT_SYMBOLS,
            holdout_start=HOLDOUT_START,
            holdout_end=HOLDOUT_END,
            ensemble_path=V2B_PATH,
            sandbox_db_path=SANDBOX,
            portfolio_value=PORTFOLIO_VALUE,
            progress_every=400,
            block_log_path=BLOCK_LOG,
            conf_floor_override=CONF_FLOOR,
        )
    except Exception as e:
        logger.exception("Replay CRASHED: %s", e)
        _telegram(f"[R18] v2b honest baseline CRASHED: {type(e).__name__}: {e}")
        return 2

    wall = time.time() - t0
    m = result["metrics"]
    logger.info("Done in %.0fs (%.1f min) metrics=%s",
                wall, wall / 60, m)

    # Write a small markdown report so ops can read the comparison
    # without digging into the metrics dict.
    report_path = PROJECT_ROOT / "logs" / "r18_v2b_honest_baseline.md"
    pf = m.get("profit_factor", 0.0)
    n = m.get("n_trades", 0)
    win = m.get("win_rate", 0.0)
    sharpe = m.get("sharpe", 0.0)
    dd = m.get("max_drawdown", 0.0)

    lines = []
    lines.append("# R18 v2b honest baseline — post-fix replay")
    lines.append("")
    lines.append(f"**Holdout:** {HOLDOUT_START.date()} -> {HOLDOUT_END.date()}")
    lines.append(f"**Ensemble:** v2b @ floor {CONF_FLOOR} cap=8 (LIVE config, unchanged)")
    lines.append(f"**Replay fix SHA:** 36b9eff (strict-less-than slicing in 3 patch sites)")
    lines.append("")
    lines.append("## Three numbers compared")
    lines.append("")
    lines.append("| | R12-R17 backtest (look-ahead) | **R18 backtest (fixed)** | Live v2b (since 2026-05-29) |")
    lines.append("|---|---:|---:|---:|")
    lines.append(f"| n_trades | {R17_NUMBERS['n_trades']} | **{n}** | {LIVE_NUMBERS['n_trades']} |")
    lines.append(f"| Profit Factor | {_fmt_pf(R17_NUMBERS['profit_factor'])} | **{_fmt_pf(pf)}** | {_fmt_pf(LIVE_NUMBERS['profit_factor'])} |")
    lines.append(f"| Win rate | {R17_NUMBERS['win_rate']:.3f} | **{win:.3f}** | {LIVE_NUMBERS['win_rate']:.3f} |")
    lines.append(f"| Sharpe | {R17_NUMBERS['sharpe']:.3f} | **{sharpe:.3f}** | n/a (small sample) |")
    lines.append(f"| Max DD | {R17_NUMBERS['max_drawdown']:.3f} | **{dd:.3f}** | n/a (small sample) |")
    lines.append("")
    lines.append("## Reading")
    lines.append("")
    if 0.4 <= pf <= 1.8:
        lines.append("**R18 fix CONFIRMED.** Post-fix backtest PF lands in the live-plausible "
                     "range (0.4-1.8 brackets where live's 0.76 sample sits comfortably). "
                     "The 10x backtest-vs-live PF gap is fully explained by the 5-minute "
                     "replay look-ahead. R12-R17 PF rankings are NOT informative about live.")
        lines.append("")
        lines.append("**Operational consequence:** v2b stays live with realistic expectations. "
                     "The model is approximately break-even, not the PF-8 phantom the "
                     "look-ahead promised. Same for v3 — pivot to swing as previously "
                     "discussed by ops is now data-supported.")
    elif pf > 1.8:
        lines.append(f"**PARTIAL fix.** Post-fix backtest PF {pf:.3f} dropped substantially "
                     f"from R17's 8.126 — confirms the replay look-ahead was real. But the "
                     f"residual gap vs live's 0.76 suggests ADDITIONAL bias remains. "
                     f"Candidates to audit next: (a) training-time look-ahead in opening_range "
                     f"or other features/engineer.py components (see R18 audit notes), "
                     f"(b) data source mismatch (Upstox historical vs yfinance live), "
                     f"(c) slippage modeling.")
    else:
        lines.append(f"**Backtest now WORSE than live.** Post-fix PF {pf:.3f} < live PF 0.76. "
                     f"Either the fix over-corrected (unlikely — strict-less-than is the "
                     f"causally correct semantics) or live has a sample-noise upward swing "
                     f"that won't persist. Need 2-3 more weeks of live to disambiguate.")
    lines.append("")
    lines.append("## Run metadata")
    lines.append("")
    lines.append(f"| | |")
    lines.append(f"|---|---|")
    lines.append(f"| wall_clock | {wall/60:.1f} min |")
    lines.append(f"| ticks | {result.get('n_ticks', '-')} |")
    lines.append(f"| symbol-evals | {result.get('n_symbol_evaluations', '-')} |")
    lines.append(f"| sandbox | `{SANDBOX.name}` |")
    lines.append(f"| block log | `{BLOCK_LOG.name}` |")
    lines.append("")
    lines.append("Compare to R17 v2b (same ensemble, same window, look-ahead enabled): "
                 "n=197, PF=8.126, win=0.888, Sharpe=12.626, max_dd=0.035, wall=38.5 min.")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Report -> %s", report_path)

    pf_str = "inf" if pf == float("inf") else f"{pf:.3f}"
    _telegram(
        f"[R18 v2b honest baseline] DONE — n={n}, PF={pf_str}, "
        f"win={win:.3f}, wall={wall/60:.1f}min. "
        f"R17 was PF 8.126 (look-ahead); live is PF 0.76. "
        f"Report: r18_v2b_honest_baseline.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
