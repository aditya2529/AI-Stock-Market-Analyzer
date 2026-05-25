"""P42 order manager — orchestrates the full safety flow for live demo trades.

Split into ``prepare_*`` (read-only safety checks + preview info) and
``execute_*`` (places, polls, records, alerts) so the CLI can interleave
the ``input("Type CONFIRM")`` gate between them — keeps the UI step out
of the business-logic surface and makes execute_* testable without
mocking ``input``.

Safety flow (matches the brief's "MANDATORY ORDER OF OPERATIONS"):

  prepare_demo_order(...)
    1. kill_switch.check()                  -> raises if LIVE_TRADING off /
                                                 active-env creds missing
    2. symbol_map.lookup(symbol)            -> raises if unknown
    3. upstox_client.get_quote_ltp(key)     -> last traded price
    4. kill_switch.validate_notional(...)   -> raises if qty*ltp > ₹2,000
    5. live_portfolio.count_open_positions  -> raises if any open
    6. upstox_client.get_profile()          -> identity for the CONFIRM preview
    -> returns "prepared" dict for CLI to display + feed back

  CLI shows preview, gets input("Type CONFIRM"), then calls:

  execute_demo_order(prepared, confirmed_at)
    7. audit_log: "place"
    8. upstox_client.place_order(...)       -> raises UpstoxError on broker reject
    9. _poll_fill_status(order_id)          -> 5×1s poll for status="complete"
   10. post-fill notional check             -> Telegram alert if breached
   11. live_portfolio.record_order(...)     -> persists the OPEN row
   12. Telegram alert ([LIVE-{env}] tag, optional [FILL PENDING] / [⚠ CAP BREACH])
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from dotenv import dotenv_values

from live_trading import audit_log, kill_switch, live_portfolio, symbol_map, upstox_client
from live_trading.upstox_client import UpstoxError


_FILL_POLL_ATTEMPTS = 5
_FILL_POLL_INTERVAL_SECONDS = 1.0


# ── Internal helpers ───────────────────────────────────────────────────────


def _active_env(env_path: str | None = None) -> str:
    """Read UPSTOX_ENV. kill_switch.check has already validated it's
    sandbox|prod by the time anyone in this module calls _active_env."""
    p = env_path or upstox_client._DEFAULT_ENV_PATH
    return dotenv_values(str(p)).get("UPSTOX_ENV", "?")


def _telegram(text: str, env: str, tag: str = "") -> None:
    """Best-effort Telegram alert with [LIVE-{env}] env tag.

    Imports ``alerts.telegram_bot`` lazily so that module's load surface is
    never touched at live_trading import time (preserves the "ZERO changes
    to alerts/" boundary even at the import-side-effect level). Never
    raises — Telegram outage must not bring down the order flow.
    """
    try:
        from alerts.telegram_bot import send_message
        prefix_parts = [f"[LIVE-{env.upper()}]"]
        if tag:
            prefix_parts.append(f"[{tag}]")
        prefix = " ".join(prefix_parts)
        send_message(f"{prefix} {text}".strip())
    except Exception:
        pass


def _poll_fill_status(order_id: str,
                       env_path: str | None = None) -> tuple[float | None, str]:
    """5-attempt × 1s foreground poll for status=complete.

    Returns ``(fill_price, status)``:
      - ("complete", float)  — order filled, fill_price is average_fill_price
      - ("pending",  None)   — poll exhausted, operator should check broker
    Raises ``UpstoxError`` if Upstox returns status=rejected|cancelled.
    """
    for attempt in range(_FILL_POLL_ATTEMPTS):
        try:
            details = upstox_client.get_order_details(order_id,
                                                       env_path=env_path)
        except UpstoxError:
            time.sleep(_FILL_POLL_INTERVAL_SECONDS)
            continue

        status = (details.get("status") or "").lower()
        audit_log.write_event(action="poll", order_id=order_id,
                               attempt=attempt + 1, status=status)
        if status == "complete":
            fill_price = details.get("average_fill_price")
            audit_log.write_event(action="fill", order_id=order_id,
                                   fill_price=fill_price, status=status)
            return (float(fill_price) if fill_price is not None else None,
                    "complete")
        if status in ("rejected", "cancelled"):
            audit_log.write_event(action="fill", order_id=order_id,
                                   status=status, fill_price=None)
            raise UpstoxError(f"order {order_id} {status}: {details}")
        time.sleep(_FILL_POLL_INTERVAL_SECONDS)

    audit_log.write_event(action="fill_pending", order_id=order_id,
                           reason="poll exhausted after 5s")
    return (None, "pending")


# ── Public API ─────────────────────────────────────────────────────────────


def prepare_demo_order(symbol: str,
                        qty: int,
                        order_type: str = "MARKET",
                        limit_price: float | None = None,
                        env_path: str | None = None) -> dict:
    """Run all safety checks + collect the preview dict the CLI displays.

    Raises ``RuntimeError`` (from kill_switch, validators) or ``KeyError``
    (from symbol_map) before any state-changing call. ``UpstoxError`` if
    the LTP fetch fails (network / auth).
    """
    env = _active_env(env_path)

    # 1. Kill switch — flag + active-env creds
    kill_switch.check(env_path)

    # 2. Symbol map (yfinance -> Upstox instrument key)
    instrument_key = symbol_map.lookup(symbol)

    # 3. Quote LTP for the cap pre-check
    ltp = upstox_client.get_quote_ltp(instrument_key, env_path=env_path)
    effective_price = (float(limit_price)
                       if order_type == "LIMIT" and limit_price is not None
                       else ltp)

    # 4. Notional cap (qty * effective_price <= 2000)
    kill_switch.validate_notional(qty=qty, price=effective_price)

    # 5. Position-slot cap (0 open)
    live_portfolio.init_live_tables()
    open_count = live_portfolio.count_open_positions()
    kill_switch.validate_position_slot(open_count)

    # 6. Profile (operator's wrong-token safety belt — shown in preview)
    profile = upstox_client.get_profile(env_path=env_path)

    prepared = {
        "symbol": symbol,
        "instrument_key": instrument_key,
        "qty": qty,
        "order_type": order_type,
        "limit_price": float(limit_price) if limit_price is not None else None,
        "ltp": ltp,
        "effective_price": effective_price,
        "estimated_notional": qty * effective_price,
        "upstox_env": env,
        "profile": profile,
    }
    audit_log.write_event(
        action="preview", env=env, symbol=symbol, qty=qty,
        ltp=ltp, order_type=order_type, limit_price=limit_price,
        estimated_notional=prepared["estimated_notional"],
        user_name=profile.get("user_name"), user_id=profile.get("user_id"),
    )
    return prepared


def execute_demo_order(prepared: dict,
                        confirmed_at: datetime | None = None,
                        env_path: str | None = None) -> dict:
    """Place the order, poll for fill, persist, alert. Called by the CLI
    AFTER the operator types CONFIRM. ``confirmed_at`` is the input()
    timestamp; defaults to now() if the caller forgets.

    Returns a result dict with order_id, fill_price, fill_status, and
    any notional_warning string (None if cap was respected post-fill).
    """
    env = prepared["upstox_env"]
    symbol = prepared["symbol"]
    qty = prepared["qty"]
    order_type = prepared["order_type"]
    limit_price = prepared["limit_price"]
    instrument_key = prepared["instrument_key"]
    confirmed_at = confirmed_at or datetime.now(timezone.utc)

    audit_log.write_event(
        action="place", env=env, symbol=symbol, qty=qty,
        order_type=order_type, instrument_key=instrument_key,
        user_confirmed=True,
    )

    try:
        resp = upstox_client.place_order(
            instrument_key=instrument_key,
            qty=qty, side="BUY",
            order_type=order_type,
            limit_price=limit_price,
            env_path=env_path,
        )
    except UpstoxError as exc:
        audit_log.write_event(action="reject", env=env, symbol=symbol,
                               qty=qty, error=str(exc))
        _telegram(f"order REJECTED: {symbol} qty={qty} — {exc}", env=env)
        raise  # Per brief: rejected order does NOT write a row to live_trades.db

    order_id = (resp.get("data") or {}).get("order_id")
    if not order_id:
        audit_log.write_event(action="reject", env=env, symbol=symbol,
                               qty=qty, error=f"no order_id: {resp}")
        _telegram(f"order failed (no order_id): {symbol}", env=env)
        raise UpstoxError(f"no order_id in place_order response: {resp}")

    fill_price, fill_status = _poll_fill_status(order_id, env_path=env_path)

    # Post-fill notional check (brief: alert only, no auto-cancel)
    notional_warning: str | None = None
    if fill_price is not None:
        actual_notional = qty * fill_price
        if actual_notional > kill_switch.MAX_LIVE_NOTIONAL:
            notional_warning = (
                f"POST-FILL notional {actual_notional} > cap "
                f"{kill_switch.MAX_LIVE_NOTIONAL} (qty={qty}, "
                f"fill_price={fill_price}). Order already filled, "
                f"no auto-cancel."
            )

    # Record the trade
    live_portfolio.record_order({
        "symbol": symbol,
        "side": "BUY",
        "qty": qty,
        "entry_price": prepared["effective_price"],
        "upstox_order_id": order_id,
        "upstox_fill_price": fill_price,
        "upstox_env": env,
        "confirmed_by_user_at": confirmed_at.isoformat(),
    })
    audit_log.write_event(action="record", env=env, symbol=symbol,
                           order_id=order_id, fill_price=fill_price,
                           fill_status=fill_status, qty=qty)

    # Telegram alert (env-prefixed, with optional FILL PENDING / CAP BREACH tag)
    if fill_status == "pending":
        _telegram(
            f"order PLACED, fill pending — {symbol} qty={qty} "
            f"order_id={order_id}. CHECK BROKER APP for fill.",
            env=env, tag="FILL PENDING",
        )
    elif notional_warning is not None:
        _telegram(
            f"{notional_warning} symbol={symbol} order_id={order_id}",
            env=env, tag="⚠ CAP BREACH",
        )
    else:
        _telegram(
            f"order FILLED — {symbol} qty={qty} fill_price={fill_price} "
            f"order_id={order_id}",
            env=env,
        )

    return {
        "order_id": order_id,
        "symbol": symbol,
        "qty": qty,
        "fill_price": fill_price,
        "fill_status": fill_status,
        "notional_warning": notional_warning,
        "env": env,
    }


def close_position(symbol: str, env_path: str | None = None) -> dict:
    """Square-off via opposite MARKET order. Same safety flow as demo_order
    (kill switch, audit log, Telegram, fill poll). No CONFIRM gate INSIDE
    this function — the CLI handles that before calling."""
    env = _active_env(env_path)
    kill_switch.check(env_path)

    # Belt-and-braces: prepare_demo_order calls init_live_tables before
    # any db read; close_position must do the same to handle a clean
    # repo state (no prior open + no demo trade yet → table didn't
    # exist before this close attempt).
    live_portfolio.init_live_tables()
    open_rows = live_portfolio.get_open_positions()
    if not open_rows:
        raise RuntimeError("no open live position to close")

    row = next((r for r in open_rows if r["symbol"] == symbol), None)
    if row is None:
        open_syms = [r["symbol"] for r in open_rows]
        raise RuntimeError(f"no open position for {symbol!r} "
                            f"(currently open: {open_syms})")

    opposite_side = "SELL" if row["side"] == "BUY" else "BUY"
    instrument_key = symbol_map.lookup(symbol)

    audit_log.write_event(
        action="close_start", env=env, symbol=symbol,
        original_side=row["side"], opposite_side=opposite_side,
        qty=row["qty"], original_order_id=row["upstox_order_id"],
        user_confirmed=True,
    )

    try:
        resp = upstox_client.place_order(
            instrument_key=instrument_key,
            qty=int(row["qty"]),
            side=opposite_side,
            order_type="MARKET",
            env_path=env_path,
        )
    except UpstoxError as exc:
        audit_log.write_event(action="reject", env=env, symbol=symbol,
                               error=f"close failed: {exc}")
        _telegram(f"close REJECTED: {symbol} — {exc}", env=env)
        raise

    close_order_id = (resp.get("data") or {}).get("order_id")
    if not close_order_id:
        raise UpstoxError(f"no order_id in close response: {resp}")

    fill_price, fill_status = _poll_fill_status(close_order_id,
                                                  env_path=env_path)

    if fill_price is not None:
        live_portfolio.mark_closed(
            order_id=row["upstox_order_id"], env=env, exit_price=fill_price,
        )

    audit_log.write_event(
        action="close_complete", env=env, symbol=symbol,
        close_order_id=close_order_id, fill_price=fill_price,
        fill_status=fill_status, original_order_id=row["upstox_order_id"],
    )

    if fill_status == "pending":
        _telegram(f"close PENDING — {symbol}. CHECK BROKER APP.",
                  env=env, tag="FILL PENDING")
    else:
        _telegram(f"close FILLED — {symbol} qty={row['qty']} "
                   f"exit_price={fill_price}", env=env)

    return {
        "close_order_id": close_order_id,
        "original_order_id": row["upstox_order_id"],
        "symbol": symbol,
        "qty": row["qty"],
        "exit_price": fill_price,
        "fill_status": fill_status,
        "env": env,
    }
