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
import gc
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Heartbeat for watchdog — touched once per tick; cron alerts if stale
HEARTBEAT_FILE = Path("/home/opc/health/intraday.heartbeat")

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
    """Fetch latest 5-min bars for a symbol.

    Uses Ticker.history() — never returns MultiIndex columns unlike yf.download(),
    which avoids the 'DataFrame with multiple columns' RSI bug.
    """
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="5d", interval="5m", auto_adjust=True)
        if df is None or df.empty:
            return None
        # history() returns clean simple columns — just lowercase
        df.columns = [c.lower() for c in df.columns]
        keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        df = df[keep]
        # Strip timezone — convert IST to naive
        if df.index.tz is not None:
            df.index = pd.to_datetime(df.index).tz_convert("Asia/Kolkata").tz_localize(None)
        else:
            df.index = pd.to_datetime(df.index)
        return df
    except Exception as e:
        logger.warning("fetch_intraday(%s): %s", symbol, e)
        return None


def _process_symbol(symbol: str, ensemble, portfolio_value: float) -> dict | None:
    """Fetch, engineer, generate signal, execute. Returns action taken or None."""
    from features.engineer import engineer_features
    from paper_trading.portfolio import get_position, get_cash
    from paper_trading.executor import try_open, try_close
    from signals.risk import compute_stop_and_target, risk_reward_ratio
    from config import INTRADAY_BUY_THRESHOLD, INTRADAY_SELL_THRESHOLD, BROKERAGE_PCT, SLIPPAGE_PCT

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

    # Regime gate — directional: don't fight the trend on intraday 5-min bars
    if regime in ("HIGH_VOL", "UNKNOWN"):
        signal = "HOLD"
    elif regime == "TRENDING_DOWN" and signal == "BUY":
        signal = "HOLD"
    elif regime == "TRENDING_UP" and signal == "SELL":
        signal = "HOLD"

    # Confidence gate — must be confident enough to trade (matches alert threshold)
    import os
    SIGNAL_MIN_CONFIDENCE = float(os.getenv("SIGNAL_MIN_CONFIDENCE", "0.70"))
    if signal in ("BUY", "SELL") and confidence < SIGNAL_MIN_CONFIDENCE:
        logger.debug("%s: conf %.2f below floor %.2f — skipping %s",
                     symbol, confidence, SIGNAL_MIN_CONFIDENCE, signal)
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
        # Compute SL/TP from expected FILL price (after slippage+brokerage),
        # not the signal price. Keeps advertised R:R matching actual R:R —
        # otherwise slippage shrinks reward and widens risk, dropping R:R 2.0 -> ~1.0
        slip_factor = 1.0 + BROKERAGE_PCT + SLIPPAGE_PCT
        expected_fill = round(current_price * slip_factor, 2)
        stop_loss, target = compute_stop_and_target(expected_fill, atr, "BUY")
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
                # Override alert payload with the values actually used for the trade,
                # so Telegram alert matches what the engine really did.
                alert_payload["price"]       = expected_fill
                alert_payload["stop_loss"]   = stop_loss
                alert_payload["target"]      = target
                alert_payload["risk_reward"] = risk_reward_ratio(expected_fill, stop_loss, target)
                alert_payload["shares"]      = opened.get("shares", alert_payload.get("shares", 0))
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

        # Heartbeat — watchdog reads this file's mtime
        try:
            HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
            HEARTBEAT_FILE.touch()
        except Exception:
            pass  # never let heartbeat I/O kill the engine

        # If we started BEFORE market open today (weekday), wait — don't quit.
        # Without this, a timer firing at 9:10 IST exits because market opens at 9:15.
        if now.weekday() < 5:
            open_today = now.replace(hour=9, minute=15, second=0, microsecond=0)
            close_today = now.replace(hour=15, minute=30, second=0, microsecond=0)
            if now < open_today:
                wait = (open_today - now).total_seconds()
                logger.info("Pre-market — sleeping %.0fs until 9:15 IST", wait)
                print(f"  [{now.strftime('%H:%M')}] Pre-market — waiting {wait:.0f}s for 9:15 IST")
                time.sleep(wait + 5)   # 5 sec safety margin
                continue

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

        # Fix 3: parallel signal scanning — 2 workers (low-RAM VPS safe)
        actions = 0
        prices = {}
        with ThreadPoolExecutor(max_workers=2) as ex:
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

        # Fix 4: reclaim DataFrame memory before sleeping — prevents OOM on small VPS
        gc.collect()

        # Portfolio snapshot every tick — keep DB-side combined log for the API,
        # but PRINT only the NSE-only numbers (this engine never trades NYSE).
        state = snapshot_portfolio(prices)
        try:
            from paper_trading.portfolio import get_market_cash, get_open_positions
            nse_cash = float(get_market_cash("nse"))
            nse_init = float(get_config("nse_initial_cash", "100000"))
            nse_pos = get_open_positions()
            if not nse_pos.empty:
                nse_pos = nse_pos[nse_pos["symbol"].str.endswith(".NS")]
                nse_open_eq = float(sum(
                    float(r["entry_price"]) * int(r["shares"])
                    for _, r in nse_pos.iterrows()
                ))
            else:
                nse_open_eq = 0.0
            nse_total = nse_cash + nse_open_eq
            nse_dd = (nse_init - nse_total) / nse_init if nse_init > 0 else 0.0
            print(f"  [{now.strftime('%H:%M')}] [NSE] "
                  f"Cash=₹{nse_cash:,.0f}  "
                  f"OpenEq=₹{nse_open_eq:,.0f}  "
                  f"Total=₹{nse_total:,.0f}  "
                  f"DD={nse_dd:.2%}  "
                  f"Actions={actions}")
        except Exception as e:
            # Never let printing crash the engine — fall back to combined
            logger.debug("NSE-only snapshot print failed: %s", e)
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
