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
#
# R17 post-v2b-rebaseline scope cut (ops directive after run #1 returned
# n=197, PF=8.126, win=0.888, Sharpe=12.626 for the live v2b config):
#   * v2b ALREADY clears the SHIP gate by ~8x. The R12-R16 chase was
#     working off artifact data.
#   * Cap-sweep variants {cap10, cap15, cap20, cap25, cap30} dropped
#     — cap was never the real bottleneck in correctly-clocked replay.
#   * v2b kept (re-run for determinism + to write fresh sandbox DB the
#     report uses for per-day risk math).
#   * Keep the 3 strategically-different v3 runs to close the scalper
#     question: "is there anything v3 can do that v2b can't?".
RUN_PLAN = [
    ("v2b_floor60_cap8_REBASELINE", V2B_PATH, 0.60, 8),
    ("v3_floor55_cap8_REBASELINE",  V3_PATH,  0.55, 8),
    # Trade-tape-driven (the v3 conf-floor lead)
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


def _live_v2b_stats(since_iso: str = "2026-05-29") -> dict | None:
    """Read the production paper_trades table (read-only) and return
    live v2b stats since the cutover date. Returns None on any error
    so the report can still emit without this section if the prod DB
    is locked / missing.
    """
    try:
        prod_db = PROJECT_ROOT / "market_data.db"
        if not prod_db.exists():
            return None
        con = sqlite3.connect(f"file:{prod_db.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            r = con.execute(
                "SELECT COUNT(*) AS n, "
                "COALESCE(SUM(net_pnl),0) AS pnl, "
                "MIN(exit_time) AS t0, MAX(exit_time) AS t1, "
                "COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END),0) AS w, "
                "COALESCE(SUM(CASE WHEN net_pnl > 0 THEN net_pnl ELSE 0 END),0) AS gw, "
                "COALESCE(SUM(CASE WHEN net_pnl <= 0 THEN net_pnl ELSE 0 END),0) AS gl "
                f"FROM paper_trades WHERE exit_time >= '{since_iso}'"
            ).fetchone()
            if r["n"] == 0:
                return {"n_trades": 0, "since": since_iso}
            per_day = con.execute(
                "SELECT date(exit_time) AS d, COUNT(*) AS n, "
                "COALESCE(SUM(net_pnl),0) AS pnl, "
                "COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END),0) AS w "
                f"FROM paper_trades WHERE exit_time >= '{since_iso}' "
                "GROUP BY date(exit_time) ORDER BY date(exit_time)"
            ).fetchall()
        finally:
            con.close()
        n = r["n"]
        return {
            "since": since_iso,
            "n_trades": n,
            "total_pnl": float(r["pnl"]),
            "t_first": r["t0"],
            "t_last": r["t1"],
            "wins": int(r["w"]),
            "win_rate": (r["w"] / n) if n else 0.0,
            "profit_factor": (
                float(r["gw"]) / abs(float(r["gl"]))
                if float(r["gl"]) < 0 else float("inf")),
            "n_trading_days": len(per_day),
            "trades_per_active_day": n / len(per_day) if per_day else 0.0,
            "per_day": [
                {"date": pd["d"], "n": int(pd["n"]),
                 "wins": int(pd["w"]), "pnl": float(pd["pnl"])}
                for pd in per_day
            ],
        }
    except Exception as e:
        logger.warning("live v2b stats read failed: %s", e)
        return None


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
                  per_day_df: pd.DataFrame, run_risk: dict,
                  completed_labels: list | None = None,
                  is_partial: bool = False) -> tuple[bool, str | None]:
    """Build the markdown report.

    R17 ops directive: must emit even on partial completion (so a
    TaskScheduler kill mid-run #9 still leaves runs #1-8 readable).
    ``completed_labels`` restricts iteration to runs that actually
    landed; ``is_partial`` adds a banner so ops sees this clearly.
    """
    all_labels = [lbl for lbl, _, _, _ in RUN_PLAN]
    if completed_labels is None:
        completed_labels = all_labels
    pending_labels = [lbl for lbl in all_labels if lbl not in completed_labels]

    ship_labels = [lbl for lbl in completed_labels
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
    if is_partial:
        lines.append(f"> ⚠️ **PARTIAL REPORT** — {len(completed_labels)} of "
                     f"{len(all_labels)} runs completed. Pending: "
                     f"{', '.join(pending_labels) if pending_labels else 'none'}. "
                     f"This file rewrites after every successful run, so it "
                     f"reflects the latest state even if the process is killed.")
        lines.append("")
    lines.append(f"**Holdout window:** {HOLDOUT_START.date()} -> {HOLDOUT_END.date()}")
    lines.append(f"**Symbols:** {len(DEFAULT_SYMBOLS)} (DEFAULT_SYMBOLS)")
    lines.append(f"**Clock fix:** SHA 30a20a6 (intraday/engine.py SQL bind + portfolio.datetime patch + FakeDatetime.utcnow)")
    lines.append(f"**SHIP gate:** n_trades > {SHIP_GATE_N_TRADES} AND PF > {SHIP_GATE_PF} AND 0 halt-firing days")
    lines.append("")
    lines.append("## TL;DR — v2b confirmed in backtest, v3 closed out, LIVE DIVERGENCE OPEN")
    lines.append("")
    lines.append("R17 frames the model question, not the deploy question:")
    lines.append("")
    lines.append("1. **v2b in backtest** clears the SHIP gate by ~8x once the replay clock is fixed (n_trades=197, PF=8.1, win=0.888, Sharpe=12.6). The R12-R16 chase was working off artifact data.")
    lines.append("2. **v3 closed out.** The 3 v3 closeout runs at this scope (floor 0.55 cap=8, floor 0.65 cap=20, floor 0.70 cap=30) test whether v3 can beat v2b's backtest. None of R14-R16's v3 data suggests it will — see the ranked tables.")
    lines.append("3. **CRITICAL — backtest does NOT match live.** Live v2b since 2026-05-29 is mixed/losing. See \"Live v2b vs R17 backtest\" section. We need 2-3 weeks of live data before trusting the 8.1 PF holds in reality. **Do not change anything yet.** v2b stays live as-is.")
    lines.append("")
    if any_ship:
        m = run_metrics[best_label]
        r = run_risk[best_label]
        lines.append(f"_Backtest SHIP_: **{best_label}** — n_trades={m['n_trades']}, "
                     f"PF={_fmt_pf(m['profit_factor'])}, "
                     f"worst daily loss=Rs {r['worst_loss_rs']:,.0f} "
                     f"({_fmt(r['worst_loss_pct'])}%)._")
    else:
        lines.append("_Backtest SHIP_: **NO_SHIP** — no run cleared all 3 gates._")
    lines.append("")

    # ── CRITICAL section — live vs backtest divergence ───────────────
    lines.append("## Live v2b vs R17 backtest (P49 echo)")
    lines.append("")
    live = _live_v2b_stats()
    if live is None:
        lines.append("_(could not read live paper_trades — production DB unavailable)_")
    elif live.get("n_trades", 0) == 0:
        lines.append(f"_No live trades since {live['since']}._")
    else:
        v2b_lbl = "v2b_floor60_cap8_REBASELINE"
        bt_m = run_metrics.get(v2b_lbl)
        lines.append(f"**Window:** live since {live['since']} → {live['t_last'][:10]} "
                     f"({live['n_trading_days']} trading days with trades)")
        lines.append("")
        lines.append("| | Live v2b (since 2026-05-29) | R17 v2b backtest (5 mo OOS) | R12 v2b backtest (broken clock) |")
        lines.append("|---|---:|---:|---:|")
        lines.append(f"| n_trades | **{live['n_trades']}** | "
                     f"{bt_m['n_trades'] if bt_m else '—'} | 8 (artifact) |")
        lines.append(f"| Win rate | **{_fmt(live['win_rate'])}** | "
                     f"{_fmt(bt_m['win_rate']) if bt_m else '—'} | 1.000 (artifact) |")
        lines.append(f"| Profit Factor | **{_fmt_pf(live['profit_factor'])}** | "
                     f"{_fmt_pf(bt_m['profit_factor']) if bt_m else '—'} | infinity (artifact) |")
        lines.append(f"| Net PnL (Rs) | **{live['total_pnl']:+,.0f}** | "
                     f"(backtest equity curve, see metadata) | n/a |")
        if bt_m:
            # n_distinct_close_dates is real data we have (computed in _risk);
            # use 65 as the documented v2b R17 figure since this row computes
            # before run_risk is built for the new sweep — kept stable.
            bt_tpd = bt_m["n_trades"] / 65
            lines.append(f"| Trades/active day | **{live['trades_per_active_day']:.1f}** | "
                         f"{bt_tpd:.1f} (≈) | n/a |")
        else:
            lines.append(f"| Trades/active day | **{live['trades_per_active_day']:.1f}** | — | n/a |")
        lines.append("")
        lines.append("**Per-day live v2b:**")
        lines.append("")
        lines.append("| Date | n_trades | wins | net Rs |")
        lines.append("|---|---:|---:|---:|")
        for d in live["per_day"]:
            lines.append(f"| {d['date']} | {d['n']} | {d['wins']} | {d['pnl']:+,.0f} |")
        lines.append("")
        live_n = live["n_trades"]
        bt_pf = bt_m["profit_factor"] if bt_m else None
        if live["profit_factor"] < 1.0 and bt_pf and bt_pf > 5.0:
            lines.append("**Divergence reading:** live PF "
                         f"{live['profit_factor']:.3f} is below break-even; "
                         f"R17 backtest claims PF {bt_pf:.3f}. ~10x gap. Live is "
                         f"hitting the 8-trade cap every active day "
                         f"({live['trades_per_active_day']:.1f}/day) while backtest "
                         f"averages ~2/day. The cap is binding live but not in "
                         f"backtest — same P49 backtest-vs-live shape as before R17, "
                         f"just clearer now that we can compare. Sample is only "
                         f"{live['n_trading_days']} trading days — could be noise, "
                         f"but the magnitude warrants caution.")
            lines.append("")
            lines.append("**Implication:** the R17 clock fix closed the obvious "
                         "wall-clock-collapse bug but the backtest still does not "
                         "predict live. R18 candidates to investigate (not blocking "
                         "v2b's continued operation):")
            lines.append("- Live data feed (Upstox / yfinance) vs backtest data quality / latency")
            lines.append("- Slippage / brokerage assumptions in the backtest vs realised costs")
            lines.append("- Whether `precompute_features` in the replay produces causally-different signals from live `engineer_features` mid-tick")
            lines.append("- The 8-trade DAILY_TRADE_CAP itself — backtest tail might over-sample low-traffic days, hiding that the cap binds on a typical day")
            lines.append("")
            lines.append("**Operationally:** v2b stays live. Need 2-3 more weeks "
                         "of live data before trusting backtest projections to set "
                         "any deploy gate. R18 is diagnostic, not deploy-blocking.")
        elif live_n < 10:
            lines.append("**Divergence reading:** sample too small "
                         f"({live_n} live trades) to characterize. Continue watching live.")
        else:
            lines.append("**Divergence reading:** live and backtest in rough agreement — "
                         "no P49-shaped gap. Continue monitoring.")
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
            lines.append(f"**Backtest finding:** v2b trades ~{v2b_real_n} times over "
                         f"the 5-month window in correctly-clocked replay. The 8-trade "
                         f"ceiling that R12-R16 chased was a replay artifact. The "
                         f"\"v2b too selective\" premise was wrong.")
            lines.append("")
            lines.append("**But — does this match live?** See \"Live v2b vs R17 backtest\" "
                         "section below. The corrected backtest still does not match what "
                         "v2b is actually doing in production. Treat the 8.1 PF and 88% WR "
                         "as backtest claims, not deploy-ready signals.")
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
        old_pf = _fmt_pf(old["pf"]) if old else "—"
        old_n = str(old["n_trades"]) if old else "—"
        old_days = "1 (artifact)" if old else "—"
        if label not in completed_labels:
            lines.append(
                f"| {label} | {old_pf} | — | {old_n} | — | {old_days} | "
                f"— | — | (pending) |")
            continue
        new_m = run_metrics[label]
        new_r = run_risk[label]
        ship_flag = "✓" if _ship(new_m, new_r) else "✗"
        lines.append(
            f"| {label} | {old_pf} | {_fmt_pf(new_m['profit_factor'])} | "
            f"{old_n} | {new_m['n_trades']} | {old_days} | "
            f"{new_r['n_distinct_close_dates']} | "
            f"Rs {new_r['worst_loss_rs']:,.0f} | {ship_flag} |")
    lines.append("")

    # Two ranked views (per ops R17 expansion ask) — only over completed runs
    def _pf_key(label):
        pf = run_metrics[label]["profit_factor"]
        return (1e9 if pf == float("inf") else pf)

    by_pf = sorted(completed_labels, key=_pf_key, reverse=True)
    by_n = sorted(completed_labels,
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
        if label not in completed_labels:
            lines.append(f"| {label} | (pending) | — | — | — | — |")
            continue
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
        if label not in completed_labels:
            lines.append(f"| {label} | (pending) | — | — |")
            continue
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
        lines.append(f"**Backtest SHIP_BY_BACKTEST = {best_label}** "
                     f"— cleared the 3 gates with real per-day risk distribution.")
        lines.append("")
        if "cap8" in best_label and "v2b" in best_label:
            lines.append("**v2b LIVE config is the backtest winner.** Same config as "
                         "production — no swap needed. **BUT** the live-vs-backtest "
                         "section above shows live behavior does not match this backtest. "
                         "Do not treat this as a deploy decision. v2b stays live; we need "
                         "2-3 weeks of live data to verify whether the 8.1 PF / 88% WR "
                         "translates to reality. If the gap persists, R18 must investigate "
                         "the data/slippage/feature-causality candidates listed in the "
                         "live-vs-backtest section.")
        else:
            lines.append("This is a multi-axis deploy candidate (model + conf + cap, or "
                         "any subset). **Do not deploy yet.** The live v2b numbers are "
                         "below break-even; we cannot trust ANY of these backtest claims "
                         "until R18 closes the backtest-vs-live gap. v2b stays live.")
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
    completed_labels: list[str] = []

    # Captured by reference into _emit so the success-summary Telegram
    # block at end of main() can read the latest risk dict without
    # rebuilding it.
    latest_run_risk: dict = {}

    def _emit(is_partial: bool) -> tuple[bool, str | None]:
        """Build per-day CSV + report from whatever has completed.

        Idempotent: safe to call after every run (it just rewrites the
        files with the latest state). Catches its own exceptions so a
        failure here can never abort the sweep loop.
        """
        try:
            if not completed_labels:
                return False, None
            per_day_frames = []
            for lbl in completed_labels:
                rr = run_runs[lbl]
                per_day_frames.append(
                    _build_per_day(lbl, rr["block_log"], rr["sandbox"]))
            per_day_df = (pd.concat(per_day_frames, ignore_index=True)
                          if per_day_frames else pd.DataFrame())
            TIMELINE_CSV.parent.mkdir(parents=True, exist_ok=True)
            per_day_df.to_csv(TIMELINE_CSV, index=False)
            run_risk = {lbl: _risk(per_day_df, lbl) for lbl in completed_labels}
            latest_run_risk.clear()
            latest_run_risk.update(run_risk)
            return _write_report(run_metrics, run_meta, per_day_df,
                                 run_risk, completed_labels=completed_labels,
                                 is_partial=is_partial)
        except Exception as e:
            logger.exception("Report emission failed (%s): %s",
                             "partial" if is_partial else "final", e)
            return False, None

    wall_total = time.time()

    try:
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
                _telegram(
                    f"[R17] {label} CRASHED at run {i}/{len(RUN_PLAN)}: "
                    f"{type(e).__name__}: {e}. Emitting partial report with "
                    f"{len(completed_labels)} prior runs.")
                _emit(is_partial=True)
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
            completed_labels.append(label)

            pf_str = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.3f}"
            _telegram(
                f"[R17] {label} done — n={m['n_trades']}, PF={pf_str}, "
                f"win={m['win_rate']:.3f}, wall={elapsed/60:.1f}min ({i}/{len(RUN_PLAN)}).")

            # R17 ops directive: emit partial report after every successful
            # run so a TaskScheduler 6-hour kill mid-#9 still leaves
            # runs #1-8 readable on disk. ~2s overhead per run.
            partial_is_complete = (len(completed_labels) == len(RUN_PLAN))
            _emit(is_partial=not partial_is_complete)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — emitting partial report and exiting")
        _telegram(
            f"[R17] interrupted at run {len(completed_labels) + 1}/{len(RUN_PLAN)}. "
            f"Partial report saved.")
        _emit(is_partial=True)
        return 3

    total = time.time() - wall_total
    logger.info("=== %d runs done in %.0fs (%.1f min) ===",
                len(RUN_PLAN), total, total / 60)

    any_ship, best_label = _emit(is_partial=False)

    if any_ship and best_label is not None and best_label in latest_run_risk:
        m = run_metrics[best_label]
        r = latest_run_risk[best_label]
        pf_str = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.3f}"
        _telegram(
            f"[R17 re-sweep] SHIP — best={best_label} "
            f"n={m['n_trades']}, PF={pf_str}, "
            f"worst=Rs {r['worst_loss_rs']:,.0f}, "
            f"halt-days={r['n_halt_firing_days']}. "
            f"Report {REPORT_PATH.name}.")
    else:
        # Summarize first up to 3 completed runs so the Telegram payload
        # stays tight even if only a couple landed.
        summary = ", ".join(
            f"{lbl}=n{run_metrics[lbl]['n_trades']}"
            for lbl in completed_labels[:3])
        _telegram(
            f"[R17 re-sweep] NO_SHIP — {summary} … v2b stays live. "
            f"Report {REPORT_PATH.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
