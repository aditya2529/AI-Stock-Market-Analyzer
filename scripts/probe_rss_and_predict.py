"""Quick probe to verify (a) get_rss_mb() returns a real number and (b)
whether direct xgboost predict from multiple threads survives without SHAP.

Run: python scripts/probe_rss_and_predict.py
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.stress.conftest import get_rss_mb, measure_explainer_rss_cost


def probe_rss():
    a = get_rss_mb()
    print(f"[probe] get_rss_mb() = {a:.1f} MB")
    big = [0] * (10 * 1024 * 1024)  # ~80MB allocation
    b = get_rss_mb()
    print(f"[probe] after 80MB alloc, get_rss_mb() = {b:.1f} MB; delta = {b-a:+.1f}")
    del big
    return a > 0


def probe_explainer_cost():
    from models.ensemble import Ensemble
    e = Ensemble.load("ensemble_intraday.pkl")
    cost = measure_explainer_rss_cost(e.signal_layer.model)
    print(f"[probe] TreeExplainer RSS cost = {cost:.1f} MB")
    return cost


def probe_xgboost_direct_concurrent():
    """Does ensemble.signal_layer.predict survive 8w x 50 calls without SHAP?"""
    from concurrent.futures import ThreadPoolExecutor
    import pandas as pd
    from models.ensemble import Ensemble
    from config import FEATURE_COLUMNS, INTRADAY_FEATURE_COLUMNS

    e = Ensemble.load("ensemble_intraday.pkl")
    all_cols = sorted(set(FEATURE_COLUMNS) | set(INTRADAY_FEATURE_COLUMNS))
    row = {c: 0.5 for c in all_cols}
    row["close"] = 1000.0
    df = pd.DataFrame([row])
    feature_cols = [c for c in FEATURE_COLUMNS if c in df.columns]
    X = df[feature_cols]

    def call_it(_):
        return e.signal_layer.predict(X)

    print("[probe] starting xgboost-direct concurrent test (8w x 50)...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(call_it, range(50)))
    print(f"[probe] xgboost predict survived. {len(results)} results.")
    return True


if __name__ == "__main__":
    print("=== RSS probe ===")
    rss_ok = probe_rss()
    print(f"[probe] rss_ok = {rss_ok}")
    print()
    print("=== TreeExplainer RSS probe ===")
    probe_explainer_cost()
    print()
    print("=== xgboost direct concurrent probe ===")
    probe_xgboost_direct_concurrent()
