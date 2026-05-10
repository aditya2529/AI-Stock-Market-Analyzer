"""Intraday trading engine — runs every 5 minutes from 9:15 AM to 3:15 PM IST.

Flow each tick:
    1. Fetch latest 5-min bars for all active symbols
    2. Check open positions: stop-loss / target hit?
    3. Generate signal for each symbol
    4. Open new positions (max INTRADAY_MAX_POSITIONS at once)
    5. At 3:15 PM: force-close all open positions
    6. Send Telegram alert on every BUY/SELL/close event
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    INTRADAY_RESOLUTION, INTRADAY_SIGNAL_INTERVAL,
    INTRADAY_FORCE_CLOSE_TIME, INTRADAY_MAX_POSITIONS,
)

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# Fix 2: Per-symbol stop-loss cooldown — reset each session
_sl_cooldown: set = set()   # symbols blocked for re-entry today after SL hit


def _ist_now() -> datetime:
    return datetime.now(IST)


def _market_open() -> bool:
    now = _ist_now()
    if now.weekday() >= 5:
        return False
    open_  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_ = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_ <= now <= close_


def _should_force_close() -> bool:
    now = _ist_now()
    fh, fm = INTRADAY_FORCE_CLOSE_TIME
    return now.hour > fh or (now.hour == fh and now.minute >= fm)


def _seconds_to_next_bar() -> float:
    """Seconds until the next 5-min bar boundary."""
    now = _ist_now()
    minutes_past = now.minute % INTRADAY_SIGNAL_INTERVAL
    seconds_past = minutes_past * 60 + now.second
    return max(0, INTRADAY_SIGNAL_INTERVAL * 60 - seconds_past)


def _fetch_intraday(symbol: str) -> pd.DataFrame | None:
    """Fetch latest 5-min bars for a symbol."""
    try:
        import yfinance as yf
        df = yf.download(symbol, period="5d", interval="5m",
                         progress=False, auto_adjust=True)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index).tz_localize(None) \
            if df.index.tz is None else pd.to_datetime(df.index).tz_convert("Asia/Kolkata").tz_localize(None)
        return df
    except Exception as e:
        logger.warning("fetch_intraday(%s): %s", symbol, e)
        return None


def _process_symbol(symbol: str, ensemble, portfolio_value: float) -> dict | None:
    """Fetch, engineer, generate signal, execute. Returns action taken or None."""
    from features.engineer import engineer_features
    from paper_trading.portfolio import get_position, get_cash
    from paper_trading.executor import try_open, try_close
    from signals.risk import compute_stop_and_target
    from config import INTRADAY_BUY_THRESHOLD, INTRADAY_SELL_THRESHOLD

    df = _fetch_intraday(symbol)
    if df is None or len(df) < 30:
        return None

    try:
        featured = engineer_features(df)
        if featured.empty:
            return None
    except Exception as e:
        logger.warning("%s feature engineering failed: %s", symbol, e)
        return None

    current_price = float(featured["close"].iloc[-1])

    # Check open position for stop/target first
    pos = get_position(symbol)
    if pos:
        result = try_close(symbol, current_price, "HOLD")
        if result:
            # Fix 2: add to cooldown if exit was stop-loss
            if result.get("exit_reason") == "stop_loss":
                _sl_cooldown.add(symbol)
                logger.info("SL cooldown: %s blocked for rest of session", symbol)
            from alerts.dispatcher import on_trade_closed
            on_trade_closed(result)
            return result

    # Generate signal
    try:
        result = ensemble.predict_with_confidence(featured)
        latest  = result.iloc[-1]
        signal  = latest["signal"]
        regime  = latest["regime"]
        confidence = float(latest.get("confidence", 0))
    except Exception as e:
        logger.warning("%s signal failed: %s", symbol, e)
        return None

    # Regime gate
    if regime in ("HIGH_VOL", "UNKNOWN"):
        signal = "HOLD"

    pos = get_position(symbol)

    if signal == "SELL" and pos:
        result = try_close(symbol, current_price, "signal")
        if result:
            from alerts.dispatcher import on_trade_closed
            on_trade_closed(result)
        return result

    if signal == "BUY" and pos is None:
        # Fix 2: block re-entry if symbol hit SL earlier today
        if symbol in _sl_cooldown:
            logger.debug("SL cooldown active — skipping BUY for %s", symbol)
            return None
        from paper_trading.portfolio import get_open_positions
        if len(get_open_positions()) >= INTRADAY_MAX_POSITIONS:
            logger.debug("%s: max positions reached, skipping BUY", symbol)
            return None

        atr = float(featured["atr"].iloc[-1])
        stop_loss, target = compute_stop_and_target(current_price, atr, "BUY")
        signal_row = {
            "signal": signal, "confidence": confidence,
            "regime": regime, "stop_loss": stop_loss, "target": target,
        }
        opened = try_open(symbol, signal_row, current_price, get_cash())
        if opened:
            try:
                from signals.generator import generate_signal
                alert_payload = generate_signal(symbol, featured, ensemble,
                                                portfolio_value=portfolio_value)
                from alerts.dispatcher import on_signal
                on_signal(alert_payload)
            except Exception:
                pass
        return opened

    return None


def _force_close_all():
    """Close every open position at market price — called at 3:15 PM."""
    from paper_trading.portfolio import get_open_positions
    from paper_trading.executor import try_close
    from alerts.dispatcher import on_trade_closed

    positions = get_open_positions()
    if positions.empty:
        return

    logger.info("3:15 PM — force closing %d open positions", len(positions))
    for _, pos in positions.iterrows():
        sym = pos["symbol"]
        df = _fetch_intraday(sym)
        price = float(df["close"].iloc[-1]) if df is not None and not df.empty \
            else float(pos["entry_price"])
        result = try_close(sym, price, "force_close_eod")
        if result:
            on_trade_closed(result)


def run_intraday_session(symbols: list[str], ensemble, portfolio_value: float = 100_000.0):
    """
    Main intraday loop. Runs every 5 minutes from 9:15 AM to 3:30 PM IST.
    Call this once at 9:15 AM — it blocks until market close.
    """
    from paper_trading.portfolio import init_paper_tables, get_config, set_cash, set_config, snapshot_portfolio
    from alerts.dispatcher import on_portfolio_snapshot

    _sl_cooldown.clear()   # Fix 2: fresh cooldown each session
    init_paper_tables()
    if get_config("cash") is None:
        set_cash(portfolio_value)
        set_config("peak_value", portfolio_value)
        set_config("initial_cash", portfolio_value)

    logger.info("Intraday session started | %d symbols | 5-min bars", len(symbols))
    print(f"\n  Intraday session running — {len(symbols)} symbols")
    print(f"  Signals every 5 min | Force close at 3:15 PM IST")
    print(f"  Press Ctrl+C to stop early (open positions will remain open)\n")

    forced_closed = False

    while True:
        now = _ist_now()

        if not _market_open():
            print(f"  [{now.strftime('%H:%M')}] Market closed. Session ended.")
            break

        # Force close at 3:15 PM
        if _should_force_close() and not forced_closed:
            _force_close_all()
            forced_closed = True
            prices = {}
            state = snapshot_portfolio(prices)
            on_portfolio_snapshot(state)
            logger.info("Day complete. Waiting for market close…")
            time.sleep(900)   # sleep 15 min then exit
            break

        # Fix 3: parallel signal scanning — 8 workers, preserves architecture
        actions = 0
        prices = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(_process_symbol, sym, ensemble, portfolio_value): sym
                       for sym in symbols}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    result = fut.result(timeout=30)
                    if result:
                        actions += 1
                except Exception as e:
                    logger.error("%s: %s", sym, e)

        # Portfolio snapshot every tick
        state = snapshot_portfolio(prices)
        print(f"  [{now.strftime('%H:%M')}] "
              f"Cash=₹{state['cash']:,.0f}  "
              f"OpenEq=₹{state['open_equity']:,.0f}  "
              f"Total=₹{state['total_value']:,.0f}  "
              f"DD={state['drawdown_pct']:.1%}  "
              f"Actions={actions}")

        # Sleep until next 5-min bar
        wait = _seconds_to_next_bar()
        logger.debug("Sleeping %.0fs until next bar", wait)
        time.sleep(max(wait, 10))
