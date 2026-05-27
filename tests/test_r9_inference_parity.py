"""R9 — v1 vs v2 ensemble inference parity.

The retrained v2 ensemble MUST return the same DataFrame shape from
``predict_with_confidence`` as v1 on the same input. The engine
consumes this DataFrame via ``result.iloc[-1]['signal' | 'confidence'
| 'regime']`` (see intraday/engine.py:_process_symbol L258-263); a
silent architecture change to v2 that drops a column, renames one,
or shifts a dtype would break the engine on first prod inference.

This test defends that contract.

Bisect-friendly: RED at the test-first commit (v2 artifact doesn't
exist yet → FileNotFoundError). The retrain commit produces
``models/saved/ensemble_intraday_v2.pkl`` and turns this GREEN.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


ROOT = Path(__file__).resolve().parents[1]
V1_PATH = ROOT / "models" / "saved" / "ensemble_intraday.pkl"
V2_PATH = ROOT / "models" / "saved" / "ensemble_intraday_v2.pkl"

SAMPLE_SYMBOL = "RELIANCE.NS"   # any DEFAULT_SYMBOL works; picked for liquidity
SAMPLE_BARS = 500                # last N 5m bars — keep test fast (~3s)


@pytest.fixture
def sample_features():
    """Load + engineer features for one DEFAULT_SYMBOL. Skips if there
    aren't enough featured rows (rare on a fresh DB; defensive)."""
    from data.database import load_ohlcv
    from features.engineer import engineer_features

    df = load_ohlcv(SAMPLE_SYMBOL, resolution="5m")
    if df.empty:
        pytest.skip(f"{SAMPLE_SYMBOL} 5m data missing — populate DB first")
    df = df.tail(SAMPLE_BARS)
    feat = engineer_features(df)
    if len(feat) < 100:
        pytest.skip(f"only {len(feat)} featured rows — engineer_features "
                     f"dropped too many; check fixture")
    return feat


def test_v2_artifact_present():
    """Sanity check — v2 artifact must exist before any parity assertion
    can fire. RED at the test-first commit; GREEN after retrain
    produces models/saved/ensemble_intraday_v2.pkl.
    """
    assert V2_PATH.exists(), (
        f"v2 artifact missing at {V2_PATH} — R9 retrain must run "
        f"first (python models/retrain_and_backtest_v2.py)"
    )


def test_v1_artifact_present_baseline():
    """Sanity check — v1 must also be on disk for the comparison. If
    this fails the rest is moot; flag the broken baseline."""
    assert V1_PATH.exists(), (
        f"v1 artifact missing at {V1_PATH} — engine cannot boot "
        f"without it; restore from backup before any retrain")


def test_v1_v2_inference_shape_parity(sample_features):
    """Core parity contract — v1 and v2 must return identical:
      - Row count (same input → same length output)
      - Column names (`signal`, `confidence`, `regime` per the engine's
        consumption pattern)
      - Column dtypes (signal=object/str, confidence=float, regime=object/str)
      - Index alignment (both keyed by the input's DatetimeIndex)

    Plus value-domain sanity on the output:
      - All `signal` values in {BUY, HOLD, SELL}
      - All `confidence` values in [0, 1]
    """
    if not V2_PATH.exists():
        pytest.skip("v2 artifact missing — covered by test_v2_artifact_present")
    if not V1_PATH.exists():
        pytest.skip("v1 artifact missing — covered by test_v1_artifact_present_baseline")

    from models.ensemble import Ensemble
    v1 = Ensemble.load(name="ensemble_intraday.pkl")
    v2 = Ensemble.load(name="ensemble_intraday_v2.pkl")

    r1 = v1.predict_with_confidence(sample_features)
    r2 = v2.predict_with_confidence(sample_features)

    # ── Shape parity ────────────────────────────────────────────────
    assert len(r1) == len(r2), (
        f"row count drift: v1={len(r1)} v2={len(r2)}")
    assert list(r1.columns) == list(r2.columns), (
        f"column drift: v1={list(r1.columns)} v2={list(r2.columns)}")
    for col in ("signal", "confidence", "regime"):
        assert col in r1.columns, f"v1 missing '{col}'"
        assert col in r2.columns, f"v2 missing '{col}'"
    for col in ("signal", "confidence", "regime"):
        assert r1[col].dtype == r2[col].dtype, (
            f"dtype drift on '{col}': v1={r1[col].dtype} v2={r2[col].dtype}")

    # ── Index alignment ─────────────────────────────────────────────
    assert (r1.index == r2.index).all(), (
        "index drift between v1 and v2 outputs — both must align with "
        "the input feature DataFrame")

    # ── Value-domain sanity ─────────────────────────────────────────
    valid_signals = {"BUY", "HOLD", "SELL"}
    v1_signals = set(r1["signal"].unique())
    v2_signals = set(r2["signal"].unique())
    assert v1_signals <= valid_signals, (
        f"v1 emitted unexpected signal values: {v1_signals - valid_signals}")
    assert v2_signals <= valid_signals, (
        f"v2 emitted unexpected signal values: {v2_signals - valid_signals}")

    assert r1["confidence"].between(0, 1).all(), "v1 confidence out of [0,1]"
    assert r2["confidence"].between(0, 1).all(), "v2 confidence out of [0,1]"


def test_v2_validate_passes_on_sample(sample_features):
    """v2 must pass the Ensemble.validate() sanity check the same way
    v1 does — this is the same gate save() applies before overwriting
    a model artifact. If v2 fails validate, ops should NOT ship even
    if the PF metric looks good."""
    if not V2_PATH.exists():
        pytest.skip("v2 artifact missing — covered by test_v2_artifact_present")

    from models.ensemble import Ensemble
    v2 = Ensemble.load(name="ensemble_intraday_v2.pkl")
    result = v2.validate(sample_features, symbol=SAMPLE_SYMBOL)
    assert result["ok"], (
        f"v2 validate FAILED on {SAMPLE_SYMBOL}: {result['errors']}")
