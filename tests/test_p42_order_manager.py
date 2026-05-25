"""P42 — order_manager unit tests.

Mocks the upstream layers (upstox_client, live_portfolio, telegram_bot) so
nothing hits the network or the project's real ``live_trades.db``. Tests
verify the orchestration: that kill_switch fires first, that the post-fill
notional check + Telegram alerts fire on the right paths, that rejections
skip the db row, and that the fill-status poll behaves per spec.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

order_manager = pytest.importorskip(
    "live_trading.order_manager",
    reason="P42 commit #4 lands the impl",
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def live_env(tmp_path):
    """A .env that passes kill_switch.check."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        '\n'.join([
            'LIVE_TRADING="true"',
            'UPSTOX_ENV="sandbox"',
            'UPSTOX_SANDBOX_API_KEY="key"',
            'UPSTOX_SANDBOX_API_SECRET="sec"',
            'UPSTOX_SANDBOX_ACCESS_TOKEN="tok"',
        ]),
        encoding="utf-8",
    )
    return str(env_path)


@pytest.fixture
def disabled_env(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        '\n'.join([
            'LIVE_TRADING="false"',
            'UPSTOX_ENV="sandbox"',
            'UPSTOX_SANDBOX_API_KEY="key"',
            'UPSTOX_SANDBOX_API_SECRET="sec"',
            'UPSTOX_SANDBOX_ACCESS_TOKEN="tok"',
        ]),
        encoding="utf-8",
    )
    return str(env_path)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Per-test live_trades.db at a tmp path. Patches the module's
    _DEFAULT_DB_PATH so record_order / count_open_positions / etc. all
    target the temp file instead of the project's real db."""
    from live_trading import live_portfolio
    db_path = str(tmp_path / "live_trades.db")
    monkeypatch.setattr(live_portfolio, "_DEFAULT_DB_PATH", db_path)
    return db_path


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    """Redirect audit_log.LOG_PATH to a tmp file so tests don't pollute
    the project's real logs/live_trading.log."""
    from live_trading import audit_log
    log_path = tmp_path / "live_trading.log"
    monkeypatch.setattr(audit_log, "LOG_PATH", log_path)
    return log_path


@pytest.fixture
def stub_upstox(monkeypatch):
    """Replace upstox_client functions with controllable mocks.

    Returns a dict of MagicMock objects so a test can assert call_count,
    inspect call_args, or change return_value mid-test.
    """
    from live_trading import upstox_client
    stubs = {
        "get_quote_ltp": MagicMock(return_value=1500.0),
        "get_profile": MagicMock(return_value={
            "user_id": "U-TEST", "user_name": "Test User",
            "email": "x@y.z", "products": ["D"],
        }),
        "place_order": MagicMock(return_value={
            "data": {"order_id": "ORD-X"}, "status": "success",
        }),
        "get_order_details": MagicMock(return_value={
            "order_id": "ORD-X", "status": "complete",
            "average_fill_price": 1500.0, "filled_quantity": 1,
        }),
        "cancel_order": MagicMock(return_value={"status": "success"}),
    }
    for name, m in stubs.items():
        monkeypatch.setattr(upstox_client, name, m)
    return stubs


@pytest.fixture
def stub_telegram(monkeypatch):
    """Patch alerts.telegram_bot.send_message so tests can verify which
    Telegram alerts fire (and what they say) without making network calls."""
    sent_messages: list[str] = []

    def _capture(text, parse_mode="HTML"):
        sent_messages.append(text)
        return True

    # The order_manager imports send_message lazily inside _telegram(); we
    # need to patch it on the module Python will look up at call time.
    monkeypatch.setattr("alerts.telegram_bot.send_message", _capture)
    return sent_messages


# ── prepare_demo_order ──────────────────────────────────────────────────────

def test_prepare_demo_order_returns_full_dict(live_env, stub_upstox,
                                                isolated_db, isolated_log):
    prepared = order_manager.prepare_demo_order(
        symbol="RELIANCE.NS", qty=1, env_path=live_env,
    )
    assert prepared["symbol"] == "RELIANCE.NS"
    assert prepared["instrument_key"] == "NSE_EQ|INE002A01018"
    assert prepared["qty"] == 1
    assert prepared["order_type"] == "MARKET"
    assert prepared["ltp"] == 1500.0
    assert prepared["estimated_notional"] == 1500.0
    assert prepared["upstox_env"] == "sandbox"
    assert prepared["profile"]["user_name"] == "Test User"


def test_prepare_demo_order_kill_switch_fires_before_any_upstream(
        disabled_env, stub_upstox, isolated_db, isolated_log):
    """Disabled flag must raise BEFORE get_quote_ltp / get_profile / etc."""
    with pytest.raises(RuntimeError, match="LIVE_TRADING"):
        order_manager.prepare_demo_order(
            symbol="RELIANCE.NS", qty=1, env_path=disabled_env,
        )
    # None of the upstream Upstox calls should have happened.
    assert stub_upstox["get_quote_ltp"].call_count == 0
    assert stub_upstox["get_profile"].call_count == 0


def test_prepare_demo_order_cap_breach_raises(live_env, stub_upstox,
                                                 isolated_db, isolated_log):
    stub_upstox["get_quote_ltp"].return_value = 2500.0  # 1 share over cap
    with pytest.raises(RuntimeError, match="notional"):
        order_manager.prepare_demo_order(
            symbol="RELIANCE.NS", qty=1, env_path=live_env,
        )


def test_prepare_demo_order_open_position_raises(live_env, stub_upstox,
                                                   isolated_db, isolated_log):
    from live_trading import live_portfolio
    live_portfolio.init_live_tables(db_path=isolated_db)
    live_portfolio.record_order({
        "symbol": "TCS.NS", "side": "BUY", "qty": 1, "entry_price": 3000.0,
        "upstox_order_id": "ORD-Y", "upstox_env": "sandbox",
    }, db_path=isolated_db)
    with pytest.raises(RuntimeError, match="position"):
        order_manager.prepare_demo_order(
            symbol="RELIANCE.NS", qty=1, env_path=live_env,
        )


def test_prepare_demo_order_unknown_symbol_raises(live_env, stub_upstox,
                                                    isolated_db, isolated_log):
    with pytest.raises(KeyError, match="UNKNOWN"):
        order_manager.prepare_demo_order(
            symbol="UNKNOWN.NS", qty=1, env_path=live_env,
        )


def test_prepare_demo_order_limit_uses_limit_price_for_notional(
        live_env, stub_upstox, isolated_db, isolated_log):
    """LIMIT: cap check uses limit_price, not LTP."""
    stub_upstox["get_quote_ltp"].return_value = 2500.0  # would breach
    # ...but LIMIT at 1500 fits under the cap
    prepared = order_manager.prepare_demo_order(
        symbol="RELIANCE.NS", qty=1, order_type="LIMIT", limit_price=1500.0,
        env_path=live_env,
    )
    assert prepared["effective_price"] == 1500.0
    assert prepared["estimated_notional"] == 1500.0


# ── execute_demo_order ──────────────────────────────────────────────────────

def test_execute_demo_order_writes_db_row_on_complete_fill(
        live_env, stub_upstox, isolated_db, isolated_log, stub_telegram):
    prepared = order_manager.prepare_demo_order(
        symbol="RELIANCE.NS", qty=1, env_path=live_env,
    )
    result = order_manager.execute_demo_order(
        prepared, confirmed_at=datetime.now(timezone.utc), env_path=live_env,
    )
    assert result["order_id"] == "ORD-X"
    assert result["fill_status"] == "complete"
    assert result["fill_price"] == 1500.0
    assert result["notional_warning"] is None

    from live_trading import live_portfolio
    opens = live_portfolio.get_open_positions()
    assert len(opens) == 1
    assert opens[0]["upstox_order_id"] == "ORD-X"


def test_execute_demo_order_rejection_skips_db_write(
        live_env, stub_upstox, isolated_db, isolated_log, stub_telegram):
    from live_trading.upstox_client import UpstoxError
    prepared = order_manager.prepare_demo_order(
        symbol="RELIANCE.NS", qty=1, env_path=live_env,
    )
    stub_upstox["place_order"].side_effect = UpstoxError(
        "HTTP 400: insufficient margin"
    )
    with pytest.raises(UpstoxError):
        order_manager.execute_demo_order(prepared, env_path=live_env)

    from live_trading import live_portfolio
    assert live_portfolio.count_open_positions() == 0
    # Telegram fired with the rejection reason
    assert any("REJECTED" in m and "margin" in m for m in stub_telegram)


def test_execute_demo_order_fill_pending_telegram_prefix(
        live_env, stub_upstox, isolated_db, isolated_log, stub_telegram):
    """When poll times out, Telegram message includes [FILL PENDING] tag."""
    stub_upstox["get_order_details"].return_value = {
        "order_id": "ORD-X", "status": "open",  # never flips to complete
    }
    prepared = order_manager.prepare_demo_order(
        symbol="RELIANCE.NS", qty=1, env_path=live_env,
    )
    # Speed up the test: don't actually sleep 5s
    with patch("live_trading.order_manager.time.sleep"):
        result = order_manager.execute_demo_order(prepared, env_path=live_env)
    assert result["fill_status"] == "pending"
    assert result["fill_price"] is None
    assert any("FILL PENDING" in m for m in stub_telegram)


def test_execute_demo_order_post_fill_cap_breach_alerts(
        live_env, stub_upstox, isolated_db, isolated_log, stub_telegram):
    """MARKET slippage: prepared notional OK, actual fill notional > cap."""
    # Prepare under cap (LTP 1500, qty 1, notional 1500)
    prepared = order_manager.prepare_demo_order(
        symbol="RELIANCE.NS", qty=1, env_path=live_env,
    )
    # ...but actual fill came in at 2100 (above ₹2,000 cap)
    stub_upstox["get_order_details"].return_value = {
        "order_id": "ORD-X", "status": "complete",
        "average_fill_price": 2100.0, "filled_quantity": 1,
    }
    result = order_manager.execute_demo_order(prepared, env_path=live_env)
    assert result["notional_warning"] is not None
    assert "2100" in result["notional_warning"] or "2100.0" in result["notional_warning"]
    assert any("CAP BREACH" in m for m in stub_telegram)


def test_execute_demo_order_telegram_includes_env_tag(
        live_env, stub_upstox, isolated_db, isolated_log, stub_telegram):
    prepared = order_manager.prepare_demo_order(
        symbol="RELIANCE.NS", qty=1, env_path=live_env,
    )
    order_manager.execute_demo_order(prepared, env_path=live_env)
    # Happy-path fill alert
    assert any("LIVE-SANDBOX" in m and "FILLED" in m for m in stub_telegram)


# ── close_position ──────────────────────────────────────────────────────────

def test_close_position_places_opposite_market(
        live_env, stub_upstox, isolated_db, isolated_log, stub_telegram):
    # Open a BUY position first
    prepared = order_manager.prepare_demo_order(
        symbol="RELIANCE.NS", qty=1, env_path=live_env,
    )
    order_manager.execute_demo_order(prepared, env_path=live_env)

    # Now close — should fire SELL MARKET
    stub_upstox["place_order"].reset_mock()
    stub_upstox["place_order"].return_value = {
        "data": {"order_id": "CLOSE-X"}, "status": "success",
    }
    stub_upstox["get_order_details"].return_value = {
        "order_id": "CLOSE-X", "status": "complete",
        "average_fill_price": 1510.0,
    }
    result = order_manager.close_position("RELIANCE.NS", env_path=live_env)

    place_call = stub_upstox["place_order"].call_args
    assert place_call.kwargs["side"] == "SELL"
    assert place_call.kwargs["order_type"] == "MARKET"
    assert result["close_order_id"] == "CLOSE-X"
    assert result["exit_price"] == 1510.0


def test_close_position_marks_original_row_closed(
        live_env, stub_upstox, isolated_db, isolated_log, stub_telegram):
    prepared = order_manager.prepare_demo_order(
        symbol="RELIANCE.NS", qty=1, env_path=live_env,
    )
    order_manager.execute_demo_order(prepared, env_path=live_env)

    stub_upstox["place_order"].reset_mock()
    stub_upstox["place_order"].return_value = {
        "data": {"order_id": "CLOSE-X"}
    }
    stub_upstox["get_order_details"].return_value = {
        "status": "complete", "average_fill_price": 1520.0,
    }
    order_manager.close_position("RELIANCE.NS", env_path=live_env)

    from live_trading import live_portfolio
    assert live_portfolio.count_open_positions() == 0
    # The original row should now have exit_price + status=CLOSED
    import sqlite3
    with sqlite3.connect(isolated_db) as conn:
        row = conn.execute(
            "SELECT status, exit_price FROM live_trades "
            "WHERE upstox_order_id='ORD-X'"
        ).fetchone()
    assert row[0] == "CLOSED"
    assert row[1] == 1520.0


def test_close_position_raises_when_no_open(
        live_env, stub_upstox, isolated_db, isolated_log):
    with pytest.raises(RuntimeError, match="no open"):
        order_manager.close_position("RELIANCE.NS", env_path=live_env)


# ── _poll_fill_status (internal) ────────────────────────────────────────────

def test_poll_fill_status_complete_first_try(
        stub_upstox, isolated_log, monkeypatch):
    stub_upstox["get_order_details"].return_value = {
        "status": "complete", "average_fill_price": 1500.0,
    }
    with patch("live_trading.order_manager.time.sleep"):
        fill_price, status = order_manager._poll_fill_status("ORD-X")
    assert fill_price == 1500.0
    assert status == "complete"
    assert stub_upstox["get_order_details"].call_count == 1


def test_poll_fill_status_pending_after_5_attempts(
        stub_upstox, isolated_log, monkeypatch):
    stub_upstox["get_order_details"].return_value = {"status": "open"}
    with patch("live_trading.order_manager.time.sleep"):
        fill_price, status = order_manager._poll_fill_status("ORD-X")
    assert fill_price is None
    assert status == "pending"
    assert stub_upstox["get_order_details"].call_count == 5


def test_poll_fill_status_raises_on_rejected(
        stub_upstox, isolated_log, monkeypatch):
    from live_trading.upstox_client import UpstoxError
    stub_upstox["get_order_details"].return_value = {"status": "rejected"}
    with patch("live_trading.order_manager.time.sleep"):
        with pytest.raises(UpstoxError, match="rejected"):
            order_manager._poll_fill_status("ORD-X")
