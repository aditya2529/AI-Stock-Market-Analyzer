"""NYSE / NASDAQ intraday engine — runs 9:30 AM to 3:50 PM ET every weekday.

Flow each 5-min tick:
    1. Fetch latest 5-min bars for all US symbols via Alpaca (IEX feed)
    2. Check open positions: stop-loss / target hit?
    3. Generate signal via ensemble model
    4. Open new positions (shares INTRADAY_MAX_POSITIONS across all markets)
    5. At 3:50 PM ET: force-close all open US positions
    6. Telegram alert on every trade event

NYSE hours: 9:30 AM – 4:00 PM ET  (= 7:00 PM – 1:30 AM UTC)
                                   (= 8:00 PM – 2:30 AM IST in summer / 9:00 PM – 3:30 AM IST in winter)

Cron (server is UTC): runs from 13:30 UTC (9:30 AM ET) on weekdays.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    INTRADAY_SIGNAL_INTERVAL, INTRADAY_MAX_POSITIONS,
    INTRADAY_BUY_THRESHOLD, INTRADAY_SELL_THRESHOLD,
    DEFAULT_US_SYMBOLS,
)

logger = logging.getLogger(__name__)

# US Eastern Time — daylight-aware using fixed offsets is brittle;
# use zoneinfo which handles DST automatically.
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    import pytz
    ET = pytz.timezone("America/New_York")

# NYSE force-close buffer: 3:50 PM ET (10 min before 4:00 PM close)
_FORCE_CLOSE_HOUR   = 15
_FORCE_CLOSE_MINUTE = 50

# Per-symbol SL cooldown (reset each session)
_sl_cooldown: set = set()


def _et_now() -> datetime:
    return datetime.now(ET)


def _nyse_open() -> bool:
    """True during NYSE regular session (Mon-Fri 9:30-16:00 ET)."""
    now = _et_now()
    if now.weekday() >= 5:       # Saturday / Sunday
        return False
    open_  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_ = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_ <= now <= close_


def _should_force_close() -> bool:
    now = _et_now()
    return now.hour > _FORCE_CLOSE_HOUR or (
        now.hour == _FORCE_CLOSE_HOUR and now.minute >= _FORCE_CLOSE_MINUTE
    )


def _seconds_to_next_bar() -> float:
    """Seconds until the next 5-min bar boundary."""
    now = _et_now()
    minutes_past = now.minute % INTRADAY_SIGNAL_INTERVAL
    seconds_past = minutes_past * 60 + now.second
    return max(0, INTRADAY_SIGNAL_INTERVAL * 60 - seconds_past)


def _fetch_us_intraday(symbol: str) -> pd.DataFrame | None:
    """Fetch latest 5-min bars for a US symbol via yfinance.

    yfinance pulls from Yahoo Finance which is near-real-time (<2 min lag) during
    NYSE hours — far better than Alpaca's free IEX feed (15-min delayed).
    Alpaca is kept only for dashboard price display, not for signal generation.
    """
    try:
        import yfinance as yf
        df = yf.download(symbol, period="5d", interval="5m",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        # Flatten MultiIndex columns (yfinance returns them for single-symbol downloads too)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        # Strip timezone — keep naive datetime (ET already embedded in values)
        df.index = pd.to_datetime(df.index).tz_localize(None) \
            if df.index.tz is None \
            else pd.to_datetime(df.index).tz_convert("America/New_York").tz_localize(None)
        return df
    except Exception as e:
        logger.warning("fetch_us_intraday(%s): %s", symbol, e)
        return None


def _process_us_symbol(symbol: str, ensemble, portfolio_value: float) -> dict | None:
    """Fetch, engineer features, generate signal, execute for one US stock."""
    from features.engineer import engineer_features
    from paper_trading.portfolio import get_position, get_cash
    from paper_trading.executor import try_open, try_close
    from signals.risk import compute_stop_and_target

    df = _fetch_us_intraday(symbol)
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
            if result.get("exit_reason") == "stop_loss":
                _sl_cooldown.add(symbol)
                logger.info("SL cooldown: %s blocked for rest of session", symbol)
            from alerts.dispatcher import on_trade_closed
            on_trade_closed(result)
            return result

    # Generate signal
    try:
        result = ensemble.predict_with_confidence(featured)
        latest     = result.iloc[-1]
        signal     = latest["signal"]
        regime     = latest["regime"]
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
        if symbol in _sl_cooldown:
            logger.debug("SL cooldown active — skipping BUY for %s", symbol)
            return None
        from paper_trading.portfolio import get_open_positions
        if len(get_open_positions()) >= INTRADAY_MAX_POSITIONS:
            logger.debug("%s: max positions reached", symbol)
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


def _force_close_all_us(symbols: list[str]):
    """Close every open US position at market price — called at 3:50 PM ET."""
    from paper_trading.portfolio import get_open_positions
    from paper_trading.executor import try_close
    from alerts.dispatcher import on_trade_closed

    positions = get_open_positions()
    if positions.empty:
        return

    # Only close positions that belong to US symbols in this session
    us_set = set(symbols)
    us_positions = positions[positions["symbol"].isin(us_set)]
    if us_positions.empty:
        return

    logger.info("3:50 PM ET — force closing %d US positions", len(us_positions))
    for _, pos in us_positions.iterrows():
        sym = pos["symbol"]
        df  = _fetch_us_intraday(sym)
        price = (float(df["close"].iloc[-1])
                 if df is not None and not df.empty
                 else float(pos["entry_price"]))
        result = try_close(sym, price, "force_close_eod")
        if result:
            on_trade_closed(result)


def run_us_session(symbols: list[str] | None = None,
                   ensemble=None,
                   portfolio_value: float = 100_000.0):
    """
    Main NYSE intraday loop. Runs every 5 minutes from 9:30 AM to 3:50 PM ET.
    Call this once when the US market opens — it blocks until market close.

    Args:
        symbols:         US stock symbols to scan (default: DEFAULT_US_SYMBOLS)
        ensemble:        Pre-loaded Ensemble model
        portfolio_value: Starting portfolio size in USD
    """
    from paper_trading.portfolio import (
        init_paper_tables, get_config, set_cash, set_config, snapshot_portfolio,
    )
    from alerts.dispatcher import on_portfolio_snapshot
    from models.ensemble import Ensemble

    if symbols is None:
        symbols = DEFAULT_US_SYMBOLS

    if ensemble is None:
        ensemble = Ensemble.load()

    _sl_cooldown.clear()
    init_paper_tables()
    if get_config("cash") is None:
        set_cash(portfolio_value)
        set_config("peak_value", portfolio_value)
        set_config("initial_cash", portfolio_value)

    logger.info("US session started | %d symbols | 5-min bars | NYSE hours", len(symbols))
    print(f"\n  US session running — {len(symbols)} symbols")
    print(f"  Signals every 5 min | Force close at 3:50 PM ET")
    print(f"  Symbols: {', '.join(symbols)}")
    print(f"  Press Ctrl+C to stop early\n")

    forced_closed = False

    while True:
        now = _et_now()

        if not _nyse_open():
            print(f"  [{now.strftime('%H:%M ET')}] NYSE closed. Session ended.")
            break

        # Force close at 3:50 PM ET
        if _should_force_close() and not forced_closed:
            _force_close_all_us(symbols)
            forced_closed = True
            state = snapshot_portfolio({})
            on_portfolio_snapshot(state)
            logger.info("US session complete. Waiting for market close...")
            time.sleep(600)   # sleep 10 min then exit
            break

        # Parallel signal scanning
        actions = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {
                ex.submit(_process_us_symbol, sym, ensemble, portfolio_value): sym
                for sym in symbols
            }
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    result = fut.result(timeout=30)
                    if result:
                        actions += 1
                except Exception as e:
                    logger.error("%s: %s", sym, e)

        # Portfolio snapshot every tick
        state = snapshot_portfolio({})
        print(f"  [{now.strftime('%H:%M ET')}] "
              f"Cash=${state['cash']:,.0f}  "
              f"OpenEq=${state['open_equity']:,.0f}  "
              f"Total=${state['total_value']:,.0f}  "
              f"DD={state['drawdown_pct']:.1%}  "
              f"Actions={actions}")

        wait = _seconds_to_next_bar()
        logger.debug("Sleeping %.0fs until next bar", wait)
        time.sleep(max(wait, 10))
