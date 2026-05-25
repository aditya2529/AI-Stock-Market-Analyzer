"""P42 — SQLite wrapper for live_trades.db.

Pure record-keeping layer. The slot-cap enforcement (MAX_LIVE_POSITIONS)
lives in ``kill_switch.validate_position_slot`` so the policy decision is
visible alongside the other hard caps. This module just records facts
the caller asks it to record.

Schema mirrors ``paper_trades`` (P35 fields ``confidence`` + ``regime``
included as nullable for parity) plus the P42-specific columns:
    upstox_order_id        TEXT
    upstox_fill_price      REAL
    confirmed_by_user_at   TEXT
    upstox_env             TEXT   -- "sandbox" or "prod"

A UNIQUE constraint on ``(upstox_order_id, upstox_env)`` prevents
duplicate-order-id collisions WITHIN an env while still allowing the
same order id across env (sandbox ORDER-001 vs prod ORDER-001 are
genuinely different orders from Upstox's POV).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Any


_DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "live_trades.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_trades (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                TEXT NOT NULL,
    side                  TEXT NOT NULL,
    qty                   INTEGER NOT NULL,
    entry_price           REAL NOT NULL,
    exit_price            REAL,
    status                TEXT NOT NULL DEFAULT 'OPEN',
    upstox_order_id       TEXT NOT NULL,
    upstox_fill_price     REAL,
    upstox_env            TEXT NOT NULL,
    confirmed_by_user_at  TEXT,
    confidence            REAL,
    regime                TEXT,
    opened_at             TEXT DEFAULT (datetime('now')),
    closed_at             TEXT,
    UNIQUE(upstox_order_id, upstox_env)
);
"""


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a per-call connection.

    Per-call (not pooled) because (a) the live path fires manually a
    handful of times per session, (b) this matches the P29 pattern in
    paper_trading.portfolio that resolved the SQLite race, and
    (c) ``check_same_thread=False`` keeps it usable from any caller.
    """
    conn = sqlite3.connect(db_path or _DEFAULT_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_live_tables(db_path: str | None = None) -> None:
    """Idempotent — safe to call at every CLI entry point."""
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def record_order(data: dict[str, Any], db_path: str | None = None) -> None:
    """Insert a new live trade row. Status defaults to 'OPEN'.

    ``data`` must include: symbol, side, qty, entry_price, upstox_order_id,
    upstox_env. Optional: upstox_fill_price, confirmed_by_user_at,
    confidence, regime, status (default 'OPEN').

    Raises ``sqlite3.IntegrityError`` if (upstox_order_id, upstox_env)
    duplicates an existing row.
    """
    cols = [
        "symbol", "side", "qty", "entry_price", "status",
        "upstox_order_id", "upstox_fill_price", "upstox_env",
        "confirmed_by_user_at", "confidence", "regime",
    ]
    values = {
        "symbol": data["symbol"],
        "side": data["side"],
        "qty": int(data["qty"]),
        "entry_price": float(data["entry_price"]),
        "status": data.get("status", "OPEN"),
        "upstox_order_id": data["upstox_order_id"],
        "upstox_fill_price": data.get("upstox_fill_price"),
        "upstox_env": data["upstox_env"],
        "confirmed_by_user_at": data.get("confirmed_by_user_at")
            or datetime.utcnow().isoformat(),
        "confidence": data.get("confidence"),
        "regime": data.get("regime"),
    }
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = f"INSERT INTO live_trades ({', '.join(cols)}) VALUES ({placeholders})"
    with _connect(db_path) as conn:
        conn.execute(sql, values)
        conn.commit()


def mark_closed(order_id: str, env: str, exit_price: float,
                 db_path: str | None = None) -> None:
    """Flip the row's status to 'CLOSED' + record exit_price + closed_at."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE live_trades SET status='CLOSED', exit_price=?, "
            "closed_at=datetime('now') "
            "WHERE upstox_order_id=? AND upstox_env=?",
            (float(exit_price), order_id, env),
        )
        conn.commit()


def get_open_positions(db_path: str | None = None) -> list[dict[str, Any]]:
    """Return all rows where status='OPEN' as a list of dicts."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM live_trades WHERE status='OPEN'"
        ).fetchall()
    return [dict(r) for r in rows]


def count_open_positions(db_path: str | None = None) -> int:
    """Count rows with status='OPEN'. Caller passes this to
    ``kill_switch.validate_position_slot`` for the 1-position cap check."""
    with _connect(db_path) as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM live_trades WHERE status='OPEN'"
        ).fetchone()
    return int(n)
