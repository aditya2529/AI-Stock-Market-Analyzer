"""Forward performance review — compares live paper-trade history against go-live gates.

Run via:  python main.py review
Or:       python main.py review --save   (saves JSON + prints report)

Gates (must ALL pass before going live):
    Sharpe Ratio    > 1.0   (backtest was 3.68 — live is always lower)
    Win Rate        > 52%   (backtest was 74.1%)
    Max Drawdown    < 20%   (backtest was 4%)
    Profit Factor   > 1.1   (backtest was 7.63)
    Min Trades      >= 20   (need enough samples for statistical confidence)
"""
import json
import numpy as np
import pandas as pd
from datetime import datetime, timezone

# Go-live gates (intentionally more lenient than backtest gates)
LIVE_GATE_SHARPE        = 1.0
LIVE_GATE_WIN_RATE      = 0.52
LIVE_GATE_MAX_DRAWDOWN  = 0.20
LIVE_GATE_PROFIT_FACTOR = 1.1
LIVE_GATE_MIN_TRADES    = 20


def _sharpe(returns: pd.Series, periods_per_year: int = 252) -> float:
    returns = returns.dropna()
    if len(returns) < 2:
        return 0.0
    rf = 0.06 / periods_per_year
    excess = returns - rf
    if excess.std() == 0:
        return 0.0
    return float((excess.mean() / excess.std()) * np.sqrt(periods_per_year))


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    roll_max = equity.cummax()
    dd = (equity - roll_max) / (roll_max + 1e-9)
    return float(-dd.min())


def _profit_factor(trades: pd.DataFrame) -> float:
    if trades.empty:
        return 0.0
    gross_win  = trades.loc[trades["net_pnl"] > 0, "net_pnl"].sum()
    gross_loss = trades.loc[trades["net_pnl"] < 0, "net_pnl"].abs().sum()
    return float(gross_win / gross_loss) if gross_loss > 0 else float("inf")


def compute_forward_metrics(trades: pd.DataFrame, equity_log: pd.DataFrame) -> dict:
    """Compute forward metrics from paper trade history."""
    if trades.empty:
        return {
            "n_trades": 0, "sharpe": 0.0, "win_rate": 0.0,
            "max_drawdown": 0.0, "profit_factor": 0.0,
            "total_pnl": 0.0, "cagr": 0.0,
            "days_tracked": 0, "start_date": None, "end_date": None,
        }

    returns = trades["return_pct"].dropna()
    n = len(returns)
    trades_per_year = min(n, 252)

    # Equity curve from portfolio log
    equity = pd.Series(dtype=float)
    days_tracked = 0
    start_date = end_date = None
    if not equity_log.empty:
        equity = pd.to_numeric(equity_log["total_value"], errors="coerce").dropna()
        ts = pd.to_datetime(equity_log["timestamp"])
        start_date = str(ts.min().date())
        end_date   = str(ts.max().date())
        days_tracked = (ts.max() - ts.min()).days + 1

    # CAGR from equity log
    cagr = 0.0
    if len(equity) >= 2 and equity.iloc[0] > 0:
        years = max(days_tracked / 365, 1 / 52)
        cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)

    return {
        "n_trades":      n,
        "sharpe":        _sharpe(returns, periods_per_year=trades_per_year),
        "win_rate":      float((returns > 0).mean()),
        "max_drawdown":  _max_drawdown(equity) if not equity.empty else 0.0,
        "profit_factor": _profit_factor(trades),
        "total_pnl":     float(trades["net_pnl"].sum()),
        "cagr":          cagr,
        "days_tracked":  days_tracked,
        "start_date":    start_date,
        "end_date":      end_date,
    }


def run_review(save: bool = False) -> dict:
    """Load paper trade history, compute forward metrics, check gates."""
    from paper_trading.portfolio import (
        init_paper_tables, get_trade_history, get_portfolio_log,
        get_cash, get_config,
    )
    init_paper_tables()

    trades    = get_trade_history()
    eq_log    = get_portfolio_log()
    initial   = float(get_config("initial_cash", "100000"))
    peak      = float(get_config("peak_value", str(initial))  )
    cash      = get_cash()
    total_val = cash  # conservative — ignores open mark-to-market

    metrics = compute_forward_metrics(trades, eq_log)

    # Per-symbol breakdown
    sym_stats = {}
    if not trades.empty:
        for sym, grp in trades.groupby("symbol"):
            rets = grp["return_pct"].dropna()
            sym_stats[sym] = {
                "n_trades":      len(grp),
                "win_rate":      float((rets > 0).mean()),
                "total_pnl":     float(grp["net_pnl"].sum()),
                "avg_return":    float(rets.mean()),
                "best":          float(rets.max()),
                "worst":         float(rets.min()),
            }

    # Gate checks
    gates = [
        ("Sharpe Ratio",   metrics["sharpe"],        LIVE_GATE_SHARPE,        ">="),
        ("Win Rate",       metrics["win_rate"],       LIVE_GATE_WIN_RATE,      ">="),
        ("Max Drawdown",   metrics["max_drawdown"],   LIVE_GATE_MAX_DRAWDOWN,  "<="),
        ("Profit Factor",  metrics["profit_factor"],  LIVE_GATE_PROFIT_FACTOR, ">="),
        ("Min Trades",     metrics["n_trades"],       LIVE_GATE_MIN_TRADES,    ">="),
    ]
    gate_results = []
    all_pass = True
    for name, value, threshold, op in gates:
        passed = (value >= threshold) if op == ">=" else (value <= threshold)
        if not passed:
            all_pass = False
        gate_results.append({
            "name": name, "value": value, "threshold": threshold,
            "op": op, "passed": passed,
        })

    return {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "metrics":       metrics,
        "gates":         gate_results,
        "all_gates_pass": all_pass,
        "verdict":       "GO LIVE ✓" if all_pass else "CONTINUE PAPER TRADING ✗",
        "portfolio": {
            "initial_cash": initial,
            "current_value": total_val,
            "peak_value": peak,
            "total_return": (total_val - initial) / initial,
        },
        "per_symbol": sym_stats,
    }


def print_review(report: dict):
    m  = report["metrics"]
    pf = report["portfolio"]

    print("\n" + "═"*58)
    print("  AI Stock Analyzer — Forward Performance Review")
    print("═"*58)

    if m["start_date"]:
        print(f"  Period:  {m['start_date']} → {m['end_date']}  ({m['days_tracked']} days)")
    else:
        print("  Period:  No paper trades recorded yet")

    print(f"\n── Portfolio ─────────────────────────────────────────────")
    print(f"  Started with:   ₹{pf['initial_cash']:>12,.2f}")
    print(f"  Current value:  ₹{pf['current_value']:>12,.2f}")
    sign = "+" if pf["total_return"] >= 0 else ""
    print(f"  Total return:   {sign}{pf['total_return']:.2%}")
    print(f"  CAGR:           {m['cagr']:+.2%}")

    print(f"\n── Forward Metrics ───────────────────────────────────────")
    print(f"  Trades:         {m['n_trades']}")
    print(f"  Sharpe:         {m['sharpe']:.2f}  (backtest: 3.68)")
    print(f"  Win Rate:       {m['win_rate']:.1%}  (backtest: 74.1%)")
    print(f"  Max Drawdown:   {m['max_drawdown']:.1%}  (backtest: 4.0%)")
    print(f"  Profit Factor:  {m['profit_factor']:.2f}  (backtest: 7.63)")
    print(f"  Total PnL:      ₹{m['total_pnl']:+,.2f}")

    print(f"\n── Go-Live Gates ─────────────────────────────────────────")
    for g in report["gates"]:
        status = "✓ PASS" if g["passed"] else "✗ FAIL"
        val = g["value"]
        thr = g["threshold"]
        if g["name"] == "Win Rate":
            fmt_val, fmt_thr = f"{val:.1%}", f"{thr:.1%}"
        elif g["name"] == "Min Trades":
            fmt_val, fmt_thr = str(int(val)), str(int(thr))
        else:
            fmt_val, fmt_thr = f"{val:.2f}", f"{thr:.2f}"
        print(f"  {status}  {g['name']:<18} {fmt_val}  (need {g['op']} {fmt_thr})")

    verdict = report["verdict"]
    is_go   = report["all_gates_pass"]
    print(f"\n  {'🟢' if is_go else '🔴'}  {verdict}")

    if not is_go:
        fails = [g["name"] for g in report["gates"] if not g["passed"]]
        if "Min Trades" in fails and m["n_trades"] < LIVE_GATE_MIN_TRADES:
            remaining = LIVE_GATE_MIN_TRADES - m["n_trades"]
            print(f"\n  Need {remaining} more closed trades before statistical confidence is reliable.")
        print(f"  Keep paper trading. Re-run review after more data accumulates.")

    if report["per_symbol"]:
        print(f"\n── Per-Symbol Breakdown ──────────────────────────────────")
        rows = sorted(report["per_symbol"].items(), key=lambda x: -x[1]["total_pnl"])
        for sym, s in rows[:10]:
            bar = "▓" * int(max(0, s["win_rate"]) * 10)
            print(f"  {sym:<22} {s['n_trades']:>3} trades  WR={s['win_rate']:.0%} {bar:<10}  PnL=₹{s['total_pnl']:+,.0f}")

    print()
