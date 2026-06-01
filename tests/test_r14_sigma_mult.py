"""R14 — sigma_mult parameterization for the vol-scaled label cutoff.

Adds an optional ``sigma_mult`` kwarg to:
  - ``backtesting.engine.make_labels``      (default 0.5 — unchanged)
  - ``models.ensemble.Ensemble.fit``         (default 0.5 — unchanged,
                                              plumbed straight through to
                                              make_labels)

The R14 brief explicitly authorised this exception to the sacred-dirs
rule on the basis that:
  * the kwarg is purely additive,
  * the default preserves all existing callers byte-for-byte, and
  * the regression tests in this file lock that property down.

Bisect-friendly: this file lands RED at the test-first commit. The
sigma_mult kwarg doesn't exist on either function yet — TypeError
on the override tests, and the signature check fails — so the test
file fails at this SHA. The impl commit immediately following turns
all three GREEN.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _synthetic_5m_frame(n_bars: int = 600, seed: int = 42) -> pd.DataFrame:
    """Build a synthetic 5-min OHLCV frame with reproducible forward
    returns spanning the noise floor + above. ``n_bars`` is generous so
    the 500-window rolling sigma in make_labels has plenty of data.
    """
    rng = np.random.default_rng(seed)
    # Daily-ish drift with bar-scale noise around it. Returns standardised
    # so a chunk of bars cross any reasonable threshold and a chunk don't.
    rets = rng.normal(loc=0.0, scale=0.003, size=n_bars)
    closes = 100.0 * np.exp(np.cumsum(rets))
    df = pd.DataFrame({
        "open": closes,
        "high": closes * 1.001,
        "low": closes * 0.999,
        "close": closes,
        "volume": rng.integers(1000, 100_000, size=n_bars),
    }, index=pd.date_range("2025-01-01 09:15", periods=n_bars, freq="5min",
                             name="time"))
    return df


# ── make_labels ──────────────────────────────────────────────────────


def test_make_labels_default_sigma_mult_is_0_5():
    """A make_labels() call with no sigma_mult kwarg must produce a
    label Series byte-identical to one with sigma_mult=0.5 — proves the
    default preserves current behaviour for every existing caller
    (R9 retrain, retrain_v2b, retrain_v2c, run_walk_forward_ensemble,
    every test that already exercises this).
    """
    from backtesting.engine import make_labels
    df = _synthetic_5m_frame()
    default = make_labels(df, lookahead=6, vol_scaled=True)
    explicit = make_labels(df, lookahead=6, vol_scaled=True, sigma_mult=0.5)
    assert default.equals(explicit), (
        "make_labels default behaviour changed — sigma_mult=0.5 must "
        "produce byte-identical labels to the no-kwarg call.")


def test_make_labels_accepts_sigma_mult_override():
    """sigma_mult=0.25 lowers the label cutoff vs the 0.5 default, so
    the strictly tighter threshold MUST produce >= the BUY+SELL count
    of the default — and in practice strictly MORE for non-degenerate
    synthetic data.
    """
    from backtesting.engine import make_labels
    df = _synthetic_5m_frame()
    labels_05 = make_labels(df, lookahead=6, vol_scaled=True, sigma_mult=0.5)
    labels_025 = make_labels(df, lookahead=6, vol_scaled=True, sigma_mult=0.25)
    n05 = (labels_05 != "HOLD").sum()
    n025 = (labels_025 != "HOLD").sum()
    assert n025 > n05, (
        f"sigma_mult=0.25 should produce strictly more non-HOLD labels "
        f"than sigma_mult=0.5; got n025={n025} vs n05={n05}")
    # Every bar labelled by the 0.5 cutoff must also be labelled by the
    # 0.25 cutoff (with the same sign) — 0.25 is the tighter cutoff so
    # its label set is a superset.
    is_labelled_05 = labels_05 != "HOLD"
    same_sign = (labels_05[is_labelled_05] == labels_025[is_labelled_05]).all()
    assert same_sign, (
        "Where 0.5 emitted a label, 0.25 must emit the same sign — "
        "asymmetric divergence would mean the two cutoffs see different "
        "forward returns, which is impossible.")


def test_make_labels_sigma_mult_ignored_when_vol_scaled_false():
    """sigma_mult only takes effect when vol_scaled=True. Passing it
    with vol_scaled=False must NOT raise and must not affect output —
    keeps the fixed-threshold path orthogonal."""
    from backtesting.engine import make_labels
    df = _synthetic_5m_frame()
    fixed_default = make_labels(df, lookahead=6,
                                  buy_threshold=0.003, sell_threshold=-0.003,
                                  vol_scaled=False)
    fixed_with_sm = make_labels(df, lookahead=6,
                                  buy_threshold=0.003, sell_threshold=-0.003,
                                  vol_scaled=False, sigma_mult=0.25)
    assert fixed_default.equals(fixed_with_sm), (
        "sigma_mult must be a no-op when vol_scaled=False — the fixed-"
        "threshold path doesn't consult it.")


# ── Ensemble.fit ─────────────────────────────────────────────────────


def test_ensemble_fit_accepts_sigma_mult():
    """Ensemble.fit must accept sigma_mult: float = 0.5. The kwarg is
    plumbed straight through to make_labels (verified by the previous
    two tests' contract on make_labels).
    """
    from models.ensemble import Ensemble
    import inspect
    sig = inspect.signature(Ensemble.fit)
    assert "sigma_mult" in sig.parameters, (
        "Ensemble.fit must accept sigma_mult: float kwarg for R14 v3 "
        "retrain (vol-scaled cutoff override).")
    assert sig.parameters["sigma_mult"].default == 0.5, (
        "Ensemble.fit sigma_mult default must be 0.5 to preserve every "
        "existing caller's behaviour byte-for-byte.")
