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

# P30: persist the cooldown to paper_config keyed by date — mirrors P20's
# forced_closed_<date> pattern. Before this, every engine restart
# (watchdog-triggered or otherwise) wiped the in-memory set, so a
# crash-loop scenario re-entered every recently-stopped symbol within
# the same trading day. May 18: AMBUJACEM.NS hit SL, crash, restart,
# fresh cooldown, AMBUJACEM re-opened, hit SL again — two losses on
# the same symbol totalling -₹1,427 in ~10 min.
_SL_COOLDOWN_KEY_FMT = "sl_cooldown_{date}"

# T2.5W1-A — same-day target cooldown. Mirrors the P30 SL pattern so a
# symbol that hit its target earlier in the session is blocked from
# being re-bought the same day. Observed pattern from the lifetime
# 66-trade ledger: model re-entered target-hit symbols intraday and
# lost on the second try. Persisted to paper_config keyed by today's
# local date so a watchdog-triggered restart inherits the block.
_target_cooldown: set = set()
_TARGET_COOLDOWN_KEY_FMT = "target_cooldown_{date}"

# T2.5W1-B — hard 14:00 IST entry cutoff. No new BUY signals after
# 14:00 IST; SELL / exit logic untouched; force-close at 15:15 IST
# untouched. Hard-coded as a fixed experimental constant — the brief
# explicitly forbids making this a runtime config because the clean
# measurement window matters.
TIME_CUTOFF_HOUR = 14


def _load_sl_cooldown_for_today() -> set:
    """Load today's SL cooldown from paper_config — survives restarts."""
    from paper_trading.portfolio import get_config
    key = _SL_COOLDOWN_KEY_FMT.format(date=date.today().isoformat())
    raw = get_config(key, "")
    return set(s for s in (raw or "").split(",") if s)


def _add_to_sl_cooldown(symbol: str) -> None:
    """Add a symbol to the cooldown AND persist to paper_config.

    Updates both the in-memory set (for fast lookup in the hot path)
    and the persisted key (so a restart inherits the same-day cooldown
    via _load_sl_cooldown_for_today).
    """
    from paper_trading.portfolio import get_config, set_config
    _sl_cooldown.add(symbol)
    key = _SL_COOLDOWN_KEY_FMT.format(date=date.today().isoformat())
    existing = get_config(key, "") or ""
    symbols = set(s for s in existing.split(",") if s)
    symbols.add(symbol)
    set_config(key, ",".join(sorted(symbols)))


def _load_target_cooldown_for_today() -> set:
    """T2.5W1-A: load today's target cooldown from paper_config —
    survives restarts. Direct mirror of _load_sl_cooldown_for_today.
    """
    from paper_trading.portfolio import get_config
    key = _TARGET_COOLDOWN_KEY_FMT.format(date=date.today().isoformat())
    raw = get_config(key, "")
    return set(s for s in (raw or "").split(",") if s)


def _add_to_target_cooldown(symbol: str) -> None:
    """T2.5W1-A: add a symbol to the target cooldown AND persist.

    Direct mirror of _add_to_sl_cooldown. Updates the in-memory set
    (hot-path lookup) and the persisted key (restart inheritance via
    _load_target_cooldown_for_today).
    """
    from paper_trading.portfolio import get_config, set_config
    _target_cooldown.add(symbol)
    key = _TARGET_COOLDOWN_KEY_FMT.format(date=date.today().isoformat())
    existing = get_config(key, "") or ""
    symbols = set(s for s in existing.split(",") if s)
    symbols.add(symbol)
    set_config(key, ",".join(sorted(symbols)))


def _is_buy_cutoff_active() -> bool:
    """T2.5W1-B: True when current IST hour >= TIME_CUTOFF_HOUR (14).

    Only consulted from the BUY-open branch of _process_symbol — moving
    this guard to any earlier scope would block SELL / exit logic too,
    which the brief explicitly forbids. The structural test in
    tests/test_t25w1_time_cutoff.py defends that placement.
    """
    return datetime.now(IST).hour >= TIME_CUTOFF_HOUR


# P28: daily safety gates — total NSE exposure cap, daily-loss circuit
# breaker, and daily trade-count cap. All three are defense-in-depth
# guards that fire BEFORE the BUY-open branch's existing slot/cooldown
# checks. Documented thresholds (defense vs. opportunity trade-off):
TOTAL_EXPOSURE_CAP = 0.80   # never deploy > 80% of nse_initial_cash
DAILY_LOSS_LIMIT = -0.03    # halt new opens if today's net_pnl < -3%
DAILY_TRADE_CAP = 8         # 5 max-open + a few closes/re-entries


def _p28_daily_gate_block(symbol: str) -> dict | None:
    """Return an action dict if any of the three daily gates trip, else None.

    Reads:
        - paper_config[nse_initial_cash] — the per-session NSE budget
        - paper_positions — current open NSE equity sum
        - paper_trades — today's cumulative net_pnl + trade count

    Uses isolated per-call SQLite connections (P29 pattern). Cheap on
    the hot path: two small SELECT COALESCE(SUM/COUNT) queries against
    indexes-friendly columns.
    """
    from paper_trading.portfolio import get_open_positions, get_config
    import sqlite3
    # Read DB_PATH from data.database at call time so test fixtures that
    # monkeypatch data.database.DB_PATH redirect this gate to the tmp DB
    # (same pattern used by load_ohlcv — see test_p26_force_close.py).
    from data import database as _db_mod

    nse_initial = float(get_config("nse_initial_cash", "500000") or "500000")
    nse_positions = get_open_positions()
    if not nse_positions.empty:
        nse_open_eq = sum(
            float(r["entry_price"]) * int(r["shares"])
            for _, r in nse_positions.iterrows()
            if r["symbol"].endswith(".NS")
        )
    else:
        nse_open_eq = 0.0
    if nse_open_eq > TOTAL_EXPOSURE_CAP * nse_initial:
        logger.info("%s: total exposure %.0f%% > %.0f%% cap — skipping BUY",
                    symbol, (nse_open_eq / nse_initial) * 100,
                    TOTAL_EXPOSURE_CAP * 100)
        return {"_action": "exposure_capped", "symbol": symbol}

    # R17 — compute "today" via Python from datetime.now(IST) instead of
    # SQL date('now','localtime'). Two reasons:
    #   (1) the replay harness patches intraday.engine.datetime via
    #       FakeDatetime — the Python-side date flows through it cleanly,
    #       so cap behavior in replay matches production. The old SQL
    #       date('now','localtime') was SQLite-side and unpatchable,
    #       causing replay to collapse "today" into whatever wall-clock
    #       day the sweep ran on (R16 P0).
    #   (2) regardless of replay, anchoring "today" to IST is what the
    #       trading semantics actually want — even a server that boots
    #       in UTC would now compute the right day for an IST trading
    #       session. Belt-and-braces.
    today_iso = datetime.now(IST).date().isoformat()
    conn = sqlite3.connect(str(_db_mod.DB_PATH), check_same_thread=False)
    try:
        today_pnl = float(conn.execute(
            "SELECT COALESCE(SUM(net_pnl), 0) FROM paper_trades "
            "WHERE date(exit_time) = ?", (today_iso,)
        ).fetchone()[0])
        today_closed_count = int(conn.execute(
            "SELECT COUNT(*) FROM paper_trades "
            "WHERE date(exit_time) = ?", (today_iso,)
        ).fetchone()[0])
    finally:
        conn.close()

    if today_pnl < DAILY_LOSS_LIMIT * nse_initial:
        logger.warning("%s: daily P&L %.2f below %.0f%% — halting new BUYs",
                       symbol, today_pnl, DAILY_LOSS_LIMIT * 100)
        return {"_action": "daily_loss_halt", "symbol": symbol}

    today_count = today_closed_count + len(nse_positions)
    if today_count >= DAILY_TRADE_CAP:
        logger.info("%s: daily trade count %d >= cap %d — skipping BUY",
                    symbol, today_count, DAILY_TRADE_CAP)
        return {"_action": "daily_count_capped", "symbol": symbol}

    return None


def _ist_now() -> datetime:
    return datetime.now(IST)


def _market_open() -> bool:
    now = _ist_now()
    if now.weekday() >= 5:
        return False
    open_  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_ = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_ <= now <= close_


def _is_nse_trading_day_today() -> bool | None:
    """P50: detect NSE holidays by checking NIFTY 50 for today's bar.

    Returns:
        True  — confirmed trading day (today's bar present in yfinance)
        False — confirmed holiday (no today bar after market should be open)
        None  — too early to tell (called before 09:25 IST) OR yfinance error

    Logic: On a normal trading day, NSE publishes the daily ^NSEI bar
    within ~5-10 min of market open (09:15 IST). If at 09:25 IST or
    later no today bar exists, it is almost certainly a holiday.

    Caller must handle the None case (treat as "continue, recheck next
    tick"). Fail-open on errors so a yfinance flake never blocks a real
    trading session.
    """
    now = _ist_now()
    # Need at least 10 min past market open to be sure
    earliest_check_time = now.replace(hour=9, minute=25, second=0, microsecond=0)
    if now < earliest_check_time:
        return None  # Too early — yfinance may not have today's bar yet

    try:
        import yfinance as yf
        df = yf.download(
            "^NSEI", period="5d", interval="1d",
            progress=False, auto_adjust=True,
        )
        if df.empty:
            return None  # Fail-open — can't determine
        today_date = now.date()
        # Walk index for today's date
        for idx in df.index:
            try:
                idx_date = idx.date() if hasattr(idx, "date") else idx
                if idx_date == today_date:
                    return True
            except Exception:
                continue
        return False  # Today not in last 5 NIFTY bars after 09:25 → holiday
    except Exception as e:
        logger.warning("P50: holiday check failed (%s) — assuming trading day", e)
        return None  # Fail-open


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
            # P30: _add_to_sl_cooldown persists to paper_config so a
            # restart in the same session does NOT re-enter this symbol.
            if result.get("exit_reason") == "stop_loss":
                _add_to_sl_cooldown(symbol)
                logger.info("SL cooldown: %s blocked for rest of session", symbol)
            # T2.5W1-A: same-day target cooldown. Mirrors the SL block
            # above — a target hit also makes the symbol ineligible for
            # re-entry the rest of the day to avoid the model's
            # observed pattern of re-buying-and-losing post-target.
            elif result.get("exit_reason") == "target":
                _add_to_target_cooldown(symbol)
                logger.info("target cooldown: %s blocked for rest of session",
                            symbol)
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
        # P28: daily safety gates (exposure / loss / count) — fire BEFORE
        # any existing slot/cooldown/sizing logic so a bad day cannot
        # quietly compound. See _p28_daily_gate_block for details.
        gate = _p28_daily_gate_block(symbol)
        if gate is not None:
            return gate
        # T2.5W1-B: hard 14:00 IST cutoff for NEW BUYs. Cheapest reject
        # — evaluated before any DB / paper_trading touch. SELL/exit
        # logic upstream is unaffected by construction (this guard
        # lives only inside the BUY-open branch).
        if _is_buy_cutoff_active():
            logger.info("%s: 14:00 cutoff active — skipping BUY", symbol)
            return {"_action": "time_cutoff", "symbol": symbol}
        # Fix 2: block re-entry if symbol hit SL earlier today
        if symbol in _sl_cooldown:
            logger.info("%s: SL cooldown active — skipping BUY", symbol)
            return {"_action": "cooldown", "symbol": symbol}
        # T2.5W1-A: block re-entry if symbol hit target earlier today
        if symbol in _target_cooldown:
            logger.info("%s: target cooldown active — skipping BUY", symbol)
            return {"_action": "target_cooldown", "symbol": symbol}
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

    _FETCH_CACHE.clear()   # Q5: fresh fetch cache each session
    init_paper_tables()
    # P30: rehydrate today's SL cooldown from paper_config. A watchdog
    # restart mid-session must NOT reset the cooldown — otherwise any
    # symbol that hit SL before the restart is eligible for re-entry,
    # which is exactly the AMBUJACEM double-stop scenario from May 18.
    _sl_cooldown.clear()
    _sl_cooldown.update(_load_sl_cooldown_for_today())
    if _sl_cooldown:
        logger.info("P30: rehydrated SL cooldown from paper_config — %d symbol(s): %s",
                    len(_sl_cooldown), sorted(_sl_cooldown))
    # T2.5W1-A: same rehydrate semantics for the target cooldown — a
    # restart mid-session must not let a target-hit symbol be re-bought.
    _target_cooldown.clear()
    _target_cooldown.update(_load_target_cooldown_for_today())
    if _target_cooldown:
        logger.info("T2.5W1-A: rehydrated target cooldown from paper_config — "
                    "%d symbol(s): %s",
                    len(_target_cooldown), sorted(_target_cooldown))
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
    # P50: holiday-check state. None until the first conclusive answer.
    # On True we set a flag so we don't recheck every tick. On False we
    # exit cleanly with a Telegram alert.
    holiday_confirmed_trading = False

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

        # P50: check if today is an NSE holiday. Runs at start of each tick
        # until we get a conclusive answer. Returns None pre-09:25 (too early),
        # True when today's NIFTY bar appears (trading day confirmed), or
        # False if NSE clearly didn't trade today (holiday).
        if not holiday_confirmed_trading:
            holiday_result = _is_nse_trading_day_today()
            if holiday_result is False:
                logger.warning(
                    "P50: NSE holiday detected — no today bar in NIFTY 50 after "
                    "09:25 IST. Exiting session cleanly. No trades will fire."
                )
                try:
                    from alerts.telegram_bot import send_message
                    send_message(
                        f"<b>NSE Holiday Detected</b>\n"
                        f"<i>{now.strftime('%a %d %b %H:%M IST')}</i>\n"
                        f"Engine skipping today's session. No trades will fire."
                    )
                except Exception as e:
                    logger.warning("P50 telegram alert failed (non-fatal): %s", e)
                break
            elif holiday_result is True:
                holiday_confirmed_trading = True
                logger.info("P50: NIFTY 50 has today's bar — trading day confirmed.")
            # else None: too early, recheck next tick

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
                       # P28 — three new daily safety gates
                       "exposure_capped": 0, "daily_loss_halt": 0,
                       "daily_count_capped": 0,
                       # T2.5W1 — same-day target cooldown + 14:00 cutoff
                       "target_cooldown": 0, "time_cutoff": 0,
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
            "| cooldown=%d | target_cooldown=%d | max_pos=%d | sell_ignored=%d "
            # T2.5W1-B: surface the 14:00 entry cutoff counter so the
            # ops dashboard log-parser picks it up alongside the rest.
            "| time_cutoff=%d "
            # P28: surface the three daily safety gate counters so a
            # blocked tick is never silent. Zero on a healthy day.
            "| exposure_capped=%d | daily_loss_halt=%d | daily_count_capped=%d "
            "| opened=%d | closed=%d%s",
            tick_counts["processed"], tick_counts["regime_blocked"],
            tick_counts["conf_blocked"], tick_counts["cooldown"],
            tick_counts["target_cooldown"],
            tick_counts["max_pos"], tick_counts["sell_ignored_no_position"],
            tick_counts["time_cutoff"],
            tick_counts["exposure_capped"], tick_counts["daily_loss_halt"],
            tick_counts["daily_count_capped"],
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
