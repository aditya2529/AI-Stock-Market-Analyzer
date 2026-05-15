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
import traceback
from datetime import date, datetime, timedelta
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


# TTL cache for _fetch_intraday — keyed on (symbol, 5-min bucket).
# Repeated calls within the same 5-min bar reuse the same DataFrame; the
# bucket key flips at the next bar boundary, naturally expiring stale data.
# Q5 fix — eliminates redundant yfinance round-trips within a tick.
_FETCH_CACHE: dict = {}


def _fetch_bucket() -> int:
    """Current 5-minute bucket (epoch_minutes // 5). Cache key component."""
    return int(time.time()) // (INTRADAY_SIGNAL_INTERVAL * 60)


def _fetch_intraday(symbol: str) -> pd.DataFrame | None:
    """Fetch latest 5-min bars for a symbol.

    Uses Ticker.history() — never returns MultiIndex columns unlike yf.download(),
    which avoids the 'DataFrame with multiple columns' RSI bug.

    TTL-cached on (symbol, 5-min bucket): within a bar, calls are O(1) dict hits.
    """
    bucket = _fetch_bucket()
    cached = _FETCH_CACHE.get(symbol)
    if cached is not None and cached[0] == bucket:
        return cached[1]

    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="5d", interval="5m", auto_adjust=True)
        if df is None or df.empty:
            _FETCH_CACHE[symbol] = (bucket, None)
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
        _FETCH_CACHE[symbol] = (bucket, df)
        return df
    except Exception as e:
        logger.warning("fetch_intraday(%s): %s", symbol, e)
        return None


def _process_symbol(symbol: str, ensemble, portfolio_value: float) -> dict | None:
    """Fetch, engineer, generate signal, execute. Returns action taken or None."""
    from features.engineer import engineer_features
    from paper_trading.portfolio import get_position, get_market_cash, _market_of
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
            result["_action"] = "closed"
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
    regime_blocked_this_signal = False
    if regime in ("HIGH_VOL", "UNKNOWN"):
        if signal in ("BUY", "SELL"):
            regime_blocked_this_signal = True
        signal = "HOLD"
    elif regime == "TRENDING_DOWN" and signal == "BUY":
        regime_blocked_this_signal = True
        signal = "HOLD"
    elif regime == "TRENDING_UP" and signal == "SELL":
        regime_blocked_this_signal = True
        signal = "HOLD"

    # Confidence gate — must be confident enough to trade (matches alert threshold)
    import os
    # P3: default lowered from 0.70 to 0.60. The retrained intraday model
    # emits calibrated probabilities in a narrower band (0.4-0.75) than the
    # daily model (0.5-0.9). At 0.70 floor, 22-24/50 symbols were
    # conf_blocked per tick on May 14 and 0 trades opened until the floor
    # was manually overridden. run-intraday.bat also sets this env var to
    # 0.60 explicitly; this change makes the default match production.
    SIGNAL_MIN_CONFIDENCE = float(os.getenv("SIGNAL_MIN_CONFIDENCE", "0.60"))
    conf_blocked_this_signal = False
    if signal in ("BUY", "SELL") and confidence < SIGNAL_MIN_CONFIDENCE:
        # Q6+observability: promoted from debug to info — this fires often and
        # was previously invisible; the per-tick summary depends on seeing it.
        logger.info("%s: conf %.2f below floor %.2f — skipping %s",
                    symbol, confidence, SIGNAL_MIN_CONFIDENCE, signal)
        conf_blocked_this_signal = True
        signal = "HOLD"

    if regime_blocked_this_signal:
        return {"_action": "regime_blocked", "symbol": symbol}
    if conf_blocked_this_signal:
        return {"_action": "conf_blocked", "symbol": symbol}

    pos = get_position(symbol)

    if signal == "SELL":
        if pos:
            result = try_close(symbol, current_price, "signal")
            if result:
                from alerts.dispatcher import on_trade_closed
                on_trade_closed(result)
                result["_action"] = "closed"
            return result
        # Q6 fix: surface SELL signals that fire on no-position (long-only
        # engine cannot act on these). Was silently dropped before.
        logger.info("%s: SELL signal ignored (long-only engine, no open position)", symbol)
        return {"_action": "sell_ignored_no_position", "symbol": symbol}

    if signal == "BUY" and pos is None:
        # Fix 2: block re-entry if symbol hit SL earlier today
        if symbol in _sl_cooldown:
            logger.info("%s: SL cooldown active — skipping BUY", symbol)
            return {"_action": "cooldown", "symbol": symbol}
        from paper_trading.portfolio import get_open_positions
        if len(get_open_positions()) >= INTRADAY_MAX_POSITIONS:
            logger.info("%s: max positions reached, skipping BUY", symbol)
            return {"_action": "max_pos", "symbol": symbol}

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
        # P1: pass per-market cash (NSE for .NS, NYSE otherwise) instead of
        # combined cash. The 20%-of-portfolio cap inside executor._position_size
        # then applies per-market, not combined — so a single NSE trade can no
        # longer eat 71% of the NSE allocation just because NYSE cash inflates
        # the combined pool.
        opened = try_open(symbol, signal_row, current_price, get_market_cash(_market_of(symbol)))
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
            except Exception as e:
                # P8/P11 fix: was `except Exception: pass`. Silent swallow hid
                # the encoding crash that killed the engine on May 14.
                logger.warning("%s: alert dispatch failed — %s", symbol, e, exc_info=True)
            opened["_action"] = "opened"
        return opened

    return None


def _force_close_all() -> bool:
    """Close every open position at market price — called at 3:15 PM.

    P20: top-level try/except. Returns True on success, False on failure.
    On failure the full traceback is written to a timestamped sidecar
    file in ``logs/force_close_failure_*.log`` and an ERROR is emitted
    via the logger. Callers must NOT mark forced_closed=True unless
    this returned True; otherwise the persisted flag would lock the
    engine out of retrying within the same session.
    """
    try:
        from paper_trading.portfolio import get_open_positions
        from paper_trading.executor import try_close
        from alerts.dispatcher import on_trade_closed

        positions = get_open_positions()
        if positions.empty:
            return True

        logger.info("3:15 PM — force closing %d open positions", len(positions))
        for _, pos in positions.iterrows():
            sym = pos["symbol"]
            df = _fetch_intraday(sym)
            price = float(df["close"].iloc[-1]) if df is not None and not df.empty \
                else float(pos["entry_price"])
            result = try_close(sym, price, "force_close_eod")
            if result:
                on_trade_closed(result)
        return True
    except Exception:
        tb = traceback.format_exc()
        sidecar = (
            Path(__file__).parent.parent / "logs"
            / f"force_close_failure_{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}.log"
        )
        try:
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(tb, encoding="utf-8")
            logger.error("P20: _force_close_all CRASHED — traceback in %s", sidecar)
        except Exception as werr:
            logger.error("P20: _force_close_all CRASHED, sidecar write also failed: %s\n%s",
                         werr, tb)
        return False


def run_intraday_session(symbols: list[str], ensemble, portfolio_value: float = 100_000.0):
    """
    Main intraday loop. Runs every 5 minutes from 9:15 AM to 3:30 PM IST.
    Call this once at 9:15 AM — it blocks until market close.
    """
    from paper_trading.portfolio import (
        init_paper_tables, get_config, set_cash, set_config,
        snapshot_portfolio, get_open_positions,
    )
    from alerts.dispatcher import on_portfolio_snapshot

    _sl_cooldown.clear()   # Fix 2: fresh cooldown each session
    _FETCH_CACHE.clear()   # Q5: fresh fetch cache each session
    init_paper_tables()
    if get_config("cash") is None:
        set_cash(portfolio_value)
        set_config("peak_value", portfolio_value)
        set_config("initial_cash", portfolio_value)

    # P20: startup sanity check — if positions are open from a previous day
    # and market is closed, force-close them and refuse to start a new
    # session. Catches the "engine respawned after force-close window passed"
    # case that left 4 positions stuck open on May 14.
    open_pos = get_open_positions()
    if not open_pos.empty:
        latest_entry_utc = pd.to_datetime(open_pos["entry_time"].max())
        latest_entry_ist = (latest_entry_utc + timedelta(hours=5, minutes=30)).date()
        today_ist = _ist_now().date()
        if latest_entry_ist < today_ist and not _market_open():
            logger.error(
                "P20: %d stale positions detected at startup (latest entry %s, "
                "today %s). Market closed — force-closing and exiting.",
                len(open_pos), latest_entry_ist, today_ist,
            )
            ok = _force_close_all()
            if ok:
                logger.error("P20: stale positions cleared. Refusing to start new "
                             "session — re-run after manual review.")
            else:
                logger.error("P20: stale-position cleanup FAILED. See sidecar log. "
                             "DO NOT auto-restart — investigate manually.")
            return

    today_key = f"forced_closed_{date.today().isoformat()}"
    forced_closed = (get_config(today_key, "0") == "1")
    if forced_closed:
        logger.info("P20: force-close already completed for %s — skipping",
                    date.today().isoformat())

    # P22: one-shot Telegram heartbeat at session start. The 09:10 IST
    # auto-boot used to be silent until the first BUY (could be 11:00+),
    # so a successful boot looked identical to a silent crash from the
    # user's phone. Wrapped in try/except so a Telegram outage can never
    # block the engine from starting. Independent of the 30-min engine
    # pulse — this fires exactly once per session.
    try:
        from alerts.telegram_bot import send_message
        from paper_trading.portfolio import get_market_cash
        send_message(
            f"🟢 <b>Engine started</b>\n"
            f"<i>{_ist_now().strftime('%a %d %b %H:%M IST')}</i>\n"
            f"NSE cash: ₹{get_market_cash('nse'):,.0f}\n"
            f"Symbols: {len(symbols)}"
        )
    except Exception as e:
        logger.warning("startup heartbeat failed (non-fatal): %s", e)

    logger.info("Intraday session started | %d symbols | 5-min bars", len(symbols))
    print(f"\n  Intraday session running — {len(symbols)} symbols")
    print(f"  Signals every 5 min | Force close at 3:15 PM IST")
    print(f"  Press Ctrl+C to stop early (open positions will remain open)\n")
    tick_count = 0                # Engine pulse: send Telegram every 6 ticks (30 min)
    PULSE_EVERY_N_TICKS = 6
    new_today = 0                  # counter — incremented on each opened position
    closed_today = 0               # counter — incremented on each close

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
            ok = _force_close_all()
            if ok:
                # P20: persist the flag so a watchdog restart after this point
                # does NOT re-run force-close in the same session.
                set_config(today_key, "1")
                forced_closed = True
                prices = {}
                state = snapshot_portfolio(prices)
                on_portfolio_snapshot(state)
                logger.info("Day complete. Waiting for market close…")
                time.sleep(900)   # sleep 15 min then exit
                break
            else:
                # _force_close_all wrote a sidecar; loop will retry on the next
                # tick. Don't sleep 15min — we want the retry promptly.
                logger.error("P20: force-close failed, will retry on next tick")

        # Q5 fix: 8 workers (was 2). Per-tick yfinance fanout was bottlenecking
        # at ~125-522s when only 2 threads served 50 symbols. yfinance is I/O
        # bound; the extra threads barely move RAM/CPU.
        actions = 0
        prices = {}
        # Observability (Phase 4): per-tick breakdown of why each symbol did or
        # did not trade. Visible at INFO level once per tick.
        tick_counts = {"opened": 0, "closed": 0,
                       "regime_blocked": 0, "conf_blocked": 0,
                       "cooldown": 0, "max_pos": 0,
                       "sell_ignored_no_position": 0,
                       "processed": 0, "errors": 0}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(_process_symbol, sym, ensemble, portfolio_value): sym
                       for sym in symbols}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    result = fut.result(timeout=30)
                    tick_counts["processed"] += 1
                    if result:
                        actions += 1
                        a = result.get("_action") if isinstance(result, dict) else None
                        if a in tick_counts:
                            tick_counts[a] += 1
                except Exception as e:
                    tick_counts["errors"] += 1
                    logger.error("%s: %s", sym, e)

        logger.info(
            "Tick summary: processed=%d | regime_blocked=%d | conf_blocked=%d "
            "| cooldown=%d | max_pos=%d | sell_ignored=%d | opened=%d | closed=%d"
            "%s",
            tick_counts["processed"], tick_counts["regime_blocked"],
            tick_counts["conf_blocked"], tick_counts["cooldown"],
            tick_counts["max_pos"], tick_counts["sell_ignored_no_position"],
            tick_counts["opened"], tick_counts["closed"],
            (f" | errors={tick_counts['errors']}" if tick_counts["errors"] else ""),
        )

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
            # P11/P9: the fallback print itself uses ₹ and can raise
            # UnicodeEncodeError on cp1252 consoles. Wrap it so a print
            # failure logs but cannot kill the main loop.
            try:
                print(f"  [{now.strftime('%H:%M')}] "
                      f"Cash=₹{state['cash']:,.0f}  "
                      f"OpenEq=₹{state['open_equity']:,.0f}  "
                      f"Total=₹{state['total_value']:,.0f}  "
                      f"DD={state['drawdown_pct']:.1%}  "
                      f"Actions={actions}")
            except Exception as pe:
                logger.warning("fallback portfolio print failed: %s", pe)

        # Per-day cumulative counters for the engine pulse
        new_today    += tick_counts["opened"]
        closed_today += tick_counts["closed"]
        tick_count   += 1

        # Engine pulse — Telegram heartbeat every PULSE_EVERY_N_TICKS ticks (~30 min).
        # Lets you check status from phone without opening dashboard.
        if tick_count % PULSE_EVERY_N_TICKS == 0:
            try:
                from alerts.telegram_bot import send_engine_pulse
                from paper_trading.portfolio import get_open_positions as _gop
                # Use NSE-only numbers if the NSE block above succeeded; otherwise fall back
                pulse_cash  = state.get("cash", 0)
                pulse_total = state.get("total_value", 0)
                pulse_dd    = state.get("drawdown_pct", 0)
                try:
                    pulse_cash  = nse_cash       # type: ignore  # noqa
                    pulse_total = nse_total      # type: ignore  # noqa
                    pulse_dd    = nse_dd         # type: ignore  # noqa
                except NameError:
                    pass
                # Optional RAM info — use psutil if available, else skip
                ram_mb = None
                try:
                    import psutil
                    ram_mb = psutil.Process().memory_info().rss / (1024 * 1024)
                except Exception:
                    try:
                        import resource
                        ram_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
                    except Exception:
                        pass
                send_engine_pulse({
                    "now":                now.strftime("%H:%M"),
                    "symbols_processed":  tick_counts["processed"],
                    "open_positions":     len(_gop()),
                    "new_today":          new_today,
                    "closed_today":       closed_today,
                    "cash":               pulse_cash,
                    "total":              pulse_total,
                    "drawdown_pct":       pulse_dd,
                    "last_tick_actions":  actions,
                    "ram_mb":             ram_mb,
                })
            except Exception as e:
                logger.debug("engine pulse send failed: %s", e)

        # Sleep until next 5-min bar
        wait = _seconds_to_next_bar()
        logger.debug("Sleeping %.0fs until next bar", wait)
        time.sleep(max(wait, 10))
