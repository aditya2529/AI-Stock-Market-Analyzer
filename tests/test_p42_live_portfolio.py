"""P42 — live_portfolio: SQLite wrapper for live_trades.db.

RED at commit #1, GREEN at commit #2.

Contracts:
  - init_live_tables(db_path) is idempotent — call twice, no error
  - Schema includes the P42-specific columns: upstox_order_id,
    upstox_fill_price, confirmed_by_user_at, upstox_env
  - Schema includes the P35-parity columns: confidence, regime (nullable)
  - record_order writes a row with status='OPEN'
  - get_open_positions returns OPEN rows only, never CLOSED
  - mark_closed flips status and stores exit price + timestamp
  - UNIQUE constraint on (upstox_order_id, upstox_env) prevents
    sandbox + prod collisions
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

# RED at commit #1 — see test_p42_kill_switch.py for the importorskip pattern.
live_portfolio = pytest.importorskip(
    "live_trading.live_portfolio",
    reason="P42 commit #2 lands the impl",
)


# ── Fixture ────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    """Per-test isolated live_trades.db."""
    p = tmp_path / "live_trades.db"
    live_portfolio.init_live_tables(db_path=str(p))
    return str(p)


def _open_row(symbol="RELIANCE.NS", order_id="ORDER-001",
               qty=1, price=1500.0, env="sandbox", confidence=None, regime=None):
    """Helper to build a representative OPEN-trade record dict."""
    return {
        "symbol": symbol,
        "side": "BUY",
        "qty": qty,
        "entry_price": price,
        "upstox_order_id": order_id,
        "upstox_fill_price": price,
        "upstox_env": env,
        "confirmed_by_user_at": datetime.utcnow().isoformat(),
        "confidence": confidence,
        "regime": regime,
        "status": "OPEN",
    }


# ── Schema + idempotency ────────────────────────────────────────────────────

def test_init_creates_live_trades_table(tmp_path):
    p = str(tmp_path / "fresh.db")
    live_portfolio.init_live_tables(db_path=p)
    with sqlite3.connect(p) as conn:
        cur = conn.execute("SELECT name FROM sqlite_master "
                            "WHERE type='table' AND name='live_trades'")
        assert cur.fetchone() is not None


def test_init_is_idempotent(tmp_path):
    p = str(tmp_path / "x.db")
    live_portfolio.init_live_tables(db_path=p)
    live_portfolio.init_live_tables(db_path=p)  # second call must not raise


def test_schema_includes_p42_columns(db_path):
    expected = {"upstox_order_id", "upstox_fill_price",
                 "confirmed_by_user_at", "upstox_env"}
    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(live_trades)")}
    missing = expected - cols
    assert not missing, f"P42 columns missing: {missing}"


def test_schema_includes_p35_parity_columns(db_path):
    """confidence + regime must exist (nullable) for parity with paper_trades."""
    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(live_trades)")}
    assert "confidence" in cols
    assert "regime" in cols


def test_unique_constraint_on_order_id_env(db_path):
    """Same upstox_order_id is allowed across env (sandbox ORDER-001 vs prod
    ORDER-001 are different orders); within a single env, duplicates raise."""
    live_portfolio.record_order(_open_row(order_id="ORDER-Z", env="sandbox"),
                                  db_path=db_path)
    live_portfolio.mark_closed(order_id="ORDER-Z", env="sandbox",
                                exit_price=1510.0, db_path=db_path)

    # Same order id, different env — OK
    live_portfolio.record_order(_open_row(order_id="ORDER-Z", env="prod"),
                                  db_path=db_path)

    # Same order id, same env — must raise
    with pytest.raises(sqlite3.IntegrityError):
        live_portfolio.record_order(_open_row(order_id="ORDER-Z", env="prod"),
                                      db_path=db_path)


# ── Row write + read ───────────────────────────────────────────────────────

def test_record_order_writes_open_row(db_path):
    live_portfolio.record_order(_open_row(order_id="ORDER-A"), db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("SELECT status, upstox_order_id FROM live_trades "
                            "WHERE upstox_order_id='ORDER-A'")
        row = cur.fetchone()
    assert row == ("OPEN", "ORDER-A")


def test_get_open_positions_returns_open_only(db_path):
    live_portfolio.record_order(_open_row(order_id="O-1"), db_path=db_path)
    live_portfolio.record_order(_open_row(order_id="O-2"), db_path=db_path)
    live_portfolio.mark_closed(order_id="O-1", env="sandbox",
                                exit_price=1510.0, db_path=db_path)

    open_rows = live_portfolio.get_open_positions(db_path=db_path)
    open_ids = {r["upstox_order_id"] for r in open_rows}
    assert open_ids == {"O-2"}


def test_mark_closed_sets_exit_fields(db_path):
    live_portfolio.record_order(_open_row(order_id="O-C"), db_path=db_path)
    live_portfolio.mark_closed(order_id="O-C", env="sandbox",
                                exit_price=1525.0, db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT status, exit_price FROM live_trades WHERE upstox_order_id='O-C'"
        )
        row = cur.fetchone()
    assert row[0] == "CLOSED"
    assert row[1] == 1525.0


# ── Second-position rejection (MAX_LIVE_POSITIONS = 1) ──────────────────────

def test_get_open_positions_count_helper(db_path):
    """Caller (order_manager) reads this count and gates against
    kill_switch.MAX_LIVE_POSITIONS before any API call."""
    assert live_portfolio.count_open_positions(db_path=db_path) == 0
    live_portfolio.record_order(_open_row(order_id="O-1"), db_path=db_path)
    assert live_portfolio.count_open_positions(db_path=db_path) == 1
    live_portfolio.record_order(_open_row(order_id="O-2"), db_path=db_path)
    assert live_portfolio.count_open_positions(db_path=db_path) == 2  # raw count
    # Note: the SLOT-CAP enforcement is in kill_switch.validate_position_slot,
    # NOT in live_portfolio.record_order — the db layer just records facts.
