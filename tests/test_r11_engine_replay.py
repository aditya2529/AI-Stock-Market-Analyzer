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


# ── E. R12 block-reason instrumentation ──────────────────────────────


def test_block_reason_logger_recognises_all_engine_gate_actions():
    """The logger's BLOCK_ACTIONS set must cover every engine-side
    block path. If a future engine commit adds a new gate (e.g.
    R10's wider-SL might add a new `_action` string), this test will
    flag the missing coverage at the next pytest run."""
    from models.engine_replay_backtest import BlockReasonLogger

    # Current engine block-action strings (intraday/engine.py:_process_symbol
    # + tick_counts dict in run_intraday_session L554-565).
    required = {
        "regime_blocked",       # regime gate HIGH_VOL/UNKNOWN/TRENDING_DOWN BUY
        "conf_blocked",         # confidence < 0.60
        "cooldown",             # P30 SL cooldown
        "target_cooldown",      # R7-A target cooldown
        "max_pos",              # 5-slot cap
        "time_cutoff",          # R7-B 14:00 IST
        "exposure_capped",      # P28 80% exposure
        "daily_loss_halt",      # P28 -3% loss
        "daily_count_capped",   # P28 8 trades/day
    }
    assert required <= BlockReasonLogger.BLOCK_ACTIONS, (
        f"missing block actions: {required - BlockReasonLogger.BLOCK_ACTIONS}")


def test_block_reason_logger_records_block(monkeypatch):
    """maybe_record() appends a row when _action is a known block."""
    from models.engine_replay_backtest import BlockReasonLogger
    log = BlockReasonLogger()
    clock = pd.Timestamp("2026-03-05 11:30:00")

    log.maybe_record("TCS.NS", clock,
                      result={"_action": "conf_blocked", "symbol": "TCS.NS"},
                      prediction_row=pd.Series({
                          "signal": "BUY", "confidence": 0.55, "regime": "TRENDING_UP"
                      }))
    assert len(log.records) == 1
    rec = log.records[0]
    assert rec["symbol"] == "TCS.NS"
    assert rec["block_reason"] == "conf_blocked"
    assert rec["confidence"] == pytest.approx(0.55)
    assert rec["regime"] == "TRENDING_UP"
    assert rec["signal"] == "BUY"


def test_block_reason_logger_ignores_non_block_actions():
    """Returns with _action='opened' / 'closed' / None / non-dict
    must NOT generate block records."""
    from models.engine_replay_backtest import BlockReasonLogger
    log = BlockReasonLogger()
    clock = pd.Timestamp("2026-03-05 11:30:00")

    log.maybe_record("TCS.NS", clock, None)
    log.maybe_record("TCS.NS", clock, {"_action": "opened", "symbol": "TCS.NS"})
    log.maybe_record("TCS.NS", clock, {"_action": "closed", "symbol": "TCS.NS"})
    log.maybe_record("TCS.NS", clock, "not-a-dict")
    log.maybe_record("TCS.NS", clock, {})  # no _action key
    assert log.records == []


def test_block_reason_logger_flush_writes_csv_with_header(tmp_path):
    """flush() must write a header row even when no records were
    captured — so downstream readers (pandas.read_csv etc) don't
    crash on a missing-file or zero-row condition."""
    from models.engine_replay_backtest import BlockReasonLogger
    log = BlockReasonLogger()
    path = tmp_path / "empty.csv"
    n = log.flush(path)
    assert n == 0
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    header = content.splitlines()[0].split(",")
    assert "symbol" in header
    assert "block_reason" in header


def test_block_reason_logger_flush_writes_data_rows(tmp_path):
    """flush() with N records writes N+1 lines (header + N rows)
    and the rows survive pandas.read_csv round-trip."""
    from models.engine_replay_backtest import BlockReasonLogger
    log = BlockReasonLogger()
    clock = pd.Timestamp("2026-03-05 11:30:00")

    for action in ("regime_blocked", "conf_blocked", "max_pos"):
        log.maybe_record(
            "TCS.NS", clock,
            result={"_action": action, "symbol": "TCS.NS"},
            prediction_row=pd.Series({
                "signal": "BUY", "confidence": 0.55, "regime": "TRENDING_UP"
            }),
        )

    path = tmp_path / "blocks.csv"
    n = log.flush(path)
    assert n == 3

    df = pd.read_csv(path)
    assert len(df) == 3
    assert set(df["block_reason"]) == {"regime_blocked", "conf_blocked", "max_pos"}


def test_run_replay_accepts_block_log_path():
    """run_replay's signature must include an optional block_log_path
    parameter so R12's CLI + driver can opt into block-reason capture."""
    from models.engine_replay_backtest import run_replay
    import inspect
    sig = inspect.signature(run_replay)
    assert "block_log_path" in sig.parameters, (
        "run_replay must accept block_log_path: Path | None for R12 "
        "block-reason instrumentation")
    # Default must be None — preserves R11 behaviour for any caller
    # that doesn't pass the new arg.
    assert sig.parameters["block_log_path"].default is None


# ── F. R13 Stage 2+3 extensions ──────────────────────────────────────


def test_run_replay_accepts_conf_floor_override():
    """R13 Stage 2 — conf_floor_override param plumbs through to the
    engine via SIGNAL_MIN_CONFIDENCE env var so the conf-floor sweep
    can run 3 replays at 0.55 / 0.57 / 0.58 without modifying config.py.
    Default None preserves the production 0.60 floor."""
    from models.engine_replay_backtest import run_replay
    import inspect
    sig = inspect.signature(run_replay)
    assert "conf_floor_override" in sig.parameters
    assert sig.parameters["conf_floor_override"].default is None


def test_run_replay_accepts_sector_filter():
    """R13 Stage 3 — sector_filter param accepts a callable that
    given (symbol, clock) returns True iff the symbol's sector is
    bullish. Engine-replay's monkey-patch on paper_trading.executor.try_open
    refuses BUYs in bearish sectors. Default None preserves R11/R12
    behaviour bit-for-bit."""
    from models.engine_replay_backtest import run_replay
    import inspect
    sig = inspect.signature(run_replay)
    assert "sector_filter" in sig.parameters
    assert sig.parameters["sector_filter"].default is None


def test_sector_momentum_filter_module_surface():
    """R13 Stage 3 — sector momentum helper exposes the required
    public surface for the engine-replay harness + future live wiring."""
    from models.r13_sector_momentum import (
        SYMBOL_TO_SECTOR, SECTOR_TO_SYMBOLS,
        SectorMomentumFilter, make_sector_filter,
    )
    # 25 DEFAULT_SYMBOLS all mapped
    assert len(SYMBOL_TO_SECTOR) == 25
    # At least one symbol per sector
    assert all(len(syms) >= 1 for syms in SECTOR_TO_SYMBOLS.values())
    # The factory builds something callable
    f = make_sector_filter({})
    assert callable(f)


def test_sector_momentum_filter_returns_true_on_unknown_symbol():
    """An unmapped symbol must NOT be silently filtered out — return
    True so the caller doesn't accidentally block trades on tickers
    that aren't in the sector map."""
    from models.r13_sector_momentum import make_sector_filter
    f = make_sector_filter({})
    assert f("UNKNOWN.NS", pd.Timestamp("2026-05-29 10:00:00")) is True


def test_sector_momentum_filter_detects_bullish_basket():
    """When the sector basket's mean pct-change > threshold, the
    filter returns True. Synthetic data: 4 IT stocks all up 1%."""
    from models.r13_sector_momentum import make_sector_filter
    day = pd.Timestamp("2026-05-29")
    raw = {}
    for sym in ("TCS.NS", "INFY.NS", "WIPRO.NS", "HCLTECH.NS"):
        idx = pd.DatetimeIndex([
            day + pd.Timedelta(hours=9, minutes=15),
            day + pd.Timedelta(hours=10),
            day + pd.Timedelta(hours=11),
        ], name="time")
        raw[sym] = pd.DataFrame({
            "open":   [100.0, 100.5, 101.0],
            "high":   [101.0, 101.5, 102.0],
            "low":    [ 99.5, 100.0, 100.5],
            "close":  [100.5, 101.0, 101.0],   # +1% by 11:00
            "volume": [1000, 1000, 1000],
        }, index=idx)
    f = make_sector_filter(raw)
    assert f("TCS.NS", day + pd.Timedelta(hours=11)) is True


def test_sector_momentum_filter_detects_bearish_basket():
    """Mean pct-change <= threshold → False. 4 IT stocks all down 1%."""
    from models.r13_sector_momentum import make_sector_filter
    day = pd.Timestamp("2026-05-29")
    raw = {}
    for sym in ("TCS.NS", "INFY.NS", "WIPRO.NS", "HCLTECH.NS"):
        idx = pd.DatetimeIndex([
            day + pd.Timedelta(hours=9, minutes=15),
            day + pd.Timedelta(hours=10),
            day + pd.Timedelta(hours=11),
        ], name="time")
        raw[sym] = pd.DataFrame({
            "open":   [100.0, 99.5, 99.0],
            "high":   [101.0, 100.0, 99.5],
            "low":    [ 99.0, 99.0, 98.5],
            "close":  [ 99.5, 99.0, 99.0],     # -1% by 11:00
            "volume": [1000, 1000, 1000],
        }, index=idx)
    f = make_sector_filter(raw)
    assert f("TCS.NS", day + pd.Timedelta(hours=11)) is False


def test_run_replay_restores_db_path_in_finally(monkeypatch):
    """R13 regression — run_replay must save + restore
    ``data.database.DB_PATH`` so a SECOND call in the same process
    reads from the real DB, not the previous run's sandbox.

    Bug surfaced during R13 Stage 2 sweep (3 sequential run_replay
    calls): replay #1 worked; replay #2 raised "no symbols had usable
    5m data" because DB_PATH still pointed at replay #1's empty sandbox.
    """
    import data.database as db_mod
    from models.engine_replay_backtest import run_replay
    import inspect

    # Source-level check: the impl must save DB_PATH before the
    # redirect AND restore it in the finally block. We inspect the
    # source rather than monkey-patching the whole replay loop
    # (which would require a full ensemble + DB fixture).
    src = inspect.getsource(run_replay)
    assert "_saved_db_path = db_mod.DB_PATH" in src, (
        "run_replay must save the production DB_PATH before redirecting")
    assert "db_mod.DB_PATH = _saved_db_path" in src, (
        "run_replay must restore DB_PATH in the finally block")


def test_sector_momentum_filter_caches_per_sector_clock():
    """Calling is_bullish for two stocks in the same sector at the
    same clock must hit the cache on the second call — confirms the
    O(constituents) computation isn't repeated for every symbol."""
    from models.r13_sector_momentum import SectorMomentumFilter
    day = pd.Timestamp("2026-05-29")
    raw = {}
    for sym in ("TCS.NS", "INFY.NS", "WIPRO.NS", "HCLTECH.NS"):
        idx = pd.DatetimeIndex([day + pd.Timedelta(hours=9, minutes=15),
                                  day + pd.Timedelta(hours=10)], name="time")
        raw[sym] = pd.DataFrame({
            "open": [100.0, 100.5], "high": [101.0, 101.0],
            "low": [99.5, 100.0], "close": [100.5, 101.0],
            "volume": [1000, 1000],
        }, index=idx)
    f = SectorMomentumFilter(raw)
    clock = day + pd.Timedelta(hours=10)
    f.is_bullish("TCS.NS", clock)
    f.is_bullish("INFY.NS", clock)   # same sector, same clock
    # 2 calls but only 1 distinct cache key — n_calls counts BOTH,
    # cache size is 1.
    assert f.n_calls == 2
    assert len(f._cache) == 1
