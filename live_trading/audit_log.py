"""P42 audit log — JSON-lines append-only event log.

One event per line, written to ``logs/live_trading.log``. Greppable +
parseable. Log rotation is an out-of-band concern (ops can ``logrotate``
or move the file at session boundaries — no in-process rotation here).

Canonical fields (every line):
    ts                 ISO 8601 UTC timestamp
    action             "preview" | "place" | "poll" | "fill" |
                        "fill_pending" | "record" | "alert" |
                        "reject" | "close_start" | "close_complete" | "status"
    env                "sandbox" | "prod" (from UPSTOX_ENV at log time)

Common optional fields:
    symbol, qty, price, ltp, fill_price, fill_status,
    order_id, original_order_id, close_order_id,
    user_name, user_id, user_confirmed (bool),
    error (string — only present on action=="reject")

Module-level ``LOG_PATH`` is monkeypatchable by tests; production callers
should not override it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = _PROJECT_ROOT / "logs" / "live_trading.log"


def write_event(**fields: Any) -> None:
    """Append one JSON-lines record. NEVER raises.

    Audit-log failures must not bring down the order flow — if the disk is
    full or the file is locked, we drop the event silently. The operator
    still gets the Telegram alert through a different code path, so loss
    of an audit line is recoverable; raising here would mask the real
    upstream success/failure with a logging error.
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts": datetime.now(timezone.utc).isoformat()}
        event.update(fields)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception:
        pass


def read_recent(n: int = 5) -> list[dict]:
    """Return the last ``n`` events as parsed dicts. Used by ``live status``
    to show recent activity without giving the CLI any state-changing
    permissions."""
    try:
        if not LOG_PATH.exists():
            return []
        with LOG_PATH.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        recent = lines[-n:]
        out = []
        for line in recent:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
    except Exception:
        return []
