"""R17 — replay clock bypass fixes.

R16 surfaced two wall-clock leaks that survive the existing R11 replay
patches:

  A. ``intraday/engine.py`` daily-cap gate uses SQL
     ``date('now','localtime')`` — SQLite-side, can't be Python-patched.
     Fix: rewrite the query to bind today via ?, computed from
     ``datetime.now(IST)`` (which IS routed through the replay's
     FakeDatetime patch).

  B. ``paper_trading/portfolio.py`` trade writer uses
     ``datetime.utcnow().isoformat()`` for entry_time/exit_time.
     The replay never patched ``paper_trading.portfolio.datetime``,
     so every trade got stamped with wall-clock UTC instead of the
     replay clock. All R12/R14/R15/R16 trades landed on the wall-clock
     day the sweep ran, collapsing the entire window into one day.
     Fix: extend ``_FakeDatetime`` with ``utcnow`` + add the
     portfolio patch to the replay's _build_patches list.

These tests lock the fixes in place. They're bisect-friendly: at the
test-first SHA they're RED; the impl SHA flips them GREEN; any future
regression flips them RED.
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── A. SQL date('now', ...) must be gone from cap query ──────────────


def test_engine_cap_query_uses_python_today_not_sql_now():
    """The daily-cap gate's today_closed_count query must compute
    today via Python (which the replay can patch) and bind it as a
    parameter — NOT via SQLite date('now','localtime').
    """
    from intraday import engine as eng
    src = inspect.getsource(eng._p28_daily_gate_block)
    # Strip comments before scanning so reference-to-the-old-bug in a
    # comment doesn't trip the test (legitimate documentation explaining
    # WHY the SQL was rewritten).
    code_only = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert "date('now'" not in code_only, (
        "Engine cap query SQL still references date('now','...') in "
        "executable code — replay clock has no effect on this. Rewrite "
        "to bind today as a parameter."
    )
    assert "WHERE date(exit_time) = ?" in code_only, (
        "Engine cap query should bind today via ? placeholder so the "
        "replay's patched datetime flows through."
    )


def test_engine_cap_query_today_resolves_through_engine_datetime():
    """The today value bound into the SQL query must come from
    ``datetime.now(IST)`` so the replay's FakeDatetime patch flows
    through. (We can't run the full gate without a DB, so source-check.)
    """
    from intraday import engine as eng
    src = inspect.getsource(eng._p28_daily_gate_block)
    # Accept either datetime.now(IST).date().isoformat() or _ist_now().date().isoformat()
    has_dt_now = "datetime.now(IST).date()" in src
    has_ist_now = "_ist_now().date()" in src
    assert has_dt_now or has_ist_now, (
        "Today value bound into the cap query must come from "
        "datetime.now(IST).date() OR _ist_now().date() — both are "
        "patched by the replay harness. Got source that uses neither."
    )


# ── B. FakeDatetime + portfolio patch ────────────────────────────────


def _fake_datetime_cls(replay_clock: pd.Timestamp):
    """Build a fresh FakeDatetime via _build_patches with the given
    replay clock, return the patched class."""
    from models.engine_replay_backtest import _build_patches, ReplayContext
    ctx = ReplayContext()
    ctx.current_clock = replay_clock
    patches = _build_patches(ctx)
    fake = next((new for _mod, name, new in patches if name == "datetime"), None)
    assert fake is not None, "No datetime patch found in _build_patches"
    return fake


def test_fakedatetime_supports_utcnow():
    """``paper_trading.portfolio.open_position`` and ``close_position``
    use ``datetime.utcnow().isoformat()`` to stamp entry_time/exit_time.
    The replay's FakeDatetime must implement utcnow() — returning the
    replay clock converted to UTC."""
    replay_clock = pd.Timestamp("2026-03-15 10:30")  # 10:30 IST
    fake = _fake_datetime_cls(replay_clock)
    assert hasattr(fake, "utcnow"), (
        "FakeDatetime must implement utcnow() so paper_trading writers "
        "stamp trades with replay-clock UTC instead of wall-clock UTC."
    )
    result = fake.utcnow()
    # 10:30 IST = 05:00 UTC same day. Replay clock is tz-naive but the
    # convention used by holdout_start (set in models/replay_*.py) is IST.
    assert result.year == 2026, f"got {result}"
    assert result.month == 3, f"got {result}"
    assert result.day == 15, f"got {result}"
    assert result.hour == 5, f"got {result} — expected 05:00 UTC from 10:30 IST"
    assert result.minute == 0, f"got {result}"


def test_fakedatetime_utcnow_falls_back_when_no_clock():
    """When ctx.current_clock is None (outside replay window or before
    the loop starts) utcnow should return the real wall-clock UTC —
    same fallback contract as the existing now() method."""
    from models.engine_replay_backtest import _build_patches, ReplayContext
    ctx = ReplayContext()  # current_clock = None
    patches = _build_patches(ctx)
    fake = next((new for _mod, name, new in patches if name == "datetime"), None)
    # Should not raise.
    result = fake.utcnow()
    # Some recent year — confirms a real-ish datetime came back.
    assert result.year >= 2025


def test_replay_patches_paper_trading_portfolio_datetime():
    """The patches list returned by _build_patches must include
    ``(paper_trading.portfolio, "datetime", FakeDatetime)`` so the
    trade writer's ``datetime.utcnow()`` flows through the replay
    clock instead of wall-clock UTC."""
    from models.engine_replay_backtest import _build_patches, ReplayContext
    import paper_trading.portfolio as port_mod
    ctx = ReplayContext()
    patches = _build_patches(ctx)
    patched_pairs = [(mod.__name__, name) for mod, name, _ in patches]
    assert ("paper_trading.portfolio", "datetime") in patched_pairs, (
        "_build_patches must patch paper_trading.portfolio.datetime — "
        "without this, every trade's entry_time/exit_time gets stamped "
        "with wall-clock UTC instead of the replay clock, collapsing all "
        "replay trades into one calendar day."
    )


# ── End-to-end smoke ────────────────────────────────────────────────


def test_open_and_close_position_stamp_replay_clock_under_patch(monkeypatch, tmp_path):
    """End-to-end: simulate ``open_position`` then ``close_position``
    with the FakeDatetime patch in place. Verify both entry_time and
    exit_time land on the replay date, not wall-clock today.

    This is the load-bearing regression that proves R16's
    ``all-trades-on-2026-06-02`` collapse cannot recur.
    """
    from models.engine_replay_backtest import _build_patches, ReplayContext
    import paper_trading.portfolio as port_mod
    import data.database as db_mod

    # Replay clock at 10:30 IST on a March day in the holdout window
    replay_clock = pd.Timestamp("2026-03-15 10:30")
    ctx = ReplayContext()
    ctx.current_clock = replay_clock
    patches = _build_patches(ctx)
    fake_dt = next(new for mod, name, new in patches
                   if mod is port_mod and name == "datetime")

    # Sandbox DB
    sandbox = tmp_path / "r17_smoke.db"
    monkeypatch.setattr(db_mod, "DB_PATH", str(sandbox))

    from data.database import init_db
    init_db()
    port_mod.init_paper_tables()
    port_mod.set_market_cash("nse", 500_000.0)

    # Install the patch
    monkeypatch.setattr(port_mod, "datetime", fake_dt)

    port_mod.open_position(
        symbol="RELIANCE.NS",
        entry_price=1000.0,
        shares=10,
        stop_loss=990.0,
        target=1020.0,
        confidence=0.6,
        regime="TRENDING_UP",
    )

    pos = port_mod.get_position("RELIANCE.NS")
    assert pos is not None, "open_position did not write the row"
    assert pos["entry_time"].startswith("2026-03-15"), (
        f"entry_time={pos['entry_time']!r} did not land on replay date "
        f"2026-03-15. The portfolio.datetime patch is not effective."
    )

    # Advance clock and close
    ctx.current_clock = pd.Timestamp("2026-03-15 14:00")
    port_mod.close_position("RELIANCE.NS", 1010.0, "TARGET")

    import sqlite3
    with sqlite3.connect(str(sandbox)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT entry_time, exit_time FROM paper_trades WHERE symbol = ?",
            ("RELIANCE.NS",),
        ).fetchone()
    assert row is not None, "paper_trades INSERT did not happen"
    assert row["entry_time"].startswith("2026-03-15"), (
        f"entry_time leaked wall-clock: {row['entry_time']!r}"
    )
    assert row["exit_time"].startswith("2026-03-15"), (
        f"exit_time leaked wall-clock: {row['exit_time']!r}"
    )
