"""Paper portfolio endpoints."""
from fastapi import APIRouter, HTTPException
import pandas as pd

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


@router.get("")
def get_portfolio():
    """Current portfolio state — combined + per-market breakdown."""
    from paper_trading.portfolio import (
        init_paper_tables, get_cash, get_market_cash, get_open_positions,
        get_trade_history, get_config,
    )
    init_paper_tables()
    positions = get_open_positions()
    trades    = get_trade_history()
    initial   = float(get_config("initial_cash", "200000"))
    peak      = float(get_config("peak_value", str(initial)))

    def _market_stats(mkt):
        is_nse = mkt == "nse"
        mkt_positions = positions[
            positions["symbol"].str.endswith(".NS") == is_nse
        ] if not positions.empty else positions
        mkt_trades = trades[
            trades["symbol"].str.endswith(".NS") == is_nse
        ] if not trades.empty else trades

        cash       = get_market_cash(mkt)
        init_cash  = float(get_config(f"{mkt}_initial_cash", str(initial / 2)))
        open_eq    = float(sum(
            float(r["entry_price"]) * int(r["shares"])
            for _, r in mkt_positions.iterrows()
        )) if not mkt_positions.empty else 0.0
        total_val  = cash + open_eq
        pnl        = float(mkt_trades["net_pnl"].sum()) if not mkt_trades.empty else 0.0
        win_rate   = float((mkt_trades["net_pnl"] > 0).mean()) if not mkt_trades.empty else 0.0
        n_trades   = len(mkt_trades)
        return {
            "cash": cash,
            "initial_cash": init_cash,
            "open_equity": open_eq,
            "total_value": total_val,
            "total_pnl": pnl,
            "pnl_pct": pnl / init_cash if init_cash else 0.0,
            "win_rate": win_rate,
            "n_trades": n_trades,
            "open_positions": mkt_positions.to_dict(orient="records"),
        }

    nse_stats  = _market_stats("nse")
    nyse_stats = _market_stats("nyse")

    total_cash  = nse_stats["cash"] + nyse_stats["cash"]
    total_value = nse_stats["total_value"] + nyse_stats["total_value"]
    total_pnl   = nse_stats["total_pnl"] + nyse_stats["total_pnl"]
    all_trades  = trades if not trades.empty else trades
    win_rate    = float((all_trades["net_pnl"] > 0).mean()) if not all_trades.empty else 0.0
    drawdown    = (peak - total_value) / peak if peak > 0 else 0.0

    return {
        # Combined
        "cash":          total_cash,
        "total_value":   total_value,
        "peak_value":    peak,
        "initial_cash":  initial,
        "drawdown_pct":  drawdown,
        "total_pnl":     total_pnl,
        "win_rate":      win_rate,
        "n_trades":      len(trades),
        "open_positions": positions.to_dict(orient="records"),
        # Per-market
        "nse":  nse_stats,
        "nyse": nyse_stats,
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
