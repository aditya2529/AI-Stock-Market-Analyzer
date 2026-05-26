"""T2.5W1-A — same-day target cooldown.

Mirrors the P30 SL-cooldown regression test pattern. After a position
closes with ``exit_reason == 'target'`` the symbol must be blocked
from being re-bought for the rest of the same trading day, AND the
cooldown must survive a watchdog-triggered engine restart (persisted
to paper_config keyed by today's local date).

Pre-fix behaviour (problem this test prevents from regressing):
the model re-entered symbols intraday after a target hit and lost on
the second try — observed pattern across the 66-trade lifetime ledger.

Bisect-friendly: this file lands in commit #1 (RED). The
``_add_to_target_cooldown`` / ``_load_target_cooldown_for_today`` /
``_target_cooldown`` symbols don't exist yet — ImportError is the
expected failure mode at commit #1. Commit #2 introduces the impl.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Redirect data.database.DB_PATH to a temp file and init paper tables.

    Mirrors the P30 fixture so the two cooldown suites share isolation
    semantics and never see each other's state.
    """
    db_file = tmp_path / "test_t25w1_target.db"
    import data.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", str(db_file))
    from paper_trading.portfolio import init_paper_tables
    init_paper_tables()
    yield db_file


def test_target_cooldown_rejects_re_entry_same_day_after_target_hit(tmp_db):
    """After ``_add_to_target_cooldown('RELIANCE.NS')`` the symbol must
    be in the in-memory ``_target_cooldown`` set.

    The brief specifies that the engine's BUY-evaluation branch will
    reject any BUY signal whose symbol is in EITHER cooldown set. This
    test only verifies the cooldown set's contents; the BUY-branch
    reject is exercised by the existing engine integration path and
    surfaced via the ``target_cooldown`` tick counter.
    """
    from intraday.engine import (
        _add_to_target_cooldown,
        _target_cooldown,
    )
    _target_cooldown.clear()

    _add_to_target_cooldown("RELIANCE.NS")
    assert "RELIANCE.NS" in _target_cooldown


def test_target_cooldown_survives_restart(tmp_db):
    """Mirror of test_sl_cooldown_survives_restart. After persistence,
    a wipe-then-reload sequence must restore the symbol — the watchdog
    restart scenario that gave us P30 must not recur for target hits."""
    from intraday.engine import (
        _add_to_target_cooldown,
        _load_target_cooldown_for_today,
        _target_cooldown,
    )
    _target_cooldown.clear()

    _add_to_target_cooldown("RELIANCE.NS")
    assert "RELIANCE.NS" in _target_cooldown

    _target_cooldown.clear()
    assert "RELIANCE.NS" not in _target_cooldown

    reloaded = _load_target_cooldown_for_today()
    assert "RELIANCE.NS" in reloaded, (
        "target cooldown failed to persist across simulated restart"
    )


def test_target_cooldown_persists_multiple_symbols(tmp_db):
    """Adding several symbols accumulates in paper_config — does not
    overwrite. Mirrors the SL-cooldown multi-symbol test."""
    from intraday.engine import (
        _add_to_target_cooldown,
        _load_target_cooldown_for_today,
        _target_cooldown,
    )
    _target_cooldown.clear()

    for sym in ["TCS.NS", "INFY.NS", "RELIANCE.NS"]:
        _add_to_target_cooldown(sym)

    _target_cooldown.clear()
    reloaded = _load_target_cooldown_for_today()
    assert reloaded == {"TCS.NS", "INFY.NS", "RELIANCE.NS"}


def test_target_cooldown_empty_when_no_prior_state(tmp_db):
    """Fresh paper_config (no key set) must yield an empty cooldown
    rather than crash or return garbage."""
    from intraday.engine import _load_target_cooldown_for_today, _target_cooldown
    _target_cooldown.clear()
    reloaded = _load_target_cooldown_for_today()
    assert reloaded == set()


def test_target_cooldown_clears_on_new_date(tmp_db, monkeypatch):
    """The cooldown is keyed by today's local date. Asking for
    "tomorrow's" cooldown must return an empty set — yesterday's
    target-hit symbols are eligible again on a new trading day.

    Achieved by patching ``date.today`` to roll forward one day on the
    reload call and asserting empty.
    """
    from intraday.engine import (
        _add_to_target_cooldown,
        _load_target_cooldown_for_today,
        _target_cooldown,
    )
    import intraday.engine as engine_mod
    from datetime import date, timedelta

    _target_cooldown.clear()
    _add_to_target_cooldown("RELIANCE.NS")

    # First reload (today) — symbol is there.
    today_set = _load_target_cooldown_for_today()
    assert "RELIANCE.NS" in today_set

    tomorrow = date.today() + timedelta(days=1)

    class _FakeDate:
        @classmethod
        def today(cls):
            return tomorrow

    monkeypatch.setattr(engine_mod, "date", _FakeDate)
    tomorrow_set = _load_target_cooldown_for_today()
    assert tomorrow_set == set(), (
        "target cooldown leaked across trading days — must be date-keyed"
    )


def test_target_cooldown_key_format_matches_p30_pattern():
    """The constant ``_TARGET_COOLDOWN_KEY_FMT`` must follow the same
    ``<purpose>_<date>`` shape P30 introduced — keeps the paper_config
    namespace consistent so the ops grep ``sl_cooldown_*`` /
    ``target_cooldown_*`` finds both sets uniformly.
    """
    from intraday.engine import _TARGET_COOLDOWN_KEY_FMT
    assert "target_cooldown" in _TARGET_COOLDOWN_KEY_FMT
    assert "{date}" in _TARGET_COOLDOWN_KEY_FMT
