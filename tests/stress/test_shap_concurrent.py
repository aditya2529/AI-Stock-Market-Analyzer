"""P34 stress test — replays P33 (SHAP + xgboost predict race).

TreeExplainer RSS cost is logged at session start by tests/stress/conftest.py;
search for "TreeExplainer RSS cost" in the pytest stderr output. The
PENDING_AUDIT_FIXES.md P33 FIXED line records the value at the time the
per-thread cache landed (grep PENDING_AUDIT_FIXES.md for "TreeExplainer RSS").

The 8-worker projection determined whether P33 shipped as per-thread cache
(< 150 MB per explainer) or fell back to the module-level lock (>= 150 MB).

Auto-gate
---------
Under the May-19 lock-based hot-patch (commit 89b7eaf), the module-level
``threading.Lock`` only guards ``explainer.shap_values()`` — NOT
``_get_explainer()``. When multiple workers race on the cache-miss branch
they each construct a ``shap.TreeExplainer(model)`` concurrently; that
construction internally calls ``xgboost.Booster.predict()`` which is not
thread-safe, and the OS kills the process. (This is the Wed-AM-May-20
crash signature.) Until the P33 per-thread cache lands, the tests below
are gated by ``_LOCK_BASED`` so pytest does not death-spiral on collection.
The marker auto-clears the moment ``_SHAP_LOCK`` disappears from
``signals/generator.py``.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor

import pytest

import signals.generator as _gen

from signals.generator import _shap_reasons

_LOCK_BASED = hasattr(_gen, "_SHAP_LOCK")
_SKIP_REASON = (
    "signals/generator.py still has _SHAP_LOCK; under that hot-patch the "
    "lock wraps shap_values() but not _get_explainer(), so concurrent "
    "TreeExplainer construction crashes the interpreter. Auto-re-enables "
    "when the P33 threading.local() per-thread cache lands and _SHAP_LOCK "
    "is removed."
)


WORKERS = 8
ITERATIONS = 200

# Stuck-scenario fallback from AUDIT_ROUND_4_BRIEF.md: when the 8/200
# parameters fail to reproduce a race, bump to these.
HIGH_INTENSITY_WORKERS = 16
HIGH_INTENSITY_ITERATIONS = 500


@pytest.mark.skipif(_LOCK_BASED, reason=_SKIP_REASON)
def test_shap_reasons_8_workers_no_crash(ensemble, df_row):
    """8 workers calling _shap_reasons in parallel must survive 200 calls."""
    def call_it(_):
        return _shap_reasons(ensemble, df_row, "BUY")

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(call_it, range(ITERATIONS)))

    assert len(results) == ITERATIONS
    assert all(isinstance(r, list) for r in results)


@pytest.mark.skipif(_LOCK_BASED, reason=_SKIP_REASON)
def test_shap_reasons_16_workers_high_intensity_no_crash(ensemble, df_row):
    """High-intensity sweep — 16 workers, 500 calls. Surfaces races the 8/200
    primary sweep misses; matches the brief's bumped-parameter fallback."""
    def call_it(_):
        return _shap_reasons(ensemble, df_row, "BUY")

    with ThreadPoolExecutor(max_workers=HIGH_INTENSITY_WORKERS) as ex:
        results = list(ex.map(call_it, range(HIGH_INTENSITY_ITERATIONS)))

    assert len(results) == HIGH_INTENSITY_ITERATIONS
    assert all(isinstance(r, list) for r in results)
