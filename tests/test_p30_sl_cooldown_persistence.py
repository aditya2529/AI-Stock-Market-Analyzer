"""Regression test for P30 — SL cooldown must survive engine restart.

Pre-fix bug: ``intraday/engine.py`` declared ``_sl_cooldown = set()``
at module scope. Every restart (watchdog-triggered or otherwise) started
with an empty set. On May 18, AMBUJACEM.NS opened, hit SL, was added to
the cooldown, engine crashed (P29), watchdog restarted, cooldown was
empty, AMBUJACEM re-opened at the next BUY signal, hit SL again — two
identical -₹712 / -₹715 losses inside ~10 min.

Fix: persist the cooldown to ``paper_config`` keyed by the local date
(same pattern as P20's ``forced_closed_<date>``). The session bootstrap
reloads the persisted set into ``_sl_cooldown`` so the in-memory set
matches DB after a restart.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Redirect data.database.DB_PATH to a temp file and init paper tables."""
    db_file = tmp_path / "test_p30.db"
    import data.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", str(db_file))
    from paper_trading.portfolio import init_paper_tables
    init_paper_tables()
    yield db_file


def test_sl_cooldown_survives_restart(tmp_db):
    """Add a symbol to the SL cooldown, simulate restart, reload from DB.

    After ``_add_to_sl_cooldown('RELIANCE.NS')`` the symbol must be
    persisted to paper_config under ``sl_cooldown_<today>``. After we
    clear the in-memory set (simulating a process restart),
    ``_load_sl_cooldown_for_today()`` must return a set containing
    ``RELIANCE.NS``.
    """
    from intraday.engine import (
        _add_to_sl_cooldown, _load_sl_cooldown_for_today, _sl_cooldown,
    )
    _sl_cooldown.clear()  # baseline — start fresh

    _add_to_sl_cooldown("RELIANCE.NS")
    assert "RELIANCE.NS" in _sl_cooldown

    # Simulate engine restart: wipe in-memory state
    _sl_cooldown.clear()
    assert "RELIANCE.NS" not in _sl_cooldown

    # Reload should re-populate from paper_config
    reloaded = _load_sl_cooldown_for_today()
    assert "RELIANCE.NS" in reloaded, (
        "P30 regression: cooldown failed to persist across restart"
    )


def test_sl_cooldown_persists_multiple_symbols(tmp_db):
    """Adding multiple symbols must accumulate in paper_config, not overwrite."""
    from intraday.engine import (
        _add_to_sl_cooldown, _load_sl_cooldown_for_today, _sl_cooldown,
    )
    _sl_cooldown.clear()

    for sym in ["AMBUJACEM.NS", "TCS.NS", "RELIANCE.NS"]:
        _add_to_sl_cooldown(sym)

    _sl_cooldown.clear()
    reloaded = _load_sl_cooldown_for_today()
    assert reloaded == {"AMBUJACEM.NS", "TCS.NS", "RELIANCE.NS"}


def test_sl_cooldown_empty_when_no_prior_state(tmp_db):
    """A fresh paper_config (no key set) must yield an empty cooldown,
    not crash or return garbage."""
    from intraday.engine import _load_sl_cooldown_for_today, _sl_cooldown
    _sl_cooldown.clear()
    reloaded = _load_sl_cooldown_for_today()
    assert reloaded == set()
