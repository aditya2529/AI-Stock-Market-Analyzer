"""P42 CLI — user-facing entry points for ``python main.py live ...``.

Three subcommands:
  demo     — full safety flow + CONFIRM gate + place + record
  close    — opposite-MARKET square-off + CONFIRM gate
  status   — READ-ONLY snapshot (kill-switch + open + last 5 events).
             NO API calls, NO state change.

Each subcommand is a function that takes the parsed argparse Namespace
and either returns cleanly (success) or calls ``sys.exit(1)`` after
printing the error reason. The CONFIRM gate is owned by this module
(not order_manager) so the business-logic surface stays UI-free + testable.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from live_trading import audit_log, kill_switch, live_portfolio, order_manager
from live_trading.upstox_client import UpstoxError


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"


# ── Pretty-print helpers ───────────────────────────────────────────────────


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if sys.stdout.isatty() else s


def _print_preview(prepared: dict) -> None:
    profile = prepared["profile"]
    env = prepared["upstox_env"]
    print()
    print("=" * 60)
    print(_bold(f"  LIVE DEMO ORDER PREVIEW  [{env.upper()}]"))
    print("=" * 60)
    print(f"  identity:   {profile.get('user_name')} ({profile.get('user_id')})")
    if profile.get("email"):
        print(f"  email:      {profile['email']}")
    print(f"  symbol:     {prepared['symbol']}")
    print(f"              -> {prepared['instrument_key']}")
    print(f"  side:       BUY")
    print(f"  qty:        {prepared['qty']}")
    print(f"  type:       {prepared['order_type']}", end="")
    if prepared["order_type"] == "LIMIT":
        print(f" @ ₹{prepared['limit_price']:.2f}")
    else:
        print()
    print(f"  LTP:        ₹{prepared['ltp']:.2f}")
    print(f"  est. cost:  ₹{prepared['estimated_notional']:.2f} "
          f"(cap ₹{kill_switch.MAX_LIVE_NOTIONAL})")
    print("=" * 60)


def _print_result(result: dict) -> None:
    env = result["env"]
    print()
    print("=" * 60)
    if result["fill_status"] == "complete":
        if result.get("notional_warning"):
            print(_bold(f"  ⚠ ORDER FILLED — POST-FILL CAP BREACH  [{env.upper()}]"))
        else:
            print(_bold(f"  ✓ ORDER FILLED  [{env.upper()}]"))
    else:
        print(_bold(f"  ⏳ ORDER PLACED — FILL PENDING  [{env.upper()}]"))
    print("=" * 60)
    print(f"  order_id:    {result['order_id']}")
    print(f"  symbol:      {result['symbol']}  qty={result['qty']}")
    if result.get("fill_price") is not None:
        print(f"  fill_price:  ₹{result['fill_price']:.2f}")
    else:
        print(f"  fill_price:  pending — check Upstox app")
    if result.get("notional_warning"):
        print()
        print(_bold("  ⚠ " + result["notional_warning"]))
    print("=" * 60)


def _confirm(prompt: str) -> bool:
    """Block on input. Only the exact string 'CONFIRM' proceeds. Anything
    else (including Ctrl+D / EOF) aborts. Ctrl+C raises KeyboardInterrupt
    which bubbles up — argparse will print a generic abort and exit."""
    try:
        choice = input(prompt).strip()
    except EOFError:
        return False
    return choice == "CONFIRM"


# ── Subcommand handlers ────────────────────────────────────────────────────


def demo(args) -> None:
    """``python main.py live demo --symbol X --qty N [--limit-price P]``"""
    order_type = "LIMIT" if args.limit_price is not None else "MARKET"
    try:
        prepared = order_manager.prepare_demo_order(
            symbol=args.symbol,
            qty=args.qty,
            order_type=order_type,
            limit_price=args.limit_price,
        )
    except (RuntimeError, KeyError) as exc:
        print(f"\n✗ pre-flight check failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except UpstoxError as exc:
        print(f"\n✗ Upstox API failure during pre-flight: {exc}",
              file=sys.stderr)
        sys.exit(1)

    _print_preview(prepared)

    if not _confirm("\nType CONFIRM to place order (anything else aborts): "):
        print("aborted (no CONFIRM).")
        sys.exit(0)

    confirmed_at = datetime.now(timezone.utc)
    try:
        result = order_manager.execute_demo_order(prepared, confirmed_at)
    except UpstoxError as exc:
        print(f"\n✗ order placement failed: {exc}", file=sys.stderr)
        sys.exit(1)

    _print_result(result)


def close(args) -> None:
    """``python main.py live close --symbol X`` — opposite-MARKET square-off."""
    try:
        kill_switch.check()
    except RuntimeError as exc:
        print(f"\n✗ kill switch: {exc}", file=sys.stderr)
        sys.exit(1)

    live_portfolio.init_live_tables()
    opens = live_portfolio.get_open_positions()
    row = next((r for r in opens if r["symbol"] == args.symbol), None)
    if row is None:
        if not opens:
            print(f"\n✗ no open live position to close.", file=sys.stderr)
        else:
            print(f"\n✗ no open position for {args.symbol!r} "
                  f"(currently open: {[r['symbol'] for r in opens]})",
                  file=sys.stderr)
        sys.exit(1)

    print()
    print("=" * 60)
    print(_bold(f"  LIVE CLOSE PREVIEW  [{row['upstox_env'].upper()}]"))
    print("=" * 60)
    print(f"  symbol:      {row['symbol']}")
    print(f"  original:    {row['side']} qty={row['qty']} "
          f"entry={row.get('entry_price')}")
    fill = row.get("upstox_fill_price")
    print(f"  fill_price:  {fill if fill is not None else 'pending'}")
    print(f"  opened_at:   {row.get('opened_at')}")
    print(f"  -> will fire opposite-MARKET to square-off")
    print("=" * 60)

    if not _confirm("\nType CONFIRM to square-off (anything else aborts): "):
        print("aborted (no CONFIRM).")
        sys.exit(0)

    try:
        result = order_manager.close_position(args.symbol)
    except (RuntimeError, UpstoxError) as exc:
        print(f"\n✗ close failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print()
    print("=" * 60)
    if result["fill_status"] == "complete":
        print(_bold(f"  ✓ POSITION CLOSED  [{result['env'].upper()}]"))
    else:
        print(_bold(f"  ⏳ CLOSE PLACED — FILL PENDING  [{result['env'].upper()}]"))
    print("=" * 60)
    print(f"  close_order_id:    {result['close_order_id']}")
    print(f"  original_order_id: {result['original_order_id']}")
    print(f"  symbol:            {result['symbol']}  qty={result['qty']}")
    if result.get("exit_price") is not None:
        print(f"  exit_price:        ₹{result['exit_price']:.2f}")
    else:
        print(f"  exit_price:        pending — check Upstox app")
    print("=" * 60)


def status(args) -> None:
    """``python main.py live status`` — READ-ONLY snapshot.

    No API calls. No state change. Reads ``.env`` (kill-switch state),
    ``live_trades.db`` (open positions), ``logs/live_trading.log``
    (last 5 events), and prints to stdout. Safe to run any time including
    when LIVE_TRADING is off.
    """
    env_vals = dotenv_values(str(_DEFAULT_ENV_PATH))
    live_flag = env_vals.get("LIVE_TRADING", "<missing>")
    upstox_env = env_vals.get("UPSTOX_ENV", "<missing>")

    print()
    print("=" * 60)
    print(_bold("  LIVE TRADING STATUS (read-only)"))
    print("=" * 60)
    print(f"  LIVE_TRADING:  {live_flag!r}")
    print(f"  UPSTOX_ENV:    {upstox_env!r}")

    try:
        kill_switch.check()
        kill_state = "✓ enabled + all active-env creds present"
    except RuntimeError as exc:
        kill_state = f"✗ {exc}"
    print(f"  kill switch:   {kill_state}")

    live_portfolio.init_live_tables()
    opens = live_portfolio.get_open_positions()
    print()
    print(f"  open positions: {len(opens)} / cap "
          f"{kill_switch.MAX_LIVE_POSITIONS}")
    for r in opens:
        fill = r.get("upstox_fill_price")
        print(f"    {r['symbol']:14s} {r['side']:4s} qty={r['qty']:<3d} "
              f"entry=₹{r.get('entry_price', 0):>8.2f}  "
              f"fill={'₹' + format(fill, '.2f') if fill is not None else 'pending':>10s}  "
              f"order={r['upstox_order_id']}")

    recent = audit_log.read_recent(5)
    print()
    print(f"  last {len(recent)} audit events ({audit_log.LOG_PATH.name}):")
    for e in recent:
        ts = e.get("ts", "")
        action = e.get("action", "?")
        sym = e.get("symbol", "-")
        env_t = e.get("env", "-")
        order = e.get("order_id", "")
        print(f"    {ts}  {action:<14s} {sym:<14s} env={env_t:<7s} {order}")
    print("=" * 60)
