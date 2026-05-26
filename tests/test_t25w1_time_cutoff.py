"""T2.5W1-B — hard 14:00 IST entry cutoff.

After 14:00 IST any new BUY signal must be rejected. SELL / exit
logic is untouched. Force-close at 15:15 IST is untouched. The cutoff
is a hard-coded experimental constant — NOT a runtime config — so the
measurement window stays clean.

Empirical justification (recorded for the audit trail): on May 26
four BUY signals fired after 14:00 IST (IOC, ABFRL, CESC, NBCC) and
all four lost a combined -₹2,293. With this overlay the day's loss
would have been -₹221 instead of -₹2,514.

Bisect-friendly: this file lands in commit #1 (RED). The
``TIME_CUTOFF_HOUR`` constant and ``_is_buy_cutoff_active`` helper
don't exist yet — AttributeError / ImportError is the expected RED.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")


def _at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    """Build a tz-aware IST datetime for a hard-coded test instant."""
    return datetime(2026, 5, 27, hour, minute, second, tzinfo=IST)


def test_time_cutoff_constant_is_14():
    """The cutoff hour must be 14. The brief explicitly forbids making
    this a runtime config — it's a fixed experimental constant for
    clean measurement. If a future commit moves this to .env or
    config.py, this test breaks intentionally."""
    from intraday.engine import TIME_CUTOFF_HOUR
    assert TIME_CUTOFF_HOUR == 14


def test_cutoff_active_at_14_00_00_ist():
    """Brief case: BUY must be rejected at exactly 14:00:00 IST."""
    import intraday.engine as engine_mod
    with patch.object(engine_mod, "datetime") as mock_dt:
        mock_dt.now.return_value = _at(14, 0, 0)
        from intraday.engine import _is_buy_cutoff_active
        assert _is_buy_cutoff_active() is True


def test_cutoff_active_at_14_59_59_ist():
    """Brief case: BUY must be rejected at 14:59:59 IST."""
    import intraday.engine as engine_mod
    with patch.object(engine_mod, "datetime") as mock_dt:
        mock_dt.now.return_value = _at(14, 59, 59)
        from intraday.engine import _is_buy_cutoff_active
        assert _is_buy_cutoff_active() is True


def test_cutoff_inactive_at_13_59_59_ist():
    """Brief case: BUY at 13:59:59 must still pass — one second before
    the cutoff is still a valid entry window."""
    import intraday.engine as engine_mod
    with patch.object(engine_mod, "datetime") as mock_dt:
        mock_dt.now.return_value = _at(13, 59, 59)
        from intraday.engine import _is_buy_cutoff_active
        assert _is_buy_cutoff_active() is False


def test_cutoff_active_at_15_14_59_ist_just_before_force_close():
    """Boundary: 15:14:59 IST — engine is still live (force-close
    fires at 15:15) but cutoff stays active because the rule is
    ``hour >= 14``. Defends against a regression where someone scopes
    the cutoff narrowly to 14:00-14:59 only."""
    import intraday.engine as engine_mod
    with patch.object(engine_mod, "datetime") as mock_dt:
        mock_dt.now.return_value = _at(15, 14, 59)
        from intraday.engine import _is_buy_cutoff_active
        assert _is_buy_cutoff_active() is True


def test_cutoff_inactive_at_09_15_ist_session_open():
    """Counter-boundary: market open (09:15 IST) — cutoff must be
    inactive. Sanity check that an off-by-one didn't invert the rule."""
    import intraday.engine as engine_mod
    with patch.object(engine_mod, "datetime") as mock_dt:
        mock_dt.now.return_value = _at(9, 15, 0)
        from intraday.engine import _is_buy_cutoff_active
        assert _is_buy_cutoff_active() is False


def test_cutoff_only_consulted_in_buy_open_branch():
    """Structural assertion — the cutoff must be inside the
    ``signal == "BUY" and pos is None`` block in engine.py.

    If a future refactor moves the guard to the function top, SELL /
    exit logic would be blocked too — exactly the regression the brief
    forbids. This test reads the source and verifies the cutoff guard
    appears strictly AFTER the BUY-open conditional and strictly
    BEFORE the next major branch (the function return).
    """
    from pathlib import Path
    import intraday.engine as eng

    src = Path(eng.__file__).read_text(encoding="utf-8")
    lines = src.splitlines()

    def first_line_containing(needle: str) -> int:
        for i, line in enumerate(lines):
            if needle in line and not line.lstrip().startswith("#"):
                return i
        return -1

    buy_branch_idx = first_line_containing('signal == "BUY" and pos is None')
    cutoff_idx = first_line_containing("_is_buy_cutoff_active")
    return_none_idx = -1
    for i in range(buy_branch_idx + 1, len(lines)):
        if lines[i].strip() == "return None":
            return_none_idx = i
            break

    assert buy_branch_idx >= 0, "could not locate BUY-open branch in engine.py"
    assert cutoff_idx >= 0, "_is_buy_cutoff_active not used in engine.py"
    assert cutoff_idx > buy_branch_idx, (
        "time-cutoff guard lives BEFORE the BUY-open branch — that would "
        "break SELL/exit logic. Move the guard inside the BUY-open block.")
    assert return_none_idx > 0
    assert cutoff_idx < return_none_idx, (
        "time-cutoff guard lives outside the BUY-open branch")


def test_time_cutoff_action_string_is_time_cutoff():
    """The return dict's ``_action`` value must be the string
    ``"time_cutoff"`` so the tick-summary aggregator picks it up
    under the matching counter name."""
    from pathlib import Path
    import intraday.engine as eng
    src = Path(eng.__file__).read_text(encoding="utf-8")
    assert '"time_cutoff"' in src, (
        "expected the return dict to carry _action='time_cutoff'")


def test_tick_summary_counter_includes_time_cutoff_and_target_cooldown():
    """Tick-summary aggregator (the ``tick_counts`` dict initialised
    every tick) must include both new keys so the dashboard log
    parser picks them up.

    Brief: "Add them to the INFO log format string for the tick
    summary line. ops dashboard will pick them up automatically via
    the existing log-parser endpoints."
    """
    from pathlib import Path
    import intraday.engine as eng
    src = Path(eng.__file__).read_text(encoding="utf-8")
    assert '"time_cutoff": 0' in src or "'time_cutoff': 0" in src, (
        "time_cutoff counter not initialised in tick_counts dict")
    assert ('"target_cooldown": 0' in src
            or "'target_cooldown': 0" in src), (
        "target_cooldown counter not initialised in tick_counts dict")
    # The INFO format string must reference both new keys.
    assert "time_cutoff=" in src, "tick-summary log missing time_cutoff="
    assert "target_cooldown=" in src, "tick-summary log missing target_cooldown="
