"""Paper portfolio endpoints."""
from fastapi import APIRouter, HTTPException
import pandas as pd

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


@router.get("")
def get_portfolio():
    """Current portfolio state — cash, open positions, summary stats."""
    from paper_trading.portfolio import (
        init_paper_tables, get_cash, get_open_positions,
        get_trade_history, get_config, get_portfolio_log,
    )
    init_paper_tables()
    cash = get_cash()
    positions = get_open_positions()
    trades = get_trade_history()
    initial = float(get_config("initial_cash", "100000"))
    peak = float(get_config("peak_value", str(initial)))

    total_pnl = float(trades["net_pnl"].sum()) if not trades.empty else 0.0
    win_rate  = float((trades["net_pnl"] > 0).mean()) if not trades.empty else 0.0
    n_trades  = len(trades)
    total_value = cash + sum(
        float(r["entry_price"]) * int(r["shares"]) for _, r in positions.iterrows()
    ) if not positions.empty else cash
    drawdown = (peak - total_value) / peak if peak > 0 else 0.0

    return {
        "cash": cash,
        "total_value": total_value,
        "peak_value": peak,
        "initial_cash": initial,
        "drawdown_pct": drawdown,
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "n_trades": n_trades,
        "open_positions": positions.to_dict(orient="records"),
    }


@router.get("/trades")
def get_trades(limit: int = 50):
    """Last N closed trades."""
    from paper_trading.portfolio import init_paper_tables, get_trade_history
    init_paper_tables()
    trades = get_trade_history()
    return trades.head(limit).to_dict(orient="records")


@router.get("/equity")
def get_equity_curve():
    """Portfolio equity log for charting."""
    from paper_trading.portfolio import init_paper_tables, get_portfolio_log
    init_paper_tables()
    log = get_portfolio_log()
    if log.empty:
        return []
    return log[["timestamp", "total_value", "drawdown_pct"]].to_dict(orient="records")


@router.get("/positions")
def get_positions():
    """All currently open positions."""
    from paper_trading.portfolio import init_paper_tables, get_open_positions
    init_paper_tables()
    return get_open_positions().to_dict(orient="records")
