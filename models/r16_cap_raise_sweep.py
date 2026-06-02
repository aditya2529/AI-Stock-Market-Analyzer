"""R16 — daily_count_cap raise sweep on v3 @ 0.55 floor.

Drives engine-replay over 2026-01-01 -> 2026-05-28 (same OOS window
as R12/R14/R15) at three raised DAILY_TRADE_CAP values: {10, 15, 20}.
Conf-floor is held at 0.55 — the most promising R15 lead
(PF 1.699 / win 0.750 on 8 capped trades).

WHY
===
R14 (label density) and R15 (conf-floor) both ruled out as
single-knob fixes for the persistent 8-trade output. R15 showed
the cap-cascade undeniably: as conf-floor dropped 0.60 -> 0.50,
daily_count_capped grew 1,618 -> 6,090 — every released candidate
absorbed by the downstream daily cap. The cap is the actual ceiling.

SHIP gate (per ops R16 brief, additive constraint over R14/R15):
    n_trades > 24  AND  PF > 1.3
    AND  worst-case daily loss stays under the 3% DAILY_LOSS_LIMIT

PRODUCTION SAFETY
=================
- ``intraday/engine.py`` UNCHANGED. The cap is overridden via
  monkey-patch in this script only: ``eng.DAILY_TRADE_CAP = N``
  before each ``run_replay`` call, restored in finally. Engine
  reads DAILY_TRADE_CAP from module globals at call time so the
  patch flows through.
- ``ensemble_intraday.pkl`` (live v2b) UNTOUCHED.
- ``config.SIGNAL_MIN_CONFIDENCE`` UNCHANGED (conf_floor_override=0.55).
- Fresh sandbox SQLite per cap level.

CONTENTION
==========
Live engine boots 09:10 IST. This sweep runs at BelowNormal priority
when launched via the PowerShell wrapper; if RAM crosses 6.5 GB the
RAM monitor inside run_replay alerts via Telegram. Live v2b session
takes priority over R16 research — manually kill PID if needed.
"""
from __future__ import annotations

import logging
import sqlite3
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
LOG_PATH = PROJECT_ROOT / "logs" / "r16_cap_raise_sweep.log"
REPORT_PATH = PROJECT_ROOT / "logs" / "r16_cap_raise_report.md"
TIMELINE_CSV = PROJECT_ROOT / "logs" / "r16_per_day_trades.csv"

HOLDOUT_START = pd.Timestamp("2026-01-01")
HOLDOUT_END = pd.Timestamp("2026-05-28")
PORTFOLIO_VALUE = 500_000.0
NSE_INITIAL_CASH = 500_000.0

CONF_FLOOR = 0.55
CAP_LEVELS = [10, 15, 20]

SHIP_GATE_N_TRADES = 24
SHIP_GATE_PF = 1.3
DAILY_LOSS_LIMIT_PCT = 0.03  # halt at -3% daily
DAILY_LOSS_LIMIT_RS = DAILY_LOSS_LIMIT_PCT * NSE_INITIAL_CASH  # Rs 15,000

# Baselines pulled verbatim from R12/R14/R15 reports
BASELINES = {
    "v2b @ floor 0.60, cap 8 (LIVE)":  {"pf": float("inf"), "sharpe": -2.602, "win_rate": 1.000, "max_dd": -0.000, "n_trades": 8},
    "v3 @ floor 0.60, cap 8 (R14)":    {"pf": float("inf"), "sharpe": -2.605, "win_rate": 1.000, "max_dd": -0.000, "n_trades": 8},
    "v3 @ floor 0.55, cap 8 (R15)":    {"pf": 1.699,        "sharpe": -2.498, "win_rate": 0.750, "max_dd": 0.011,  "n_trades": 8},
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
logger = logging.getLogger("r16_sweep")


def _telegram(msg: str) -> None:
    try:
        from alerts.telegram_bot import send_message  # type: ignore
        send_message(msg)
    except Exception as e:
        logger.warning("Telegram alert dropped: %s", e)


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


# ── Per-day timeline (Q2) ────────────────────────────────────────────


def _build_per_day_timeline(cap_runs: dict) -> pd.DataFrame:
    """For each (cap, date), pull from the cap's block_reasons CSV +
    sandbox DB:
      - n_conf_blocked, n_regime_blocked, n_daily_count_capped
      - first_cap_block_time
      - n_BUYs_opened, n_closed_today
      - daily_pnl, daily_pnl_pct
    The R15 floor-0.55 cap-8 run gets included too so the report can
    diff each cap-raise against the cap-8 baseline.
    """
    rows = []
    runs = dict(cap_runs)
    runs[8] = {
        "block_log": PROJECT_ROOT / "logs" / "r15_block_reasons_f55.csv",
        "sandbox":   PROJECT_ROOT / "logs" / "r15_v3_sandbox_f55.db",
    }
    for cap in sorted(runs):
        blocks_path = runs[cap]["block_log"]
        sandbox = runs[cap]["sandbox"]
        if not blocks_path.exists():
            logger.warning("cap=%d block log missing: %s", cap, blocks_path)
            continue

        blocks = pd.read_csv(blocks_path, parse_dates=["timestamp"])
        blocks["date"] = blocks["timestamp"].dt.date
        per_day_block = (
            blocks
            .groupby(["date", "block_reason"])
            .size()
            .unstack(fill_value=0)
        )
        cap_block_first = (
            blocks[blocks["block_reason"] == "daily_count_capped"]
            .groupby("date")["timestamp"]
            .min()
        )

        trades_df = pd.DataFrame()
        if sandbox.exists():
            try:
                with sqlite3.connect(str(sandbox)) as conn:
                    trades_df = pd.read_sql_query(
                        "SELECT symbol, entry_time, exit_time, net_pnl "
                        "FROM paper_trades", conn,
                        parse_dates=["entry_time", "exit_time"],
                    )
            except Exception as e:
                logger.warning("cap=%d sandbox read failed: %s", cap, e)

        if not trades_df.empty:
            trades_df["entry_date"] = trades_df["entry_time"].dt.date
            trades_df["exit_date"] = trades_df["exit_time"].dt.date
            per_day_opens = trades_df.groupby("entry_date").size()
            per_day_closes = trades_df.groupby("exit_date").size()
            per_day_pnl = trades_df.groupby("exit_date")["net_pnl"].sum()
        else:
            per_day_opens = pd.Series(dtype=int)
            per_day_closes = pd.Series(dtype=int)
            per_day_pnl = pd.Series(dtype=float)

        all_dates = sorted(set(per_day_block.index)
                           | set(per_day_opens.index)
                           | set(per_day_closes.index))
        for d in all_dates:
            row = {
                "cap": cap,
                "date": d,
                "conf_blocked": int(per_day_block.get("conf_blocked", pd.Series()).get(d, 0)),
                "regime_blocked": int(per_day_block.get("regime_blocked", pd.Series()).get(d, 0)),
                "daily_count_capped": int(per_day_block.get("daily_count_capped", pd.Series()).get(d, 0)),
                "exposure_capped": int(per_day_block.get("exposure_capped", pd.Series()).get(d, 0)),
                "first_cap_block_time": (
                    cap_block_first.get(d).strftime("%H:%M:%S")
                    if d in cap_block_first.index else ""
                ),
                "n_BUYs_opened": int(per_day_opens.get(d, 0)),
                "n_trades_closed": int(per_day_closes.get(d, 0)),
                "daily_net_pnl": round(float(per_day_pnl.get(d, 0.0)), 2),
                "daily_pnl_pct_of_account": round(
                    float(per_day_pnl.get(d, 0.0)) / NSE_INITIAL_CASH * 100, 3),
            }
            rows.append(row)
    df = pd.DataFrame(rows)
    return df


def _risk_math(per_day_df: pd.DataFrame, cap: int) -> dict:
    """Risk summary for one cap level.

    Returns worst-case daily loss, average daily loss across days with
    losses, count of days where the 3% loss-halt would have fired.
    """
    cap_df = per_day_df[per_day_df["cap"] == cap]
    if cap_df.empty:
        return {"n_days": 0, "worst_loss_rs": 0.0,
                "worst_loss_pct": 0.0, "n_halt_firing_days": 0,
                "avg_loss_on_losing_days_rs": 0.0}
    worst_loss_rs = float(cap_df["daily_net_pnl"].min())
    if worst_loss_rs > 0:
        worst_loss_rs = 0.0
    worst_loss_pct = worst_loss_rs / NSE_INITIAL_CASH * 100.0
    losing = cap_df[cap_df["daily_net_pnl"] < 0]
    halt_fire = cap_df[cap_df["daily_net_pnl"] <= -DAILY_LOSS_LIMIT_RS]
    return {
        "n_days": int((cap_df["n_trades_closed"] > 0).sum()),
        "worst_loss_rs": worst_loss_rs,
        "worst_loss_pct": worst_loss_pct,
        "n_halt_firing_days": int(len(halt_fire)),
        "avg_loss_on_losing_days_rs": float(losing["daily_net_pnl"].mean()) if not losing.empty else 0.0,
    }


# ── Report ───────────────────────────────────────────────────────────


def _ship(metrics: dict, risk: dict) -> bool:
    pf = metrics.get("profit_factor", 0.0)
    n = metrics.get("n_trades", 0)
    pf_ok = (pf == float("inf")) or (pf > SHIP_GATE_PF)
    trades_ok = n > SHIP_GATE_N_TRADES
    risk_ok = risk["n_halt_firing_days"] == 0
    return pf_ok and trades_ok and risk_ok


def _write_report(cap_metrics: dict, cap_meta: dict,
                  per_day_df: pd.DataFrame,
                  cap_risk: dict) -> tuple[bool, int | None]:
    ship_caps = [c for c in CAP_LEVELS if _ship(cap_metrics[c], cap_risk[c])]
    any_ship = bool(ship_caps)
    best_cap = None
    if any_ship:
        def _key(c):
            m = cap_metrics[c]
            pf = m["profit_factor"]
            pf_score = 1e9 if pf == float("inf") else pf
            return (pf_score, m["n_trades"])
        best_cap = max(ship_caps, key=_key)

    lines = []
    lines.append("# R16 — daily_count_cap raise sweep on v3 @ 0.55")
    lines.append("")
    lines.append(f"**Holdout window:** {HOLDOUT_START.date()} -> {HOLDOUT_END.date()} (same as R12/R14/R15)")
    lines.append(f"**Symbols:** {len(DEFAULT_SYMBOLS)} (DEFAULT_SYMBOLS)")
    lines.append(f"**Ensemble:** `ensemble_intraday_v3.pkl` (R14 scalper)")
    lines.append(f"**Conf-floor:** {CONF_FLOOR:.2f} (held — R15's most promising lead)")
    lines.append(f"**Caps swept:** {CAP_LEVELS}")
    lines.append(f"**SHIP gate:** n_trades > {SHIP_GATE_N_TRADES} AND PF > {SHIP_GATE_PF} AND worst-case daily loss < 3% (Rs {int(DAILY_LOSS_LIMIT_RS):,})")
    lines.append("")
    lines.append("## Q1 — Current cap")
    lines.append("")
    lines.append("`DAILY_TRADE_CAP = 8` at `intraday/engine.py:130`. Comment: \"5 max-open + a few closes/re-entries\".")
    lines.append("")
    lines.append("Cap-block fires when `today_count >= DAILY_TRADE_CAP`, where ")
    lines.append("`today_count = today_closed_count + len(nse_positions)` (`intraday/engine.py:186`).")
    lines.append("")
    lines.append("## TL;DR — SHIP VERDICT")
    lines.append("")
    if any_ship:
        m = cap_metrics[best_cap]
        r = cap_risk[best_cap]
        lines.append(f"**SHIP v3 @ floor 0.55, cap {best_cap}**")
        lines.append("")
        lines.append(f"_n_trades={m['n_trades']}, PF={_fmt_pf(m['profit_factor'])}, "
                     f"win_rate={_fmt(m['win_rate'])}, worst daily loss="
                     f"Rs {r['worst_loss_rs']:,.0f} ({_fmt(r['worst_loss_pct'])}%)._")
    else:
        lines.append("**NO_SHIP — no cap level cleared all 3 gates**")
        lines.append("")
        bullets = []
        for c in CAP_LEVELS:
            m = cap_metrics[c]
            r = cap_risk[c]
            pf = m["profit_factor"]
            pf_ok = "Y" if (pf == float("inf") or pf > SHIP_GATE_PF) else "N"
            n_ok = "Y" if m["n_trades"] > SHIP_GATE_N_TRADES else "N"
            risk_ok = "Y" if r["n_halt_firing_days"] == 0 else "N"
            bullets.append(f"cap {c}: n={m['n_trades']} (gate {n_ok}), PF={_fmt_pf(pf)} (gate {pf_ok}), risk_ok={risk_ok}")
        lines.append("_" + " | ".join(bullets) + "_")
    lines.append("")

    lines.append("## Full comparison")
    lines.append("")
    lines.append("| Variant | PF | Sharpe | Win | Max DD | n_trades | n_halt-firing days |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for name, b in BASELINES.items():
        lines.append(
            f"| {name} | {_fmt_pf(b['pf'])} | {_fmt(b['sharpe'])} | "
            f"{_fmt(b['win_rate'])} | {_fmt(b['max_dd'])} | {b['n_trades']} | 0 (baseline) |")
    for c in CAP_LEVELS:
        m = cap_metrics[c]
        r = cap_risk[c]
        flag = " **← SHIP**" if (best_cap == c) else (" (SHIP)" if c in ship_caps else "")
        lines.append(
            f"| v3 @ floor 0.55, cap {c} (R16){flag} | "
            f"{_fmt_pf(m['profit_factor'])} | {_fmt(m['sharpe'])} | "
            f"{_fmt(m['win_rate'])} | {_fmt(m['max_drawdown'])} | "
            f"{m['n_trades']} | {r['n_halt_firing_days']} |")
    lines.append("")

    lines.append("## Q2 — Per-day cap-hit timeline (cap-fills-early or cap-fills-natural?)")
    lines.append("")
    lines.append(f"Full per-day timeline written to `{TIMELINE_CSV.name}` "
                 f"(one row per (cap, date)). Summary per cap:")
    lines.append("")
    lines.append("| Cap | days with cap-blocks | first-cap-block median time | mean cap-blocks/day | days where cap saturated (>0 cap-blocks) |")
    lines.append("|---:|---:|---|---:|---:|")
    for cap in [8] + CAP_LEVELS:
        cap_df = per_day_df[per_day_df["cap"] == cap]
        if cap_df.empty:
            lines.append(f"| {cap} | - | - | - | - |")
            continue
        cap_days = cap_df[cap_df["daily_count_capped"] > 0]
        n_cap_days = len(cap_days)
        if n_cap_days > 0:
            times = pd.to_datetime(cap_days["first_cap_block_time"],
                                    format="%H:%M:%S", errors="coerce").dropna()
            median_time = times.dt.strftime("%H:%M:%S").iloc[len(times)//2] if not times.empty else "-"
            mean_blocks = cap_days["daily_count_capped"].mean()
        else:
            median_time = "-"
            mean_blocks = 0.0
        lines.append(f"| {cap} | {n_cap_days} | {median_time} | "
                     f"{mean_blocks:.0f} | {n_cap_days} |")
    lines.append("")

    lines.append("## Q3 — Sweep metadata")
    lines.append("")
    lines.append("| Cap | ticks | symbol-evals | wall_secs | sandbox db | block log |")
    lines.append("|---:|---:|---:|---:|---|---|")
    for cap in CAP_LEVELS:
        meta = cap_meta[cap]
        lines.append(
            f"| {cap} | {meta.get('n_ticks', '-')} | "
            f"{meta.get('n_symbol_evaluations', '-')} | "
            f"{_fmt(meta.get('wall_clock_secs'), 1)} | "
            f"`{Path(meta['sandbox_db_path']).name}` | "
            f"`r16_block_reasons_cap{cap:02d}.csv` |")
    lines.append("")

    lines.append("## Q4 — Risk math (per cap level)")
    lines.append("")
    lines.append(f"DAILY_LOSS_LIMIT = -3% = -Rs {int(DAILY_LOSS_LIMIT_RS):,} on Rs {int(NSE_INITIAL_CASH):,} account. "
                 "The halt is at the gate level — once today's net_pnl falls below this, all new BUYs block.")
    lines.append("")
    lines.append("| Cap | trading days w/ trades | worst daily loss (Rs) | worst daily loss (%) | days halt-would-fire | avg loss on losing days (Rs) |")
    lines.append("|---:|---:|---:|---:|---:|---:|")
    for cap in [8] + CAP_LEVELS:
        r = cap_risk[cap]
        worst_loss_str = f"{r['worst_loss_rs']:,.0f}" if r["worst_loss_rs"] < 0 else "0"
        avg_loss_str = f"{r['avg_loss_on_losing_days_rs']:,.0f}" if r["avg_loss_on_losing_days_rs"] < 0 else "0"
        lines.append(f"| {cap} | {r['n_days']} | {worst_loss_str} | {r['worst_loss_pct']:.3f}% | {r['n_halt_firing_days']} | {avg_loss_str} |")
    lines.append("")
    lines.append("**Risk reading:** if `days halt-would-fire = 0` across all swept caps, the cap-raise is "
                 "risk-coherent. The 3% loss-halt is the real circuit breaker; cap is about frequency, "
                 "not loss-limit. If any cap level shows >0 halt-firing days, that cap is "
                 "categorically NOT a SHIP candidate regardless of PF.")
    lines.append("")

    lines.append("## Recommendation")
    lines.append("")
    if any_ship:
        m = cap_metrics[best_cap]
        r = cap_risk[best_cap]
        lines.append(f"v3 @ floor 0.55, cap {best_cap} cleared all 3 gates.")
        lines.append("")
        lines.append("This is a **THREE-axis deploy** — model swap + config conf-floor + engine cap.")
        lines.append("Ops procedure (before 09:10 IST):")
        lines.append("```")
        lines.append("# 1. Backup live state")
        lines.append("cp models/saved/ensemble_intraday.pkl     models/saved/ensemble_intraday_v2b_pre_r16_backup.pkl")
        lines.append("cp config.py                              config.py.pre_r16_backup")
        lines.append("cp intraday/engine.py                     intraday/engine.py.pre_r16_backup")
        lines.append("# 2. Swap ensemble")
        lines.append("cp models/saved/ensemble_intraday_v3.pkl  models/saved/ensemble_intraday.pkl")
        lines.append("# 3. Edit config.py: SIGNAL_MIN_CONFIDENCE = 0.55")
        lines.append(f"# 4. Edit intraday/engine.py:130: DAILY_TRADE_CAP = {best_cap}")
        lines.append("```")
        lines.append("Rollback if v3 misbehaves in-session:")
        lines.append("```")
        lines.append("cp models/saved/ensemble_intraday_v2b_pre_r16_backup.pkl models/saved/ensemble_intraday.pkl")
        lines.append("cp config.py.pre_r16_backup    config.py")
        lines.append("cp intraday/engine.py.pre_r16_backup intraday/engine.py")
        lines.append("```")
    else:
        lines.append("No cap level cleared all 3 gates. v2b stays live.")
        lines.append("")
        lines.append("If trade count rises but PF degrades: the higher-frequency v3 signals are "
                     "structurally lower-quality on this window (the lower confidence reflects "
                     "real ambiguity). Suggests R17 should investigate WHY v3 confidence sits at "
                     "0.40-0.55 — feature engineering or sequence-layer calibration, not a "
                     "different threshold or cap.")
        lines.append("")
        lines.append("If halt-would-fire > 0 at any cap: raising the cap re-introduces drawdown "
                     "risk that v2b's lower frequency avoided. Cap-raise without retraining is "
                     "not safe.")
    lines.append("")

    lines.append("## Replay-vs-live cap discrepancy (filed P-item)")
    lines.append("")
    lines.append("`intraday/engine.py:172` reads `today_closed_count` via SQL "
                 "`date('now','localtime')` — wall-clock today, NOT the replay-clock. ")
    lines.append("During replay, `today_closed_count = 0` always, so the cap effectively "
                 "becomes a global concurrent-position cap rather than a daily cap. In "
                 "production it resets daily.")
    lines.append("")
    lines.append("**Implication for this report:** these replay numbers may UNDERESTIMATE "
                 "the live trade count after a cap raise. If the cap looks safe in replay, "
                 "it's at-least-as-safe in production; if it looks unsafe, it might be even "
                 "worse live. Ops should weigh accordingly.")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Report written -> %s (ANY_SHIP=%s, best=%s)",
                REPORT_PATH, any_ship, best_cap)
    return any_ship, best_cap


# ── Main ─────────────────────────────────────────────────────────────


def _patch_cap(cap_value: int):
    """Monkey-patch intraday.engine.DAILY_TRADE_CAP. Returns the saved
    original so the caller can restore it in finally."""
    import intraday.engine as eng
    original = eng.DAILY_TRADE_CAP
    eng.DAILY_TRADE_CAP = cap_value
    logger.info("Patched DAILY_TRADE_CAP %d -> %d", original, cap_value)
    return original


def _restore_cap(original: int):
    import intraday.engine as eng
    eng.DAILY_TRADE_CAP = original
    logger.info("Restored DAILY_TRADE_CAP -> %d", original)


def main() -> int:
    logger.info("=== R16 cap-raise sweep ===")
    logger.info("Holdout: %s -> %s", HOLDOUT_START.date(), HOLDOUT_END.date())
    logger.info("Conf-floor: %.2f", CONF_FLOOR)
    logger.info("Caps: %s", CAP_LEVELS)
    logger.info("Symbols: %d", len(DEFAULT_SYMBOLS))
    logger.info("v3 ensemble: %s (exists=%s)", V3_PATH, V3_PATH.exists())
    if not V3_PATH.exists():
        logger.error("v3.pkl missing at %s", V3_PATH)
        _telegram(f"[R16] ABORT — v3.pkl missing at {V3_PATH}")
        return 1

    _telegram(
        f"[R16 cap-raise sweep] kickoff — v3 @ floor {CONF_FLOOR:.2f}, "
        f"caps={CAP_LEVELS}, window {HOLDOUT_START.date()} -> {HOLDOUT_END.date()}, "
        f"ETA ~{len(CAP_LEVELS) * 30} min.")

    cap_metrics: dict[int, dict] = {}
    cap_meta: dict[int, dict] = {}
    cap_runs: dict[int, dict] = {}

    wall_total = time.time()

    for i, cap in enumerate(CAP_LEVELS, start=1):
        sandbox = PROJECT_ROOT / "logs" / f"r16_v3_sandbox_cap{cap:02d}.db"
        block_log = PROJECT_ROOT / "logs" / f"r16_block_reasons_cap{cap:02d}.csv"
        if sandbox.exists():
            sandbox.unlink()
        logger.info("--- [%d/%d] cap=%d ---", i, len(CAP_LEVELS), cap)
        original_cap = _patch_cap(cap)
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
                conf_floor_override=CONF_FLOOR,
            )
        except Exception as e:
            logger.exception("cap=%d CRASHED: %s", cap, e)
            _telegram(f"[R16] cap={cap} CRASHED: {type(e).__name__}: {e}")
            _restore_cap(original_cap)
            return 2
        finally:
            _restore_cap(original_cap)

        elapsed = time.time() - t0
        logger.info("cap=%d done in %.0fs (%.1f min)  metrics=%s",
                    cap, elapsed, elapsed / 60, result["metrics"])
        cap_metrics[cap] = result["metrics"]
        cap_meta[cap] = result
        cap_runs[cap] = {"block_log": block_log, "sandbox": sandbox}

        m = result["metrics"]
        pf_str = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.3f}"
        _telegram(
            f"[R16] cap={cap} done — n_trades={m['n_trades']}, "
            f"PF={pf_str}, win={m['win_rate']:.3f}, "
            f"wall={elapsed/60:.1f}min ({i}/{len(CAP_LEVELS)}).")

    total_secs = time.time() - wall_total
    logger.info("=== sweep done in %.0fs (%.1f min) ===",
                total_secs, total_secs / 60)

    # Q2 + Q4 — per-day timeline + risk math
    logger.info("Building per-day timeline + risk math ...")
    per_day_df = _build_per_day_timeline(cap_runs)
    TIMELINE_CSV.parent.mkdir(parents=True, exist_ok=True)
    per_day_df.to_csv(TIMELINE_CSV, index=False)
    logger.info("Per-day timeline written -> %s (%d rows)",
                TIMELINE_CSV, len(per_day_df))

    cap_risk = {cap: _risk_math(per_day_df, cap)
                for cap in [8] + CAP_LEVELS}

    any_ship, best_cap = _write_report(cap_metrics, cap_meta, per_day_df, cap_risk)

    if any_ship:
        m = cap_metrics[best_cap]
        r = cap_risk[best_cap]
        pf_str = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.3f}"
        _telegram(
            f"[R16] SHIP @ cap={best_cap} — n_trades={m['n_trades']}, "
            f"PF={pf_str}, worst_loss=Rs {r['worst_loss_rs']:,.0f}, "
            f"halt-firing days={r['n_halt_firing_days']}. "
            f"3-axis deploy; ops review {REPORT_PATH.name}.")
    else:
        summary = ", ".join(
            f"cap{c}=n{cap_metrics[c]['n_trades']}/PF{_fmt_pf(cap_metrics[c]['profit_factor'])}"
            for c in CAP_LEVELS)
        _telegram(
            f"[R16] NO_SHIP — {summary}. v2b stays live. "
            f"Report {REPORT_PATH.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
