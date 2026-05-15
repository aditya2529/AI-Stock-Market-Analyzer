"""Simulated order execution for paper trading.

Fills are applied at the open of the bar AFTER the signal bar (T+1 open),
matching a realistic live setup where signals are generated after market close
and orders are placed before next day's open.
"""
from __future__ import annotations
import logging
from typing import Optional

from config import BROKERAGE_PCT, SLIPPAGE_PCT, MAX_RISK_PCT
from paper_trading.portfolio import (
    get_cash, get_market_cash, _market_of,
    get_position, open_position, close_position,
    get_open_positions,
)

logger = logging.getLogger(__name__)


def _fill_price(price: float, side: str) -> float:
    """Apply slippage + brokerage to a fill price."""
    adj = BROKERAGE_PCT + SLIPPAGE_PCT
    return price * (1 + adj) if side == "BUY" else price * (1 - adj)


def _position_size(entry_price: float, stop_loss: float,
                   portfolio_value: float) -> int:
    """Fixed-fraction position sizing: risk MAX_RISK_PCT of portfolio per trade."""
    risk_per_share = abs(entry_price - stop_loss)
    if risk_per_share <= 0:
        return 0
    risk_amount = portfolio_value * MAX_RISK_PCT
    shares = int(risk_amount / risk_per_share)
    # Safety: cap position at 20% of portfolio to avoid concentration
    max_shares = int(portfolio_value * 0.20 / entry_price)
    return min(shares, max_shares)


def try_open(symbol: str, signal_row: dict, next_open: float,
             portfolio_value: float) -> Optional[dict]:
    """
    Attempt to open a new long position.

    Args:
        symbol:          Ticker
        signal_row:      Output row from ensemble.predict_with_confidence
        next_open:       Simulated fill price (next bar's open)
        portfolio_value: Current total portfolio value for sizing

    Returns:
        Trade dict if opened, else None.
    """
    if get_position(symbol) is not None:
        logger.debug("%s: already have open position, skipping BUY", symbol)
        return None

    stop_loss = signal_row.get("stop_loss")
    target = signal_row.get("target")
    if stop_loss is None or target is None:
        logger.warning("%s: signal missing stop_loss/target, skipping", symbol)
        return None

    fill = _fill_price(next_open, "BUY")
    shares = _position_size(fill, stop_loss, portfolio_value)
    if shares <= 0:
        logger.warning("%s: position size = 0 (stop too close or cash too low)", symbol)
        return None

    cash = get_market_cash(_market_of(symbol))
    if fill * shares > cash:
        shares = int(cash * 0.95 / fill)  # use up to 95% of cash
        if shares <= 0:
            logger.warning("%s: insufficient %s cash for even 1 share", symbol, _market_of(symbol).upper())
            return None

    open_position(
        symbol=symbol,
        entry_price=fill,
        shares=shares,
        stop_loss=stop_loss,
        target=target,
        confidence=signal_row.get("confidence"),
        regime=signal_row.get("regime"),
    )
    logger.info("OPEN  %-20s fill=%.2f  shares=%d  SL=%.2f  TP=%.2f",
                symbol, fill, shares, stop_loss, target)
    return {"action": "open", "symbol": symbol, "fill": fill, "shares": shares}


def try_close(symbol: str, current_price: float, signal: str) -> Optional[dict]:
    """
    Close an open position if exit conditions are met.

    Exit triggers (in priority order):
        1. Stop-loss hit (current_price <= stop_loss)
        2. Target hit (current_price >= target)
        3. SELL signal from ensemble
    """
    pos = get_position(symbol)
    if pos is None:
        return None

    reason = None
    if current_price <= pos["stop_loss"]:
        reason = "stop_loss"
    elif current_price >= pos["target"]:
        reason = "target"
    elif signal == "SELL":
        reason = "signal"
    elif isinstance(signal, str) and signal.startswith("force_close"):
        # P26: force-close signals (e.g. "force_close_eod") must close
        # unconditionally — even when price is between SL and TP. Prior
        # bug: try_close silently rejected the close, _force_close_all
        # still returned True, and forced_closed_<date> flag was set
        # while positions stayed open over weekend.
        reason = signal

    if reason is None:
        return None

    fill = _fill_price(current_price, "SELL")
    result = close_position(symbol, fill, reason)
    pnl_sign = "+" if result["net_pnl"] >= 0 else ""
    logger.info("CLOSE %-20s fill=%.2f  pnl=%s%.2f  reason=%s",
                symbol, fill, pnl_sign, result["net_pnl"], reason)
    return result


def check_all_stops(current_prices: dict) -> list[dict]:
    """Check every open position against current prices for stop/target hits."""
    closed = []
    positions = get_open_positions()
    for _, pos in positions.iterrows():
        sym = pos["symbol"]
        price = current_prices.get(sym)
        if price is None:
            continue
        result = try_close(sym, price, "HOLD")  # no signal override — stops only
        if result:
            closed.append(result)
    return closed
