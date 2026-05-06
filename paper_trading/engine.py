"""Paper trading engine — orchestrates feed → signal → execution → portfolio update."""
import logging
import time
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def run_paper_session(symbols: list[str], ensemble,
                      portfolio_value: float = 100_000.0,
                      resolution: str = "1d",
                      interval_sec: int = 300,
                      run_once: bool = False):
    """
    Main paper trading loop.

    Flow per tick:
        1. Fetch latest OHLCV bar for each symbol (refreshes local DB)
        2. Check all open positions for stop/target hits at current price
        3. Engineer features on full history
        4. Generate ensemble signal for latest bar
        5. If BUY and no open position → open at simulated next-open price
        6. If SELL on open position → close at simulated fill price
        7. Snapshot portfolio state to DB

    Args:
        symbols:         Symbols to monitor.
        ensemble:        Loaded Ensemble instance.
        portfolio_value: Starting cash (used for first sizing; subsequent calls read DB).
        resolution:      Bar resolution ('1d').
        interval_sec:    Polling cadence in seconds.
        run_once:        Run a single pass and return (EOD mode).
    """
    from paper_trading.portfolio import (
        init_paper_tables, get_cash, get_open_positions,
        snapshot_portfolio, get_config,
    )
    from paper_trading.executor import try_open, try_close, check_all_stops
    from paper_trading.feed import fetch_latest_bar, fetch_updated_ohlcv, market_is_open, time_to_next_open
    from signals.generator import generate_signal

    init_paper_tables()

    # If this is a fresh portfolio, set starting cash
    if get_config("cash") is None:
        from paper_trading.portfolio import set_cash, set_config
        set_cash(portfolio_value)
        set_config("peak_value", portfolio_value)
        set_config("initial_cash", portfolio_value)

    logger.info("Paper trading engine started | symbols=%d | resolution=%s", len(symbols), resolution)

    while True:
        if not market_is_open() and not run_once:
            wait = time_to_next_open()
            if wait > 120:
                logger.info("Market closed — next open in %.0f min", wait / 60)
                time.sleep(min(wait - 60, 3600))
                continue

        prices = {}

        for sym in symbols:
            try:
                _process_symbol(sym, ensemble, resolution, prices)
            except Exception as e:
                logger.error("%s: error during processing — %s", sym, e)

        # Snapshot portfolio
        if prices:
            state = snapshot_portfolio(prices)
            state["n_open"] = len(get_open_positions())
            logger.info(
                "Portfolio | Cash=%.0f  OpenEq=%.0f  Total=%.0f  DD=%.1f%%",
                state["cash"], state["open_equity"], state["total_value"],
                state["drawdown_pct"] * 100,
            )
            if run_once:
                # EOD mode — send daily summary
                from alerts.dispatcher import on_portfolio_snapshot
                on_portfolio_snapshot(state)

        if run_once:
            break

        logger.info("Sleeping %ds until next poll …", interval_sec)
        time.sleep(interval_sec)


def _process_symbol(symbol: str, ensemble, resolution: str, prices: dict):
    """Process a single symbol: refresh data → check exits → generate signal → maybe open."""
    from paper_trading.executor import try_open, try_close
    from paper_trading.portfolio import get_cash, get_open_positions
    from data.ingestion import get_ohlcv
    from features.engineer import engineer_features

    # Refresh local DB with latest bars
    from paper_trading.feed import fetch_updated_ohlcv
    fetch_updated_ohlcv(symbol, resolution=resolution, days_back=30)

    # Load full history for feature engineering
    df = get_ohlcv(symbol, resolution=resolution)
    if len(df) < 50:
        logger.warning("%s: insufficient history (%d bars), skipping", symbol, len(df))
        return

    featured = engineer_features(df)
    if featured.empty:
        return

    # Track current price
    current_price = float(featured["close"].iloc[-1])
    prices[symbol] = current_price

    # --- Check open position for exit conditions first ---
    from paper_trading.portfolio import get_position
    pos = get_position(symbol)
    if pos:
        result = try_close(symbol, current_price, "HOLD")  # stops / targets only
        if result:
            logger.info("%s: closed via %s | PnL=%.2f", symbol, result["exit_reason"], result["net_pnl"])
            from alerts.dispatcher import on_trade_closed
            on_trade_closed(result)

    # --- Generate signal ---
    try:
        result = ensemble.predict_with_confidence(featured)
        latest = result.iloc[-1]
        signal = latest["signal"]
        regime = latest["regime"]
        confidence = latest.get("confidence", 0.0)
    except Exception as e:
        logger.warning("%s: signal generation failed — %s", symbol, e)
        return

    logger.debug("%s | price=%.2f | signal=%s | regime=%s | conf=%.2f",
                 symbol, current_price, signal, regime, confidence)

    # --- Apply regime gate ---
    if regime in ("HIGH_VOL", "UNKNOWN"):
        signal = "HOLD"

    # --- Execute ---
    pos = get_position(symbol)  # re-check after potential close

    if signal == "SELL" and pos:
        result = try_close(symbol, current_price, "signal")
        if result:
            from alerts.dispatcher import on_trade_closed
            on_trade_closed(result)

    elif signal == "BUY" and pos is None:
        # Build signal_row with stop/target from risk module
        from signals.risk import compute_stop_and_target
        from signals.generator import generate_signal
        atr = float(featured["atr"].iloc[-1])
        stop_loss, target = compute_stop_and_target(current_price, atr, "BUY")
        signal_row = {
            "signal": signal,
            "confidence": float(confidence),
            "regime": regime,
            "stop_loss": stop_loss,
            "target": target,
        }
        cash = get_cash()
        total_value = cash
        next_open = current_price
        opened = try_open(symbol, signal_row, next_open, total_value)
        if opened:
            # Build full signal payload for alert (includes SHAP reasons)
            try:
                from paper_trading.portfolio import get_position
                pos_new = get_position(symbol)
                shares = pos_new["shares"] if pos_new else 0
                alert_payload = generate_signal(symbol, featured, ensemble,
                                                portfolio_value=total_value)
                alert_payload["shares"] = shares
                from alerts.dispatcher import on_signal
                on_signal(alert_payload)
            except Exception as e:
                logger.warning("%s: alert dispatch failed — %s", symbol, e)


def print_paper_status():
    """Print current paper portfolio status to stdout."""
    from paper_trading.portfolio import (
        init_paper_tables, get_cash, get_open_positions,
        get_trade_history, get_config,
    )
    init_paper_tables()

    cash = get_cash()
    positions = get_open_positions()
    trades = get_trade_history()
    initial = float(get_config("initial_cash", "100000"))
    peak = float(get_config("peak_value", str(initial)))

    print("\n── Paper Portfolio Status ─────────────────────────────────")
    print(f"  Cash:         ₹{cash:>12,.2f}")

    if not positions.empty:
        print(f"\n  Open Positions ({len(positions)}):")
        for _, pos in positions.iterrows():
            print(f"    {pos['symbol']:<20} {pos['shares']:>5} shares @ ₹{pos['entry_price']:.2f}"
                  f"  SL=₹{pos['stop_loss']:.2f}  TP=₹{pos['target']:.2f}"
                  f"  [{pos.get('regime', '?')}]")
    else:
        print("  Open Positions: none")

    if not trades.empty:
        total_pnl = trades["net_pnl"].sum()
        wins = (trades["net_pnl"] > 0).sum()
        win_rate = wins / len(trades)
        print(f"\n  Closed Trades:  {len(trades)}")
        print(f"  Total Net PnL:  ₹{total_pnl:+,.2f}")
        print(f"  Win Rate:       {win_rate:.1%}")
        print(f"\n  Last 5 trades:")
        for _, t in trades.head(5).iterrows():
            sign = "+" if t["net_pnl"] >= 0 else ""
            print(f"    {t['symbol']:<20} {sign}₹{t['net_pnl']:,.2f}  ({t['return_pct']:+.2%})  [{t['exit_reason']}]")
    else:
        print("  Closed Trades:  none yet")

    print(f"\n  Peak Value:     ₹{peak:>12,.2f}")
    print(f"  Started with:   ₹{initial:>12,.2f}")
    print()
