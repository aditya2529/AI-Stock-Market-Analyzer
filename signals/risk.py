"""Risk Management Engine — position sizing, stop-loss, target calculation."""
import pandas as pd
from config import (
    MAX_RISK_PCT, ATR_SL_MULTIPLIER, ATR_TARGET_MULTIPLIER, DAILY_LOSS_LIMIT_PCT
)


def calculate_stop_loss(entry_price: float, atr: float, signal: str) -> float:
    """Stop-loss price at 1x ATR from entry."""
    if signal == "BUY":
        return round(entry_price - ATR_SL_MULTIPLIER * atr, 2)
    elif signal == "SELL":
        return round(entry_price + ATR_SL_MULTIPLIER * atr, 2)
    return entry_price


def calculate_target(entry_price: float, atr: float, signal: str) -> float:
    """Target price at 2x ATR from entry."""
    if signal == "BUY":
        return round(entry_price + ATR_TARGET_MULTIPLIER * atr, 2)
    elif signal == "SELL":
        return round(entry_price - ATR_TARGET_MULTIPLIER * atr, 2)
    return entry_price


def risk_reward_ratio(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk == 0:
        return 0.0
    return round(reward / risk, 2)


def position_size(portfolio_value: float, entry_price: float, stop_loss: float,
                  risk_pct: float = MAX_RISK_PCT) -> int:
    """Number of shares to buy given portfolio size and stop-loss distance.

    Uses the fixed-fraction formula:
        shares = (portfolio * risk_pct) / |entry - stop_loss|
    """
    risk_amount = portfolio_value * risk_pct
    risk_per_share = abs(entry_price - stop_loss)
    if risk_per_share == 0:
        return 0
    return max(1, int(risk_amount / risk_per_share))


def compute_stop_and_target(entry_price: float, atr: float,
                            signal: str) -> tuple[float, float]:
    """Convenience wrapper — returns (stop_loss, target) for a given entry."""
    stop = calculate_stop_loss(entry_price, atr, signal)
    target = calculate_target(entry_price, atr, signal)
    return stop, target


def is_daily_loss_limit_breached(portfolio_value: float, start_of_day_value: float) -> bool:
    daily_loss = (portfolio_value - start_of_day_value) / start_of_day_value
    return daily_loss <= -DAILY_LOSS_LIMIT_PCT
