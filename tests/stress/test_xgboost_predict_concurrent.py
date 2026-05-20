"""P34 stress test — direct xgboost.Booster.predict() under 8 workers.

Exercises the same C-extension as P33 but without the SHAP wrapper, so a
regression in the underlying xgboost predict path shows up here even if the
SHAP code path has been mitigated.

Out-of-scope for P33
--------------------
P33's per-thread fix lives in ``signals/generator.py`` only; it does NOT
touch ``models/signal_layer.py`` or the shared booster behind it.
``SignalLayer.predict`` still shares one ``XGBClassifier`` instance across
all worker threads, so concurrent ``self.model.predict(X[cols])`` races on
the underlying DMatrix construction (xgboost.data._from_pandas_df).
Surfacing the race here requires a per-thread booster copy or a
predict-side lock — both out of P33's commit scope. The tests are SKIPPED
with a clear marker so a future round picks the work up; the test file
exists now as scaffolding so the eventual fix has a verification hook.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor

import pytest


WORKERS = 8
ITERATIONS = 200

_OUT_OF_SCOPE = (
    "xgboost.Booster.predict races on DMatrix construction when SignalLayer "
    "shares a single booster across threads. Fix requires per-thread booster "
    "or predict-side lock in models/signal_layer.py — out of scope for P33 "
    "(signals/generator.py only). Tracked as P37 candidate for next round."
)


@pytest.mark.skip(reason=_OUT_OF_SCOPE)
def test_signal_layer_predict_8_workers_no_crash(ensemble, df_row):
    """8 workers calling signal_layer.predict directly must survive 200 calls."""
    def call_it(_):
        return ensemble.signal_layer.predict(df_row)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(call_it, range(ITERATIONS)))

    assert len(results) == ITERATIONS
    assert all(r is not None for r in results)


@pytest.mark.skip(reason=_OUT_OF_SCOPE)
def test_signal_layer_predict_proba_8_workers_no_crash(ensemble, df_row):
    """predict_proba runs the same booster code path; cover it explicitly."""
    def call_it(_):
        return ensemble.signal_layer.predict_proba(df_row)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(call_it, range(ITERATIONS)))

    assert len(results) == ITERATIONS
