"""Paper portfolio endpoints."""
from fastapi import APIRouter, HTTPException
import numpy as np
import pandas as pd


def _clean_for_json(obj):
    """Convert NaN/Inf to None in any DataFrame before JSON serialization.

    FastAPI's default encoder rejects NaN/Inf as non-compliant (HTTP 500 with
    'Out of range float values are not JSON compliant'). Pre-P35 trades have
    NaN in confidence; future drawdown calculations could produce Inf if
    peak_value=0. This helper protects every endpoint from the same class of
    failure. Non-DataFrame inputs pass through unchanged.
    """
    if isinstance(obj, pd.DataFrame):
        return obj.replace([np.nan, np.inf, -np.inf], None).to_dict(orient="records")
    return obj


router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


@router.get("")
def get_portfolio():
    """Current portfolio state — NSE only.

    P36: removed NYSE block entirely. NYSE was never actively traded and the
    phantom cash credits were polluting drawdown + total_value calculations.
    The dashboard's NSE cards read from `nse` block; top-level fields mirror
    the NSE numbers for backward compatibility.
    """
    from paper_trading.portfolio import (
        init_paper_tables, get_market_cash, get_open_positions,
        get_trade_history, get_config,
    )
    init_paper_tables()
    positions = get_open_positions()
    trades    = get_trade_history()
    initial   = float(get_config("initial_cash", "500000"))
    peak      = float(get_config("peak_value", str(initial)))

    # NSE-only filtering — drops any historical NYSE rows (none currently)
    nse_positions = positions[positions["symbol"].str.endswith(".NS")] if not positions.empty else positions
    nse_trades    = trades[trades["symbol"].str.endswith(".NS")] if not trades.empty else trades

    cash    = get_market_cash("nse")
    init    = float(get_config("nse_initial_cash", str(initial)))
    open_eq = float(sum(
        float(r["entry_price"]) * int(r["shares"])
        for _, r in nse_positions.iterrows()
    )) if not nse_positions.empty else 0.0
    total_val = cash + open_eq
    pnl       = float(nse_trades["net_pnl"].sum()) if not nse_trades.empty else 0.0
    win_rate  = float((nse_trades["net_pnl"] > 0).mean()) if not nse_trades.empty else 0.0
    n_trades  = len(nse_trades)
    drawdown  = (peak - total_val) / peak if peak > 0 else 0.0

    nse_block = {
        "cash":           cash,
        "initial_cash":   init,
        "open_equity":    open_eq,
        "total_value":    total_val,
        "total_pnl":      pnl,
        "pnl_pct":        pnl / init if init else 0.0,
        "win_rate":       win_rate,
        "n_trades":       n_trades,
        "open_positions": _clean_for_json(nse_positions),
    }

    return {
        # Top-level fields mirror NSE for backward compat with existing
        # dashboard JS that reads root-level cash/total_value/etc.
        "cash":           cash,
        "total_value":    total_val,
        "peak_value":     peak,
        "initial_cash":   init,
        "drawdown_pct":   drawdown,
        "total_pnl":      pnl,
        "win_rate":       win_rate,
        "n_trades":       n_trades,
        "open_positions": _clean_for_json(nse_positions),
        "nse":            nse_block,
        # NYSE block intentionally removed (P36)
    }


@router.get("/trades")
def get_trades(limit: int = 50):
    """Last N closed trades."""
    from paper_trading.portfolio import init_paper_tables, get_trade_history
    init_paper_tables()
    trades = get_trade_history()
    return _clean_for_json(trades.head(limit))


@router.get("/equity")
def get_equity_curve():
    """Portfolio equity log for charting."""
    from paper_trading.portfolio import init_paper_tables, get_portfolio_log
    init_paper_tables()
    log = get_portfolio_log()
    if log.empty:
        return []
    return _clean_for_json(log[["timestamp", "total_value", "drawdown_pct"]])


@router.get("/positions")
def get_positions():
    """All currently open positions."""
    from paper_trading.portfolio import init_paper_tables, get_open_positions
    init_paper_tables()
    return _clean_for_json(get_open_positions())
