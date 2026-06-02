"""R17 — re-baseline + re-sweep with clock fix in place.

After the R17 clock fix (SHA 30a20a6), this driver re-runs:

  1. v2b @ floor 0.60, cap 8 — the LIVE engine config. Did v2b
     actually trade 8 times over 5 months, or was that itself a
     clock artifact?
  2. v3  @ floor 0.55, cap 8 — R15's promising baseline.
  3. v3  @ floor 0.55, cap ∈ {10, 15, 20, 25, 30} — R16's sweep
     plus two new caps now that we can trust the risk math.

SHIP gate (per R14/R16 brief):
    n_trades > 24  AND  PF > 1.3
    AND  worst-case daily loss < 3% (Rs 15,000) — REAL daily
    distribution now that the clock fix is in.

PRODUCTION SAFETY
=================
- ``intraday/engine.py`` source EDITED at R17 (cap-query SQL only)
  but the live engine (PID 22336) is running its in-memory bytecode
  from this morning's 09:10 IST boot — it does NOT see these
  edits until tomorrow's boot.
- ``ensemble_intraday.pkl`` (live v2b) UNTOUCHED.
- ``config.SIGNAL_MIN_CONFIDENCE`` UNCHANGED.
- DAILY_TRADE_CAP overridden via monkey-patch per run, restored in
  finally. Engine source still says 8.
- 7 isolated sandbox SQLite files under ``logs/``.

CONTENTION
==========
Must start at or after 15:30 IST (NSE close). Live engine still
sleeps with its in-memory state until the EOD shutdown / tomorrow's
boot — but ops directive is to not run intensive replays during
the trading window regardless. BelowNormal priority preserved by
the PowerShell launcher.

ETA
===
7 replays × ~30 min each = ~3.5 hr sequential. Start 15:30 ->
done ~19:00 IST.
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

V2B_PATH = PROJECT_ROOT / "models" / "saved" / "ensemble_intraday_v2b.pkl"
V3_PATH = PROJECT_ROOT / "models" / "saved" / "ensemble_intraday_v3.pkl"
LOG_PATH = PROJECT_ROOT / "logs" / "r17_re_baseline_and_sweep.log"
REPORT_PATH = PROJECT_ROOT / "logs" / "r17_re_baseline_and_sweep.md"
TIMELINE_CSV = PROJECT_ROOT / "logs" / "r17_per_day_trades.csv"

HOLDOUT_START = pd.Timestamp("2026-01-01")
HOLDOUT_END = pd.Timestamp("2026-05-28")
PORTFOLIO_VALUE = 500_000.0
NSE_INITIAL_CASH = 500_000.0

# Run plan — (label, ensemble_path, conf_floor, cap)
# The two high-floor runs were added after the R16 cap=20 trade-tape
# (logs/r16_cap20_trade_tape.md) surfaced a 0.235 mean confidence gap
# between wins (0.856) and losses (0.621) — 5 of 6 losses had conf
# < 0.60. The lower conf-floor we've been sweeping was letting bad
# trades through. These two runs test "honest" higher-floor combos.
RUN_PLAN = [
    ("v2b_floor60_cap8_REBASELINE", V2B_PATH, 0.60, 8),
    ("v3_floor55_cap8_REBASELINE",  V3_PATH,  0.55, 8),
    ("v3_floor55_cap10",            V3_PATH,  0.55, 10),
    ("v3_floor55_cap15",            V3_PATH,  0.55, 15),
    ("v3_floor55_cap20",            V3_PATH,  0.55, 20),
    ("v3_floor55_cap25",            V3_PATH,  0.55, 25),
    ("v3_floor55_cap30",            V3_PATH,  0.55, 30),
    # Trade-tape-driven additions (the actual lead)
    ("v3_floor65_cap20",            V3_PATH,  0.65, 20),
    ("v3_floor70_cap30",            V3_PATH,  0.70, 30),
]

SHIP_GATE_N_TRADES = 24
SHIP_GATE_PF = 1.3
DAILY_LOSS_LIMIT_PCT = 0.03
DAILY_LOSS_LIMIT_RS = DAILY_LOSS_LIMIT_PCT * NSE_INITIAL_CASH

# R16 numbers preserved for the diff (only for the runs that overlap)
R16_NUMBERS = {
    "v2b_floor60_cap8_REBASELINE": {"pf": float("inf"), "sharpe": -2.602, "win_rate": 1.000, "max_dd": -0.000, "n_trades": 8,  "note": "R12 baseline"},
    "v3_floor55_cap8_REBASELINE":  {"pf": 1.699,        "sharpe": -2.498, "win_rate": 0.750, "max_dd": 0.011,  "n_trades": 8,  "note": "R15 baseline"},
    "v3_floor55_cap10":            {"pf": 2.562,        "sharpe": -1.628, "win_rate": 0.800, "max_dd": 0.011,  "n_trades": 10, "note": "R16 result"},
    "v3_floor55_cap15":            {"pf": 2.356,        "sharpe": -0.501, "win_rate": 0.733, "max_dd": 0.018,  "n_trades": 15, "note": "R16 result"},
    "v3_floor55_cap20":            {"pf": 2.400,        "sharpe": -0.042, "win_rate": 0.700, "max_dd": 0.021,  "n_trades": 20, "note": "R16 result"},
    "v3_floor55_cap25":            None,
    "v3_floor55_cap30":            None,
    "v3_floor65_cap20":            None,  # NEW — trade-tape lead
    "v3_floor70_cap30":            None,  # NEW — trade-tape lead
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
logger = logging.getLogger("r17_sweep")


def _telegram(msg: str) -> None:
    try:
        from alerts.telegram_bot import send_message  # type: ignore
        send_message(msg)
    except Exception as e:
        logger.warning("Telegram dropped: %s", e)


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


# ── Per-day timeline + risk math ─────────────────────────────────────


def _build_per_day(run_label: str, block_log: Path, sandbox: Path) -> pd.DataFrame:
    if not block_log.exists():
        return pd.DataFrame()
    blocks = pd.read_csv(block_log, parse_dates=["timestamp"])
    blocks["date"] = blocks["timestamp"].dt.date
    per_day_block = (blocks.groupby(["date", "block_reason"]).size()
                     .unstack(fill_value=0))
    cap_block_first = (
        blocks[blocks["block_reason"] == "daily_count_capped"]
        .groupby("date")["timestamp"].min())

    trades_df = pd.DataFrame()
    if sandbox.exists():
        try:
            with sqlite3.connect(str(sandbox)) as conn:
                trades_df = pd.read_sql_query(
                    "SELECT symbol, entry_time, exit_time, net_pnl "
                    "FROM paper_trades", conn,
                    parse_dates=["entry_time", "exit_time"])
        except Exception as e:
            logger.warning("%s sandbox read failed: %s", run_label, e)

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
    rows = []
    for d in all_dates:
        rows.append({
            "run": run_label,
            "date": d,
            "conf_blocked": int(per_day_block.get("conf_blocked", pd.Series()).get(d, 0)),
            "regime_blocked": int(per_day_block.get("regime_blocked", pd.Series()).get(d, 0)),
            "daily_count_capped": int(per_day_block.get("daily_count_capped", pd.Series()).get(d, 0)),
            "exposure_capped": int(per_day_block.get("exposure_capped", pd.Series()).get(d, 0)),
            "first_cap_block_time": (
                cap_block_first.get(d).strftime("%H:%M:%S")
                if d in cap_block_first.index else ""),
            "n_BUYs_opened": int(per_day_opens.get(d, 0)),
            "n_trades_closed": int(per_day_closes.get(d, 0)),
            "daily_net_pnl": round(float(per_day_pnl.get(d, 0.0)), 2),
            "daily_pnl_pct": round(
                float(per_day_pnl.get(d, 0.0)) / NSE_INITIAL_CASH * 100, 3),
        })
    return pd.DataFrame(rows)


def _risk(per_day_df: pd.DataFrame, run_label: str) -> dict:
    sub = per_day_df[per_day_df["run"] == run_label]
    if sub.empty:
        return {"n_days_with_trades": 0, "worst_loss_rs": 0.0,
                "worst_loss_pct": 0.0, "n_halt_firing_days": 0,
                "avg_loss_on_losing_days_rs": 0.0,
                "n_distinct_close_dates": 0}
    worst = float(sub["daily_net_pnl"].min())
    if worst > 0:
        worst = 0.0
    losing = sub[sub["daily_net_pnl"] < 0]
    halt_fire = sub[sub["daily_net_pnl"] <= -DAILY_LOSS_LIMIT_RS]
    return {
        "n_days_with_trades": int((sub["n_trades_closed"] > 0).sum()),
        "worst_loss_rs": worst,
        "worst_loss_pct": worst / NSE_INITIAL_CASH * 100.0,
        "n_halt_firing_days": int(len(halt_fire)),
        "avg_loss_on_losing_days_rs": (
            float(losing["daily_net_pnl"].mean()) if not losing.empty else 0.0),
        "n_distinct_close_dates": int(sub["n_trades_closed"].gt(0).sum()),
    }


# ── Report ───────────────────────────────────────────────────────────


def _ship(metrics: dict, risk: dict) -> bool:
    pf = metrics.get("profit_factor", 0.0)
    n = metrics.get("n_trades", 0)
    pf_ok = (pf == float("inf")) or (pf > SHIP_GATE_PF)
    n_ok = n > SHIP_GATE_N_TRADES
    risk_ok = risk["n_halt_firing_days"] == 0
    return pf_ok and n_ok and risk_ok


def _write_report(run_metrics: dict, run_meta: dict,
                  per_day_df: pd.DataFrame, run_risk: dict) -> tuple[bool, str | None]:
    ship_labels = [lbl for lbl, _, _, _ in RUN_PLAN
                   if _ship(run_metrics[lbl], run_risk[lbl])]
    any_ship = bool(ship_labels)
    best_label = None
    if any_ship:
        def _key(lbl):
            m = run_metrics[lbl]
            pf = m["profit_factor"]
            pf_score = 1e9 if pf == float("inf") else pf
            return (pf_score, m["n_trades"])
        best_label = max(ship_labels, key=_key)

    lines = []
    lines.append("# R17 — re-baseline + re-sweep (replay clock fix in place)")
    lines.append("")
    lines.append(f"**Holdout window:** {HOLDOUT_START.date()} -> {HOLDOUT_END.date()}")
    lines.append(f"**Symbols:** {len(DEFAULT_SYMBOLS)} (DEFAULT_SYMBOLS)")
    lines.append(f"**Clock fix:** SHA 30a20a6 (intraday/engine.py SQL bind + portfolio.datetime patch + FakeDatetime.utcnow)")
    lines.append(f"**SHIP gate:** n_trades > {SHIP_GATE_N_TRADES} AND PF > {SHIP_GATE_PF} AND 0 halt-firing days")
    lines.append("")
    lines.append("## TL;DR — SHIP VERDICT")
    lines.append("")
    if any_ship:
        m = run_metrics[best_label]
        r = run_risk[best_label]
        lines.append(f"**SHIP {best_label}**")
        lines.append("")
        lines.append(f"_n_trades={m['n_trades']}, PF={_fmt_pf(m['profit_factor'])}, "
                     f"worst daily loss=Rs {r['worst_loss_rs']:,.0f} "
                     f"({_fmt(r['worst_loss_pct'])}%)._")
    else:
        lines.append("**NO_SHIP — no run cleared all 3 gates**")
    lines.append("")

    lines.append("## v2b LIVE baseline — R12 fiction vs R17 reality")
    lines.append("")
    v2b_lbl = "v2b_floor60_cap8_REBASELINE"
    if v2b_lbl in run_metrics:
        v2b_m = run_metrics[v2b_lbl]
        v2b_r = run_risk[v2b_lbl]
        lines.append("| Source | n_trades | PF | distinct close-dates | worst loss |")
        lines.append("|---|---:|---:|---:|---:|")
        lines.append(f"| R12 v2b (broken clock) | 8 | infinity | 1 (all 2026-05-28 ish) | n/a (artifact) |")
        lines.append(f"| **R17 v2b (fixed clock)** | **{v2b_m['n_trades']}** | "
                     f"**{_fmt_pf(v2b_m['profit_factor'])}** | "
                     f"**{v2b_r['n_distinct_close_dates']}** | "
                     f"**Rs {v2b_r['worst_loss_rs']:,.0f}** |")
        lines.append("")
        v2b_real_n = v2b_m["n_trades"]
        if v2b_real_n >= 24:
            lines.append(f"**Big finding:** v2b ALREADY trades ~{v2b_real_n} times over "
                         f"the 5-month window in correctly-clocked replay. The 8-trade "
                         f"ceiling we've been chasing was a replay artifact. The "
                         f"\"v2b too selective\" premise is wrong.")
        elif v2b_real_n > 8:
            lines.append(f"v2b trades {v2b_real_n} times (not 8) on the corrected replay — "
                         f"the previous ceiling was partly artifact, partly real. v2b is "
                         f"still under-frequency relative to scalper target ({SHIP_GATE_N_TRADES}+).")
        else:
            lines.append(f"v2b genuinely trades {v2b_real_n} times even with corrected "
                         f"clock — the under-frequency is structural, not artifact. "
                         f"R12-R16 conclusions stand.")
    lines.append("")

    lines.append("## Full comparison — R16 vs R17 numbers")
    lines.append("")
    lines.append("| Variant | R16 PF | R17 PF | R16 n | R17 n | R16 days | R17 days | R17 worst loss | R17 SHIP |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|:---:|")
    for label, _, floor, cap in RUN_PLAN:
        old = R16_NUMBERS.get(label)
        new_m = run_metrics[label]
        new_r = run_risk[label]
        old_pf = _fmt_pf(old["pf"]) if old else "—"
        old_n = str(old["n_trades"]) if old else "—"
        old_days = "1 (artifact)" if old else "—"
        ship_flag = "✓" if _ship(new_m, new_r) else "✗"
        lines.append(
            f"| {label} | {old_pf} | {_fmt_pf(new_m['profit_factor'])} | "
            f"{old_n} | {new_m['n_trades']} | {old_days} | "
            f"{new_r['n_distinct_close_dates']} | "
            f"Rs {new_r['worst_loss_rs']:,.0f} | {ship_flag} |")
    lines.append("")

    # Two ranked views (per ops R17 expansion ask)
    def _pf_key(label):
        pf = run_metrics[label]["profit_factor"]
        return (1e9 if pf == float("inf") else pf)

    by_pf = sorted([lbl for lbl, _, _, _ in RUN_PLAN], key=_pf_key, reverse=True)
    by_n = sorted([lbl for lbl, _, _, _ in RUN_PLAN],
                   key=lambda lbl: run_metrics[lbl]["n_trades"], reverse=True)

    lines.append("## Ranked by PF (descending)")
    lines.append("")
    lines.append("| Rank | Variant | PF | n_trades | Win rate | Halt-firing days | SHIP |")
    lines.append("|---:|---|---:|---:|---:|---:|:---:|")
    for i, lbl in enumerate(by_pf, start=1):
        m = run_metrics[lbl]
        r = run_risk[lbl]
        ship_flag = "✓" if _ship(m, r) else "✗"
        lines.append(
            f"| {i} | {lbl} | {_fmt_pf(m['profit_factor'])} | "
            f"{m['n_trades']} | {_fmt(m['win_rate'])} | "
            f"{r['n_halt_firing_days']} | {ship_flag} |")
    lines.append("")

    lines.append("## Ranked by n_trades (descending)")
    lines.append("")
    lines.append("| Rank | Variant | n_trades | PF | Win rate | Halt-firing days | SHIP |")
    lines.append("|---:|---|---:|---:|---:|---:|:---:|")
    for i, lbl in enumerate(by_n, start=1):
        m = run_metrics[lbl]
        r = run_risk[lbl]
        ship_flag = "✓" if _ship(m, r) else "✗"
        lines.append(
            f"| {i} | {lbl} | {m['n_trades']} | "
            f"{_fmt_pf(m['profit_factor'])} | {_fmt(m['win_rate'])} | "
            f"{r['n_halt_firing_days']} | {ship_flag} |")
    lines.append("")

    lines.append("## Per-day risk (real distribution, post-fix)")
    lines.append("")
    lines.append("| Variant | days w/ trades | worst daily loss (Rs) | worst (%) | halt-firing days | avg loss/losing day |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for label, _, _, _ in RUN_PLAN:
        r = run_risk[label]
        worst = f"{r['worst_loss_rs']:,.0f}" if r["worst_loss_rs"] < 0 else "0"
        avg = f"{r['avg_loss_on_losing_days_rs']:,.0f}" if r["avg_loss_on_losing_days_rs"] < 0 else "0"
        lines.append(f"| {label} | {r['n_days_with_trades']} | {worst} | "
                     f"{r['worst_loss_pct']:.3f}% | {r['n_halt_firing_days']} | {avg} |")
    lines.append("")
    lines.append(f"_DAILY_LOSS_LIMIT = -3% = -Rs {int(DAILY_LOSS_LIMIT_RS):,}. Halt-firing days > 0 = categorical NO_SHIP._")
    lines.append("")

    lines.append("## Run metadata")
    lines.append("")
    lines.append("| Variant | wall_secs | sandbox db | block log |")
    lines.append("|---|---:|---|---|")
    for label, _, _, _ in RUN_PLAN:
        meta = run_meta[label]
        lines.append(
            f"| {label} | {_fmt(meta.get('wall_clock_secs'), 1)} | "
            f"`{Path(meta['sandbox_db_path']).name}` | "
            f"`r17_block_reasons_{label}.csv` |")
    lines.append("")

    lines.append("## Recommendation")
    lines.append("")
    if any_ship:
        m = run_metrics[best_label]
        r = run_risk[best_label]
        lines.append(f"**{best_label}** cleared all 3 gates with real per-day risk distribution.")
        lines.append("")
        if "cap8" in best_label and "v2b" in best_label:
            lines.append("**v2b LIVE config already qualifies** under the SHIP gate when "
                         "measured correctly. This is the cheapest ship of all — no model "
                         "swap, no config change, no cap raise. The R12-R16 saga was "
                         "chasing a phantom. Just need to keep watching live production.")
        else:
            lines.append("Deploy is multi-axis (model + conf + cap, or any subset). Ops "
                         "procedure depends on which axes changed vs current LIVE state.")
    else:
        lines.append("No run cleared SHIP gate with real risk distribution.")
        lines.append("")
        lines.append("If the high-floor runs (floor 0.65 / 0.70) deliver high PF "
                     "but n_trades stays under 24, that's the **honest finding** ops "
                     "flagged: this strategy may be high-quality-low-frequency by "
                     "nature (closer to swing than scalping). A valid result, not a "
                     "failure — informs the next conversation about target SHIP gate "
                     "or about pivoting away from scalper framing.")
        lines.append("")
        lines.append("Possible R18 directions (not implemented):")
        lines.append("1. If high-floor runs deliver PF > 5 but n_trades 10-20: accept v3 @ high-floor as a high-quality swing model, redefine SHIP gate around it (eg n>10 + PF>3).")
        lines.append("2. If trade count rose but PF degraded vs R16: v3 signal quality drops at higher cadence → re-examine sequence layer.")
        lines.append("3. If halt-firing days > 0 at caps that R16 said were safe: cap-raise is genuinely risky in production, regardless of PF.")
        lines.append("4. If v2b real-distribution numbers look much better than R12 suggested: the whole \"v2b too selective\" frame was wrong; focus on operational tuning rather than retraining.")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Report written -> %s (ANY_SHIP=%s, best=%s)",
                REPORT_PATH, any_ship, best_label)
    return any_ship, best_label


# ── Cap monkey-patch helpers ─────────────────────────────────────────


def _patch_cap(value: int):
    import intraday.engine as eng
    original = eng.DAILY_TRADE_CAP
    eng.DAILY_TRADE_CAP = value
    logger.info("Patched DAILY_TRADE_CAP %d -> %d", original, value)
    return original


def _restore_cap(original: int):
    import intraday.engine as eng
    eng.DAILY_TRADE_CAP = original


# ── Main ─────────────────────────────────────────────────────────────


def main() -> int:
    logger.info("=== R17 re-baseline + re-sweep ===")
    logger.info("Holdout: %s -> %s", HOLDOUT_START.date(), HOLDOUT_END.date())
    logger.info("Plan: %d runs", len(RUN_PLAN))
    if not V2B_PATH.exists() or not V3_PATH.exists():
        logger.error("Ensembles missing: v2b=%s v3=%s",
                     V2B_PATH.exists(), V3_PATH.exists())
        _telegram("[R17] ABORT — ensemble pkl missing")
        return 1

    _telegram(
        f"[R17 re-baseline + re-sweep] kickoff — {len(RUN_PLAN)} runs, "
        f"ETA ~{len(RUN_PLAN) * 30} min.")

    run_metrics: dict[str, dict] = {}
    run_meta: dict[str, dict] = {}
    run_runs: dict[str, dict] = {}

    wall_total = time.time()

    for i, (label, ens_path, floor, cap) in enumerate(RUN_PLAN, start=1):
        sandbox = PROJECT_ROOT / "logs" / f"r17_sandbox_{label}.db"
        block_log = PROJECT_ROOT / "logs" / f"r17_block_reasons_{label}.csv"
        if sandbox.exists():
            sandbox.unlink()
        logger.info("--- [%d/%d] %s (floor=%.2f cap=%d) ---",
                    i, len(RUN_PLAN), label, floor, cap)
        original_cap = _patch_cap(cap)
        t0 = time.time()
        try:
            result = run_replay(
                symbols=DEFAULT_SYMBOLS,
                holdout_start=HOLDOUT_START,
                holdout_end=HOLDOUT_END,
                ensemble_path=ens_path,
                sandbox_db_path=sandbox,
                portfolio_value=PORTFOLIO_VALUE,
                progress_every=400,
                block_log_path=block_log,
                conf_floor_override=floor,
            )
        except Exception as e:
            logger.exception("%s CRASHED: %s", label, e)
            _telegram(f"[R17] {label} CRASHED: {type(e).__name__}: {e}")
            _restore_cap(original_cap)
            return 2
        finally:
            _restore_cap(original_cap)

        elapsed = time.time() - t0
        m = result["metrics"]
        logger.info("%s done in %.0fs (%.1f min) metrics=%s",
                    label, elapsed, elapsed / 60, m)
        run_metrics[label] = m
        run_meta[label] = result
        run_runs[label] = {"block_log": block_log, "sandbox": sandbox}

        pf_str = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.3f}"
        _telegram(
            f"[R17] {label} done — n={m['n_trades']}, PF={pf_str}, "
            f"win={m['win_rate']:.3f}, wall={elapsed/60:.1f}min ({i}/{len(RUN_PLAN)}).")

    total = time.time() - wall_total
    logger.info("=== %d runs done in %.0fs (%.1f min) ===",
                len(RUN_PLAN), total, total / 60)

    logger.info("Building per-day timeline + risk math …")
    per_day_frames = []
    for label, _, _, _ in RUN_PLAN:
        rr = run_runs[label]
        df = _build_per_day(label, rr["block_log"], rr["sandbox"])
        per_day_frames.append(df)
    per_day_df = pd.concat(per_day_frames, ignore_index=True) if per_day_frames else pd.DataFrame()
    TIMELINE_CSV.parent.mkdir(parents=True, exist_ok=True)
    per_day_df.to_csv(TIMELINE_CSV, index=False)
    logger.info("Per-day timeline -> %s (%d rows)", TIMELINE_CSV, len(per_day_df))

    run_risk = {label: _risk(per_day_df, label) for label, _, _, _ in RUN_PLAN}

    any_ship, best_label = _write_report(run_metrics, run_meta, per_day_df, run_risk)

    if any_ship:
        m = run_metrics[best_label]
        r = run_risk[best_label]
        pf_str = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.3f}"
        _telegram(
            f"[R17 re-sweep] SHIP — best={best_label} "
            f"n={m['n_trades']}, PF={pf_str}, "
            f"worst=Rs {r['worst_loss_rs']:,.0f}, "
            f"halt-days={r['n_halt_firing_days']}. "
            f"Report {REPORT_PATH.name}.")
    else:
        summary = ", ".join(
            f"{lbl}=n{run_metrics[lbl]['n_trades']}"
            for lbl, _, _, _ in RUN_PLAN[:3])
        _telegram(
            f"[R17 re-sweep] NO_SHIP — {summary} … v2b stays live. "
            f"Report {REPORT_PATH.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
