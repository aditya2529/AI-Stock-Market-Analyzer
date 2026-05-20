"""Shared fixtures + RSS measurement for tests/stress/ (P34).

These tests deliberately exercise xgboost, SHAP, SQLite, urllib SSL, and
yfinance under 8 concurrent worker threads to surface C-extension races
that sequential unit tests miss. They are the meta-fix that lets future
audit rounds catch threading bugs BEFORE production trades fire.

RSS measurement
---------------
Windows lacks ``resource``; ``psutil`` is not pinned; an earlier
``ctypes psapi`` attempt returned 0 because ``cb`` was not set on the
``PROCESS_MEMORY_COUNTERS`` struct before the call. ``get_rss_mb()``
below sets ``cb`` first and falls back to ``tasklist`` if the API
returns failure.

``measure_explainer_rss_cost(model)`` brackets a single
``shap.TreeExplainer(model)`` construction with two RSS reads. The
delta is the per-explainer memory cost. ``pytest_sessionstart`` prints
this number at the top of every test session so the audit team can grep
for the latest measurement.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import gc
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from config import FEATURE_COLUMNS, INTRADAY_FEATURE_COLUMNS
from models.ensemble import Ensemble

ENSEMBLE_PATH = PROJECT_ROOT / "models" / "saved" / "ensemble_intraday.pkl"


# ---------- Windows RSS measurement ----------

class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_psapi = ctypes.WinDLL("psapi.dll")
_psapi.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), wintypes.DWORD,
]
_psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE


def get_rss_mb() -> float:
    """Return current process RSS in MB via psapi GetProcessMemoryInfo.

    Requires explicit ``argtypes``/``restype`` because the default int marshalling
    truncates the HANDLE on 64-bit Windows and the call silently writes garbage.
    Falls back to ``tasklist`` if the psapi call fails for any reason.
    """
    counters = _PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    h = ctypes.windll.kernel32.GetCurrentProcess()
    if _psapi.GetProcessMemoryInfo(h, ctypes.byref(counters), counters.cb):
        return counters.WorkingSetSize / (1024 * 1024)
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {os.getpid()}", "/FO", "CSV", "/NH"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        parts = [p.strip(' "\r\n') for p in out.split(",")]
        return float(parts[4].replace(" K", "").replace(",", "")) / 1024
    except Exception:
        return 0.0


def measure_explainer_rss_cost(model) -> float:
    """RSS delta in MB across a single shap.TreeExplainer(model) construction."""
    import shap
    gc.collect()
    before = get_rss_mb()
    explainer = shap.TreeExplainer(model)
    after = get_rss_mb()
    _ = explainer.expected_value
    return max(0.0, after - before)


# ---------- ensemble + df_row fixtures ----------

@pytest.fixture(scope="session")
def ensemble():
    """Load the production intraday ensemble once per stress session."""
    if not ENSEMBLE_PATH.exists():
        pytest.skip(f"missing model: {ENSEMBLE_PATH}")
    return Ensemble.load("ensemble_intraday.pkl")


@pytest.fixture(scope="session")
def df_row(ensemble):
    """Single-row DataFrame populated for every feature the model might ask for.

    Carries the union of FEATURE_COLUMNS + INTRADAY_FEATURE_COLUMNS so the
    production filter ``[c for c in FEATURE_COLUMNS if c in df_row.columns]``
    inside ``signals/generator.py::_shap_reasons`` resolves cleanly regardless
    of which feature set the loaded ensemble was trained on.
    """
    all_cols = sorted(set(FEATURE_COLUMNS) | set(INTRADAY_FEATURE_COLUMNS))
    row = {c: 0.5 for c in all_cols}
    row["close"] = 1000.0
    row["high"] = 1010.0
    row["low"] = 990.0
    row["open"] = 1000.0
    row["volume"] = 100000.0
    return pd.DataFrame([row])


# ---------- session reporting ----------

def pytest_sessionstart(session):
    """Print the live TreeExplainer RSS measurement at session start."""
    if not ENSEMBLE_PATH.exists():
        return
    try:
        e = Ensemble.load("ensemble_intraday.pkl")
        cost_mb = measure_explainer_rss_cost(e.signal_layer.model)
        msg = (
            f"\n[tests/stress/conftest.py] TreeExplainer RSS cost = "
            f"{cost_mb:.1f} MB per explainer "
            f"(8-worker projected = {cost_mb * 8:.0f} MB)."
        )
        print(msg, file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"\n[tests/stress/conftest.py] RSS measurement failed: {exc}",
              file=sys.stderr, flush=True)
