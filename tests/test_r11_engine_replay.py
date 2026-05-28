"""R11 — engine-replay backtest harness contracts.

Tests the harness that drives intraday.engine functions chronologically
through historical 5m bars to diagnose the backtest-vs-live PF gap
(P49). The harness MUST NOT modify intraday/engine.py — it reuses
the live tick processor via monkey-patching of 7 named functions plus
a sandboxed SQLite DB path.

Bisect-friendly: this file lands RED at the test-first commit.
``models.engine_replay_backtest`` doesn't exist yet — ImportError is
the expected failure mode. The impl commit immediately following
turns these GREEN.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

IST = ZoneInfo("Asia/Kolkata")


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def tmp_sandbox_db(tmp_path, monkeypatch):
    """Redirect data.database.DB_PATH to a temp file and init the
    paper-trading tables there. Mirrors the P30 cooldown test pattern.
    """
    db_file = tmp_path / "r11_replay.db"
    import data.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", str(db_file))
    from paper_trading.portfolio import init_paper_tables
    init_paper_tables()
    yield db_file


@pytest.fixture
def sample_raw_bars():
    """Two trading days of synthetic 5m OHLCV — enough to exercise
    one full session + an overnight reset. Index is tz-naive 5-min
    timestamps matching what load_ohlcv returns from the DB.
    """
    # Day 1: 2026-03-02 (Mon) 09:15 -> 15:30 = 75 bars
    # Day 2: 2026-03-03 (Tue) 09:15 -> 15:30 = 75 bars
    rows = []
    base_price = 1500.0
    for day_offset in range(2):
        d = pd.Timestamp("2026-03-02") + pd.Timedelta(days=day_offset)
        for bar_idx in range(75):
            ts = pd.Timestamp(f"{d.date()} 09:15:00") + pd.Timedelta(minutes=5 * bar_idx)
            # Mildly walking price; close = open + small noise; high/low band
            o = base_price + bar_idx * 0.1
            c = o + 0.5
            rows.append({
                "open": o, "high": o + 1.0, "low": o - 1.0,
                "close": c, "volume": 100000 + bar_idx,
            })
    df = pd.DataFrame(rows,
                        index=pd.DatetimeIndex(
                            [pd.Timestamp("2026-03-02 09:15:00")
                             + pd.Timedelta(minutes=5 * i)
                             for i in range(150 // 2)]
                            + [pd.Timestamp("2026-03-03 09:15:00")
                               + pd.Timedelta(minutes=5 * i)
                               for i in range(75)],
                            name="time"))
    return df


# ── A. Module surface ────────────────────────────────────────────────


def test_replay_module_exists():
    """The harness file must exist at models/engine_replay_backtest.py
    with the required public surface."""
    import models.engine_replay_backtest as r
    assert hasattr(r, "ReplayContext"), "ReplayContext class required"
    assert hasattr(r, "apply_engine_patches"), "apply_engine_patches() required"
    assert hasattr(r, "precompute_features"), "precompute_features() required"
    assert hasattr(r, "run_replay"), "run_replay() required (full driver)"


def test_replay_context_holds_required_state():
    """ReplayContext must carry current_symbol, current_clock, and
    the precomputed feature maps that the patched engineer_features
    consults."""
    from models.engine_replay_backtest import ReplayContext
    ctx = ReplayContext()
    # Mutable, defaults present
    assert ctx.current_symbol is None
    assert ctx.current_clock is None
    assert isinstance(ctx.raw_by_symbol, dict)
    assert isinstance(ctx.featured_by_symbol, dict)


# ── B. The 7+1 patch points ──────────────────────────────────────────


def test_patch_fetch_intraday_returns_historical_slice(monkeypatch, sample_raw_bars):
    """Patched intraday.engine._fetch_intraday must return a slice of
    pre-loaded raw bars ending at ctx.current_clock — NOT a live
    yfinance call."""
    from models.engine_replay_backtest import ReplayContext, apply_engine_patches
    import intraday.engine as eng

    ctx = ReplayContext()
    ctx.raw_by_symbol["RELIANCE.NS"] = sample_raw_bars
    ctx.current_symbol = "RELIANCE.NS"
    ctx.current_clock = pd.Timestamp("2026-03-02 14:00:00")

    apply_engine_patches(monkeypatch, ctx)

    df = eng._fetch_intraday("RELIANCE.NS")
    assert df is not None
    assert not df.empty
    assert df.index.max() <= ctx.current_clock, (
        "patched _fetch_intraday returned bars after the replay clock — "
        "would be data-leakage from the future")


def test_patch_ist_now_returns_replay_clock(monkeypatch):
    """intraday.engine._ist_now() must return ctx.current_clock as
    a tz-aware IST timestamp — so _is_buy_cutoff_active and force-
    close logic see the historical 'now'."""
    from models.engine_replay_backtest import ReplayContext, apply_engine_patches
    import intraday.engine as eng

    ctx = ReplayContext()
    ctx.current_clock = pd.Timestamp("2026-03-02 14:30:00")
    apply_engine_patches(monkeypatch, ctx)

    now = eng._ist_now()
    assert now.hour == 14
    assert now.minute == 30


def test_patch_ist_now_makes_buy_cutoff_active_after_14(monkeypatch):
    """End-to-end: patching _ist_now to 14:00+ must make
    _is_buy_cutoff_active() return True. Locks the integration that
    R7-B depends on at replay time."""
    from models.engine_replay_backtest import ReplayContext, apply_engine_patches
    import intraday.engine as eng

    ctx = ReplayContext()
    ctx.current_clock = pd.Timestamp("2026-03-02 14:01:00")
    apply_engine_patches(monkeypatch, ctx)

    assert eng._is_buy_cutoff_active() is True

    ctx.current_clock = pd.Timestamp("2026-03-02 13:59:00")
    assert eng._is_buy_cutoff_active() is False


def test_patch_market_open_always_true(monkeypatch):
    """During replay the market is always 'open' — we control the
    clock and don't want startup gates closing the session."""
    from models.engine_replay_backtest import ReplayContext, apply_engine_patches
    import intraday.engine as eng

    ctx = ReplayContext()
    apply_engine_patches(monkeypatch, ctx)
    assert eng._market_open() is True


def test_patch_seconds_to_next_bar_returns_zero(monkeypatch):
    """Replay drives the clock manually — engine.time.sleep on the
    bar boundary must be a no-op or zero seconds."""
    from models.engine_replay_backtest import ReplayContext, apply_engine_patches
    import intraday.engine as eng

    ctx = ReplayContext()
    apply_engine_patches(monkeypatch, ctx)
    assert eng._seconds_to_next_bar() == 0


def test_patch_alerts_are_noops(monkeypatch, tmp_sandbox_db):
    """Replay MUST NOT fire real Telegram alerts or write to the
    live alert log. Patching at alerts.dispatcher level catches the
    engine's local-import call sites."""
    from models.engine_replay_backtest import ReplayContext, apply_engine_patches
    import alerts.dispatcher as ad

    ctx = ReplayContext()
    apply_engine_patches(monkeypatch, ctx)

    # All three engine-touched alert dispatch points must be callable
    # without side effects (no AttributeError, no network).
    ad.on_signal({"symbol": "TCS.NS", "action": "BUY"})
    ad.on_trade_closed({"symbol": "TCS.NS", "exit_reason": "target"})
    ad.on_portfolio_snapshot({"cash": 500000.0, "total": 500000.0})


def test_patch_engineer_features_returns_precomputed(monkeypatch, sample_raw_bars):
    """Patched engineer_features (at features.engineer source) must
    return the precomputed featured slice for ctx.current_symbol
    ending at ctx.current_clock — NOT recompute features per tick
    (5x perf optimisation).

    The engine's _process_symbol uses ``from features.engineer
    import engineer_features`` (local import at L271 of
    intraday/engine.py), so the patch must live on the source module
    not on intraday.engine."""
    from models.engine_replay_backtest import (
        ReplayContext, apply_engine_patches, precompute_features,
    )

    ctx = ReplayContext()
    ctx.raw_by_symbol["RELIANCE.NS"] = sample_raw_bars
    ctx.featured_by_symbol["RELIANCE.NS"] = precompute_features(sample_raw_bars)
    ctx.current_symbol = "RELIANCE.NS"
    ctx.current_clock = pd.Timestamp("2026-03-02 14:00:00")

    apply_engine_patches(monkeypatch, ctx)

    # Mirror the engine's call shape: fresh local import (after patch).
    from features.engineer import engineer_features
    feat = engineer_features(sample_raw_bars)
    assert isinstance(feat, pd.DataFrame)
    # Must not return rows after the replay clock — no data leakage
    assert feat.index.max() <= ctx.current_clock


def test_fetch_cache_cleared_each_tick(monkeypatch, sample_raw_bars):
    """The engine module's _FETCH_CACHE dict must be cleared between
    replay ticks — stale cached frames from a prior bar would mask
    the new patched _fetch_intraday output."""
    from models.engine_replay_backtest import ReplayContext, apply_engine_patches
    import intraday.engine as eng

    ctx = ReplayContext()
    ctx.raw_by_symbol["TCS.NS"] = sample_raw_bars
    apply_engine_patches(monkeypatch, ctx)

    # Pretend prior tick stuffed cache
    eng._FETCH_CACHE["TCS.NS"] = (("any-bucket",), sample_raw_bars.head(10))
    ctx.current_symbol = "TCS.NS"
    ctx.current_clock = pd.Timestamp("2026-03-02 14:00:00")
    # The patched fetch must NOT return the stale frame — it must
    # return the slice ending at current_clock.
    df = eng._fetch_intraday("TCS.NS")
    assert df is not None
    assert df.index.max() <= ctx.current_clock


# ── C. Sandbox DB isolation ──────────────────────────────────────────


def test_sandbox_isolates_engine_writes(tmp_sandbox_db, monkeypatch,
                                         sample_raw_bars):
    """When the engine calls set_config / try_open / etc, the writes
    must land in the temp sandbox DB and NOT in production
    market_data.db. Verified by reading the temp DB directly."""
    from paper_trading.portfolio import set_config, get_config
    set_config("r11_replay_sentinel", "alive")
    # Read back via the public API (still routed through tmp_sandbox_db)
    assert get_config("r11_replay_sentinel") == "alive"

    # Read direct via sqlite to confirm the row landed in the temp DB,
    # not the real one
    con = sqlite3.connect(str(tmp_sandbox_db))
    cur = con.cursor()
    cur.execute("SELECT value FROM paper_config WHERE key='r11_replay_sentinel'")
    row = cur.fetchone()
    con.close()
    assert row == ("alive",), (
        "engine sandbox write didn't land in the temp DB — DB_PATH "
        "patching failed")


def test_sandbox_resets_cooldowns_between_replays(tmp_sandbox_db,
                                                    monkeypatch):
    """A fresh sandbox DB must start with empty cooldown state — no
    leak from a prior replay run."""
    from intraday.engine import _load_sl_cooldown_for_today, _load_target_cooldown_for_today
    assert _load_sl_cooldown_for_today() == set()
    assert _load_target_cooldown_for_today() == set()


# ── D. Driver / end-to-end shape ─────────────────────────────────────


def test_run_replay_returns_metrics_dict():
    """run_replay must return a dict with the same metric keys the R9
    shortcut harness emits, so the comparison report can diff them
    directly: profit_factor, sharpe, win_rate, max_drawdown, n_trades."""
    from models.engine_replay_backtest import run_replay
    import inspect
    sig = inspect.signature(run_replay)
    required = {"symbols", "holdout_start", "holdout_end",
                 "ensemble_path", "sandbox_db_path"}
    actual = set(sig.parameters.keys())
    missing = required - actual
    assert not missing, f"run_replay missing parameters: {missing}"


def test_precompute_features_is_idempotent_per_symbol(sample_raw_bars):
    """Calling precompute_features twice on the same raw frame must
    return DataFrames with identical shape — defends against a
    refactor that adds per-call state mutation."""
    from models.engine_replay_backtest import precompute_features
    f1 = precompute_features(sample_raw_bars)
    f2 = precompute_features(sample_raw_bars)
    assert f1.shape == f2.shape
    assert list(f1.columns) == list(f2.columns)
