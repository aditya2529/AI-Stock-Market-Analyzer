"""P42 — CLI tests.

The CLI is mostly UI glue around order_manager + live_portfolio. Tests
exercise the interactive paths via builtins.input patching, the abort-
on-non-CONFIRM behavior, and status' read-only guarantee (no API calls).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

cli = pytest.importorskip("live_trading.cli",
                            reason="P42 commit #5 lands the impl")


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    """Patch the cli module's default .env path to a tmp file with
    LIVE_TRADING=true. Also resets the cached profile."""
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
    monkeypatch.setattr(cli, "_DEFAULT_ENV_PATH", env_path)
    from live_trading import kill_switch, upstox_client, live_portfolio, audit_log
    monkeypatch.setattr(kill_switch, "_DEFAULT_ENV_PATH", env_path)
    monkeypatch.setattr(upstox_client, "_DEFAULT_ENV_PATH", env_path)
    upstox_client.reset_profile_cache()
    return env_path


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from live_trading import live_portfolio
    p = str(tmp_path / "live_trades.db")
    monkeypatch.setattr(live_portfolio, "_DEFAULT_DB_PATH", p)
    return p


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    from live_trading import audit_log
    monkeypatch.setattr(audit_log, "LOG_PATH", tmp_path / "live_trading.log")
    return audit_log.LOG_PATH


@pytest.fixture
def stub_upstox(monkeypatch):
    from live_trading import upstox_client
    stubs = {
        "get_quote_ltp": MagicMock(return_value=1500.0),
        "get_profile": MagicMock(return_value={
            "user_id": "U-T", "user_name": "Test User",
            "email": "x@y.z", "products": ["D"],
        }),
        "place_order": MagicMock(return_value={
            "data": {"order_id": "ORD-X"},
        }),
        "get_order_details": MagicMock(return_value={
            "status": "complete", "average_fill_price": 1500.0,
        }),
    }
    for name, m in stubs.items():
        monkeypatch.setattr(upstox_client, name, m)
    return stubs


# ── demo ───────────────────────────────────────────────────────────────────


def test_demo_happy_path(live_env, stub_upstox, isolated_db,
                          isolated_log, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt="": "CONFIRM")
    args = SimpleNamespace(symbol="RELIANCE.NS", qty=1, limit_price=None)
    with patch("live_trading.cli.order_manager._telegram"):
        cli.demo(args)
    out = capsys.readouterr().out
    assert "LIVE DEMO ORDER PREVIEW" in out
    assert "Test User" in out
    assert "RELIANCE.NS" in out
    assert "ORDER FILLED" in out
    assert "ORD-X" in out


def test_demo_aborts_when_not_confirm(live_env, stub_upstox, isolated_db,
                                         isolated_log, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")
    args = SimpleNamespace(symbol="RELIANCE.NS", qty=1, limit_price=None)
    with pytest.raises(SystemExit) as e:
        cli.demo(args)
    assert e.value.code == 0
    assert "aborted" in capsys.readouterr().out.lower()
    # Critically: place_order must NOT have been called
    assert stub_upstox["place_order"].call_count == 0


def test_demo_aborts_on_eof(live_env, stub_upstox, isolated_db,
                              isolated_log, monkeypatch, capsys):
    def _eof(*_a, **_k):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    args = SimpleNamespace(symbol="RELIANCE.NS", qty=1, limit_price=None)
    with pytest.raises(SystemExit) as e:
        cli.demo(args)
    assert e.value.code == 0
    assert stub_upstox["place_order"].call_count == 0


def test_demo_exits_1_on_kill_switch(tmp_path, monkeypatch, capsys,
                                       stub_upstox, isolated_db, isolated_log):
    env_path = tmp_path / ".env"
    env_path.write_text(
        '\n'.join([
            'LIVE_TRADING="false"',
            'UPSTOX_ENV="sandbox"',
            'UPSTOX_SANDBOX_API_KEY="k"',
            'UPSTOX_SANDBOX_API_SECRET="s"',
            'UPSTOX_SANDBOX_ACCESS_TOKEN="t"',
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_DEFAULT_ENV_PATH", env_path)
    from live_trading import kill_switch, upstox_client
    monkeypatch.setattr(kill_switch, "_DEFAULT_ENV_PATH", env_path)
    monkeypatch.setattr(upstox_client, "_DEFAULT_ENV_PATH", env_path)

    args = SimpleNamespace(symbol="RELIANCE.NS", qty=1, limit_price=None)
    with pytest.raises(SystemExit) as e:
        cli.demo(args)
    assert e.value.code == 1
    assert "LIVE_TRADING" in capsys.readouterr().err


def test_demo_with_limit_price_uses_limit(live_env, stub_upstox, isolated_db,
                                            isolated_log, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt="": "CONFIRM")
    args = SimpleNamespace(symbol="RELIANCE.NS", qty=1, limit_price=1450.0)
    with patch("live_trading.cli.order_manager._telegram"):
        cli.demo(args)
    # Preview should mention LIMIT
    out = capsys.readouterr().out
    assert "LIMIT" in out
    # place_order called with LIMIT
    place_kwargs = stub_upstox["place_order"].call_args.kwargs
    assert place_kwargs["order_type"] == "LIMIT"
    assert place_kwargs["limit_price"] == 1450.0


# ── close ──────────────────────────────────────────────────────────────────


def test_close_aborts_when_not_confirm(live_env, stub_upstox, isolated_db,
                                          isolated_log, monkeypatch, capsys):
    from live_trading import live_portfolio
    live_portfolio.init_live_tables(db_path=isolated_db)
    live_portfolio.record_order({
        "symbol": "RELIANCE.NS", "side": "BUY", "qty": 1, "entry_price": 1500.0,
        "upstox_order_id": "ORD-X", "upstox_env": "sandbox",
        "upstox_fill_price": 1500.0,
    }, db_path=isolated_db)

    monkeypatch.setattr("builtins.input", lambda _p="": "no")
    args = SimpleNamespace(symbol="RELIANCE.NS")
    with pytest.raises(SystemExit) as e:
        cli.close(args)
    assert e.value.code == 0
    # MUST NOT have placed an opposite order
    assert stub_upstox["place_order"].call_count == 0


def test_close_exits_1_when_no_open(live_env, stub_upstox, isolated_db,
                                       isolated_log, monkeypatch, capsys):
    args = SimpleNamespace(symbol="RELIANCE.NS")
    with pytest.raises(SystemExit) as e:
        cli.close(args)
    assert e.value.code == 1


def test_close_happy_path(live_env, stub_upstox, isolated_db,
                            isolated_log, monkeypatch, capsys):
    from live_trading import live_portfolio
    live_portfolio.init_live_tables(db_path=isolated_db)
    live_portfolio.record_order({
        "symbol": "RELIANCE.NS", "side": "BUY", "qty": 1, "entry_price": 1500.0,
        "upstox_order_id": "ORD-X", "upstox_env": "sandbox",
        "upstox_fill_price": 1500.0,
    }, db_path=isolated_db)

    stub_upstox["place_order"].return_value = {
        "data": {"order_id": "CLOSE-X"},
    }
    stub_upstox["get_order_details"].return_value = {
        "status": "complete", "average_fill_price": 1520.0,
    }
    monkeypatch.setattr("builtins.input", lambda _p="": "CONFIRM")
    args = SimpleNamespace(symbol="RELIANCE.NS")
    with patch("live_trading.cli.order_manager._telegram"):
        cli.close(args)

    out = capsys.readouterr().out
    assert "POSITION CLOSED" in out
    assert "CLOSE-X" in out
    assert "1520.00" in out


# ── status ─────────────────────────────────────────────────────────────────


def test_status_makes_no_api_calls(live_env, stub_upstox, isolated_db,
                                      isolated_log, capsys):
    args = SimpleNamespace()
    cli.status(args)
    # The defining read-only property: no HTTP calls
    assert stub_upstox["get_quote_ltp"].call_count == 0
    assert stub_upstox["get_profile"].call_count == 0
    assert stub_upstox["place_order"].call_count == 0
    assert stub_upstox["get_order_details"].call_count == 0


def test_status_prints_expected_sections(live_env, stub_upstox, isolated_db,
                                            isolated_log, capsys):
    args = SimpleNamespace()
    cli.status(args)
    out = capsys.readouterr().out
    assert "LIVE TRADING STATUS" in out
    assert "LIVE_TRADING" in out
    assert "UPSTOX_ENV" in out
    assert "kill switch" in out
    assert "open positions" in out
    assert "audit events" in out


def test_status_reports_kill_switch_disabled_when_off(
        tmp_path, monkeypatch, stub_upstox, isolated_db, isolated_log, capsys):
    env_path = tmp_path / ".env"
    env_path.write_text('LIVE_TRADING="false"\nUPSTOX_ENV="sandbox"\n',
                          encoding="utf-8")
    monkeypatch.setattr(cli, "_DEFAULT_ENV_PATH", env_path)
    from live_trading import kill_switch
    monkeypatch.setattr(kill_switch, "_DEFAULT_ENV_PATH", env_path)

    cli.status(SimpleNamespace())
    out = capsys.readouterr().out
    assert "LIVE_TRADING" in out
    assert "✗" in out  # kill-switch line shows ✗ when disabled


def test_status_shows_open_position(live_env, stub_upstox, isolated_db,
                                       isolated_log, capsys):
    from live_trading import live_portfolio
    live_portfolio.init_live_tables(db_path=isolated_db)
    live_portfolio.record_order({
        "symbol": "RELIANCE.NS", "side": "BUY", "qty": 1, "entry_price": 1500.0,
        "upstox_order_id": "ORD-X", "upstox_env": "sandbox",
        "upstox_fill_price": 1500.0,
    }, db_path=isolated_db)

    cli.status(SimpleNamespace())
    out = capsys.readouterr().out
    assert "RELIANCE.NS" in out
    assert "ORD-X" in out
