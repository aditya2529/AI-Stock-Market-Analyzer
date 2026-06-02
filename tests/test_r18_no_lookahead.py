"""R18 — replay harness must not leak future bars into the slice.

The R18 audit (logs/r18_backtest_vs_live_gap.md) found that the three
replay-side slicing predicates used ``<=`` against ``ctx.current_clock``.
With OHLCV bars stamped at OPEN time (bar at index T carries the
[T, T+5min) window), the inclusive slice gave the engine access to a
``close`` resolved 5 minutes after the supposed wall-clock — the
10x backtest-vs-live PF gap.

These tests:
  1. Source-inspect the three patch sites for STRICT-less-than slicing.
  2. End-to-end fixture: build a synthetic raw frame whose bar-T close
     is dramatically different from bar-T-5min's close, then drive the
     patched _fetch_intraday at clock T and verify the returned slice's
     last bar is bar T-5min (NOT bar T).
  3. Same end-to-end check for the engineer_features and
     precompute-predict slicing.

Bisect-friendly: at the bug SHA all three tests fail; at the fix SHA
they go GREEN.
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Source-inspection guards ─────────────────────────────────────────


def _source_of_build_patches() -> str:
    from models.engine_replay_backtest import _build_patches
    return inspect.getsource(_build_patches)


def _source_of_run_replay() -> str:
    from models.engine_replay_backtest import run_replay
    return inspect.getsource(run_replay)


def test_fetch_intraday_slice_uses_strict_less_than():
    """``_patched_fetch_intraday`` must slice with ``raw.index < clock``
    (strict). Inclusive ``<=`` includes the bar at the current clock,
    whose ``close`` is the price 5 minutes in the future."""
    src = _source_of_build_patches()
    # Strip comments before scanning so historical docs don't trip us.
    code_only = "\n".join(line.split("#")[0] for line in src.splitlines())
    # We need to find the fetch-intraday-specific slice. The block we
    # care about uses raw.loc[raw.index ... ctx.current_clock].
    assert "raw.loc[raw.index < ctx.current_clock]" in code_only, (
        "_patched_fetch_intraday must slice with strict less-than. "
        "Inclusive <= includes the in-progress bar whose close is "
        "5-min future info."
    )
    assert "raw.loc[raw.index <= ctx.current_clock]" not in code_only, (
        "_patched_fetch_intraday still uses inclusive slice — the R18 "
        "look-ahead bug is back."
    )


def test_engineer_features_slice_uses_strict_less_than():
    """``_patched_engineer_features`` must slice the featured frame with
    strict less-than, same reason as ``_patched_fetch_intraday``."""
    src = _source_of_build_patches()
    code_only = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert "feat.loc[feat.index < clock]" in code_only, (
        "_patched_engineer_features must slice with strict less-than."
    )
    assert "feat.loc[feat.index <= clock]" not in code_only, (
        "_patched_engineer_features still uses inclusive slice — "
        "R18 look-ahead bug is back."
    )


def test_precomputed_predict_slice_uses_strict_less_than():
    """The precomputed prediction lookup inside ``run_replay`` (the
    ``_patched_predict`` defined in-line) must also slice with strict
    less-than."""
    src = _source_of_run_replay()
    code_only = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert "pred_full.loc[pred_full.index < clock]" in code_only, (
        "Precomputed _patched_predict must slice with strict less-than."
    )
    assert "pred_full.loc[pred_full.index <= clock]" not in code_only, (
        "Precomputed _patched_predict still uses inclusive slice — "
        "R18 look-ahead bug is back."
    )


# ── End-to-end fixture: prove the slice's last bar is T-5min ─────────


def _synthetic_raw_frame(n_bars: int = 200) -> pd.DataFrame:
    """Build a 5-min OHLCV frame where each bar's close is UNIQUELY
    identifiable by its index — so we can assert which bar ended up
    in ``.iloc[-1]`` after slicing.

    Strategy: each bar's close = its index-as-int. So slice.iloc[-1].close
    tells us exactly which bar the slicer kept as the last.
    """
    idx = pd.date_range("2026-01-05 09:15", periods=n_bars, freq="5min")
    closes = np.arange(n_bars, dtype=float) + 1000.0  # 1000, 1001, 1002, ...
    df = pd.DataFrame({
        "open":   closes,
        "high":   closes + 0.5,
        "low":    closes - 0.5,
        "close":  closes,
        "volume": np.full(n_bars, 1000),
    }, index=idx)
    df.index.name = "time"
    return df


def test_fetch_intraday_slice_last_bar_is_pre_clock():
    """End-to-end: build a synthetic raw frame, install
    _patched_fetch_intraday at a chosen clock T, and verify the
    returned slice's last bar is at T-5min (NOT T).
    """
    from models.engine_replay_backtest import _build_patches, ReplayContext

    raw = _synthetic_raw_frame()
    ctx = ReplayContext()
    ctx.raw_by_symbol = {"TEST.NS": raw}
    # Pick a clock somewhere in the middle so we have plenty of warmup.
    clock = raw.index[100]  # bar #100 has close = 1100.0
    ctx.current_clock = clock
    ctx.current_symbol = "TEST.NS"

    patches = _build_patches(ctx)
    fetch_patch = next((new for mod, name, new in patches
                        if name == "_fetch_intraday"), None)
    assert fetch_patch is not None, "no _fetch_intraday patch found"

    slice_df = fetch_patch("TEST.NS")
    assert slice_df is not None, "patched fetch returned None — expected a slice"

    last_bar = slice_df.iloc[-1]
    last_idx = slice_df.index[-1]
    # Bar #99 has close = 1099.0; that's what live would see at clock T (bar #100).
    # If the slice includes bar #100 (close = 1100.0), the look-ahead bug is present.
    assert last_idx < clock, (
        f"slice last index {last_idx} >= clock {clock} — replay sees "
        f"the in-progress bar T. This is the R18 look-ahead bug."
    )
    assert abs(last_bar["close"] - 1099.0) < 1e-9, (
        f"slice last bar close = {last_bar['close']}; expected 1099.0 "
        f"(bar at clock T-5min, the last fully-closed bar). Got "
        f"{last_bar['close']} which suggests the slice includes bar at T."
    )


def test_engineer_features_slice_last_bar_is_pre_clock():
    """Same end-to-end test for the engineer_features patch."""
    from models.engine_replay_backtest import _build_patches, ReplayContext

    raw = _synthetic_raw_frame()
    ctx = ReplayContext()
    ctx.featured_by_symbol = {"TEST.NS": raw}
    clock = raw.index[100]
    ctx.current_clock = clock
    ctx.current_symbol = "TEST.NS"

    patches = _build_patches(ctx)
    feat_patch = next((new for mod, name, new in patches
                       if name == "engineer_features"), None)
    assert feat_patch is not None, "no engineer_features patch found"

    feat_slice = feat_patch(raw)
    last_idx = feat_slice.index[-1]
    assert last_idx < clock, (
        f"engineer_features slice last index {last_idx} >= clock {clock} "
        f"— replay's feature engineering peeks at the in-progress bar."
    )
    assert abs(feat_slice["close"].iloc[-1] - 1099.0) < 1e-9, (
        f"engineer_features last bar close = {feat_slice['close'].iloc[-1]}; "
        f"expected 1099.0."
    )
