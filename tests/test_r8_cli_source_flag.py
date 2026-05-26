"""R8 — main.py --source flag + fetch_and_store routing.

The CLI gains an additive ``--source {yfinance,upstox}`` flag on the
existing ``fetch`` subcommand. Default = yfinance, so existing
operators see bit-for-bit identical behaviour. ``--source upstox``
opts into the new Upstox v2/v3 adapter path.

Routing flows downstream:
  cmd_fetch(args)
    -> fetch_and_store(symbol, years, resolution, source=args.source)
       -> _get_adapter(market, source=source)
          -> UpstoxAdapter() if source=="upstox" else YFinanceAdapter()

Tests in this file lock the wire-up so a future refactor cannot
silently make ``--source upstox`` no-op back to yfinance.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch
import argparse

import pytest


# ── CLI shape ────────────────────────────────────────────────────────


def _build_parser_isolated():
    """Return main.py's argparse parser via its existing ``build_parser``
    helper. main.py already exposes this — we just need to import + call."""
    import main
    return main.build_parser()


def test_fetch_parser_exposes_source_flag():
    """The fetch subcommand must accept --source with the documented
    choices."""
    parser = _build_parser_isolated()
    args = parser.parse_args(["fetch", "--source", "upstox",
                               "--symbol", "TCS.NS", "--years", "2"])
    assert args.source == "upstox"


def test_fetch_parser_defaults_source_to_yfinance():
    """Existing operators running `python main.py fetch` must see
    bit-for-bit identical behaviour: default source = yfinance."""
    parser = _build_parser_isolated()
    args = parser.parse_args(["fetch", "--symbol", "TCS.NS"])
    assert args.source == "yfinance"


def test_fetch_parser_rejects_unknown_source():
    """argparse must reject `--source polygon` (or any unrecognised
    value) — the choices list is the contract."""
    parser = _build_parser_isolated()
    with pytest.raises(SystemExit):
        parser.parse_args(["fetch", "--source", "polygon",
                            "--symbol", "TCS.NS"])


def test_fetch_parser_accepts_yfinance_explicit():
    """Setting --source yfinance explicitly is the same as omitting
    it. Both branches must work."""
    parser = _build_parser_isolated()
    args = parser.parse_args(["fetch", "--source", "yfinance",
                               "--symbol", "TCS.NS"])
    assert args.source == "yfinance"


# ── Routing through fetch_and_store ─────────────────────────────────


def test_fetch_and_store_accepts_source_kwarg():
    """fetch_and_store must accept a `source` keyword arg matching
    the CLI flag — that's the wire between argparse and the adapter
    selection. Default = None (which falls back to config.DATA_ADAPTER
    so the v2 day/week/month path stays bit-for-bit unchanged)."""
    import inspect
    from data.ingestion import fetch_and_store
    sig = inspect.signature(fetch_and_store)
    assert "source" in sig.parameters, (
        "fetch_and_store must accept a `source` kwarg for R8 routing")
    # Default must be None — preserves existing behaviour for any
    # in-process caller that doesn't pass source.
    assert sig.parameters["source"].default is None


def test_fetch_and_store_source_upstox_routes_to_upstox_adapter(monkeypatch):
    """source='upstox' must select the Upstox adapter regardless of
    what config.DATA_ADAPTER is set to. Verified by patching the
    adapter classes and asserting only UpstoxAdapter was instantiated."""
    import data.ingestion as ingest_mod

    yf_calls = {"n": 0}
    ux_calls = {"n": 0}

    class _FakeYFinance:
        def fetch_ohlcv(self, symbol, years=1, resolution="1d"):
            yf_calls["n"] += 1
            import pandas as pd
            return pd.DataFrame()

    class _FakeUpstox:
        def fetch_ohlcv(self, symbol, years=1, resolution="1d"):
            ux_calls["n"] += 1
            import pandas as pd
            return pd.DataFrame()

    monkeypatch.setattr(ingest_mod, "_get_adapter",
                         lambda market, source=None: (
                             _FakeUpstox() if source == "upstox"
                             else _FakeYFinance()))
    monkeypatch.setattr(ingest_mod, "validate_and_clean",
                         lambda raw, symbol: raw)
    monkeypatch.setattr(ingest_mod, "upsert_ohlcv",
                         lambda *a, **kw: None)

    # Use 1d resolution here to stay on the legacy fetch_ohlcv path.
    # 5m + upstox dispatches to get_historical_intraday — that's a
    # separate concern covered by test_fetch_and_store_5m_upstox_*.
    ingest_mod.fetch_and_store("TCS.NS", years=1, resolution="1d",
                                 source="upstox")
    assert ux_calls["n"] == 1
    assert yf_calls["n"] == 0


def test_fetch_and_store_source_yfinance_routes_to_yfinance(monkeypatch):
    """source='yfinance' must select yfinance even when an Upstox
    token would otherwise be picked up by config.DATA_ADAPTER. This
    is the operator escape hatch."""
    import data.ingestion as ingest_mod

    yf_calls = {"n": 0}
    ux_calls = {"n": 0}

    class _FakeYFinance:
        def fetch_ohlcv(self, symbol, years=1, resolution="1d"):
            yf_calls["n"] += 1
            import pandas as pd
            return pd.DataFrame()

    class _FakeUpstox:
        def fetch_ohlcv(self, symbol, years=1, resolution="1d"):
            ux_calls["n"] += 1
            import pandas as pd
            return pd.DataFrame()

    monkeypatch.setattr(ingest_mod, "_get_adapter",
                         lambda market, source=None: (
                             _FakeUpstox() if source == "upstox"
                             else _FakeYFinance()))
    monkeypatch.setattr(ingest_mod, "validate_and_clean",
                         lambda raw, symbol: raw)
    monkeypatch.setattr(ingest_mod, "upsert_ohlcv",
                         lambda *a, **kw: None)

    # 1d resolution — both adapters expose fetch_ohlcv for 1d.
    ingest_mod.fetch_and_store("TCS.NS", years=1, resolution="1d",
                                 source="yfinance")
    assert yf_calls["n"] == 1
    assert ux_calls["n"] == 0


# ── Intraday-resolution dispatch in the INGESTION layer ─────────────
#
# Why dispatch lives in data/ingestion.py, NOT in UpstoxAdapter.fetch_ohlcv:
# the P42 test test_fetch_ohlcv_raises_on_unsupported_resolution asserts
# that fetch_ohlcv("5m") raises ValueError. R8 must NOT regress that
# contract. Instead, fetch_and_store(...)`source="upstox", resolution="5m"`
# routes to UpstoxAdapter.get_historical_intraday at the ingestion layer.
# This keeps the adapter's v2 surface bit-for-bit unchanged and gives
# the R8 CLI flag a clean intraday path.


def test_fetch_and_store_5m_upstox_routes_to_get_historical_intraday(monkeypatch):
    """When source='upstox' AND resolution is intraday (5m), the
    ingestion layer must call adapter.get_historical_intraday(...),
    NOT adapter.fetch_ohlcv(...). This is the ONE-LINE wire that
    makes `main.py fetch --source upstox --resolution 5m` reach
    Upstox v3 instead of crashing on the v2 endpoint's ValueError."""
    import data.ingestion as ingest_mod

    intraday_calls = {"n": 0, "args": None}
    fetch_ohlcv_calls = {"n": 0}

    class _FakeUpstox:
        def get_historical_intraday(self, symbol, interval_minutes,
                                      from_date, to_date):
            intraday_calls["n"] += 1
            intraday_calls["args"] = {
                "symbol": symbol, "interval_minutes": interval_minutes,
                "from_date": from_date, "to_date": to_date,
            }
            import pandas as pd
            return pd.DataFrame(columns=["open", "high", "low", "close",
                                          "volume"])

        def fetch_ohlcv(self, symbol, years=1, resolution="1d"):
            fetch_ohlcv_calls["n"] += 1
            raise AssertionError(
                "fetch_ohlcv must NOT be called for intraday + upstox — "
                "the v2 path raises ValueError on 5m by design (P42 contract)")

    monkeypatch.setattr(ingest_mod, "_get_adapter",
                         lambda market, source=None: _FakeUpstox())
    monkeypatch.setattr(ingest_mod, "validate_and_clean",
                         lambda raw, symbol: raw)
    monkeypatch.setattr(ingest_mod, "upsert_ohlcv",
                         lambda *a, **kw: None)

    ingest_mod.fetch_and_store("TCS.NS", years=1, resolution="5m",
                                 source="upstox")
    assert intraday_calls["n"] == 1
    assert intraday_calls["args"]["interval_minutes"] == 5
    assert fetch_ohlcv_calls["n"] == 0


def test_fetch_and_store_1d_upstox_still_uses_v2_fetch_ohlcv(monkeypatch):
    """1d resolution with source=upstox must keep using the v2
    fetch_ohlcv path (unchanged from P42). Regression guard."""
    import data.ingestion as ingest_mod

    intraday_calls = {"n": 0}
    fetch_ohlcv_calls = {"n": 0}

    class _FakeUpstox:
        def get_historical_intraday(self, **kwargs):
            intraday_calls["n"] += 1
            import pandas as pd
            return pd.DataFrame()

        def fetch_ohlcv(self, symbol, years=1, resolution="1d"):
            fetch_ohlcv_calls["n"] += 1
            import pandas as pd
            return pd.DataFrame()

    monkeypatch.setattr(ingest_mod, "_get_adapter",
                         lambda market, source=None: _FakeUpstox())
    monkeypatch.setattr(ingest_mod, "validate_and_clean",
                         lambda raw, symbol: raw)
    monkeypatch.setattr(ingest_mod, "upsert_ohlcv",
                         lambda *a, **kw: None)

    ingest_mod.fetch_and_store("TCS.NS", years=1, resolution="1d",
                                 source="upstox")
    assert intraday_calls["n"] == 0, (
        "1d resolution must NOT go to v3 — it stays on v2 fetch_ohlcv.")
    assert fetch_ohlcv_calls["n"] == 1


def test_p42_contract_5m_still_raises_on_direct_adapter_call(monkeypatch):
    """The P42 contract (fetch_ohlcv raises ValueError on intraday
    resolution) must stay intact. R8 routes around it at the
    ingestion layer; it must NOT silently swap the adapter's
    fetch_ohlcv behaviour for intraday. Defends against a future
    refactor that 'helpfully' dispatches 5m inside fetch_ohlcv and
    breaks the P42 regression test."""
    from data.adapters import upstox_adapter as ua

    # Pretend a token is present so we get past the env check and
    # into the resolution check.
    monkeypatch.setattr(ua, "_active_access_token",
                         lambda *a, **k: "FAKE_TOKEN")

    a = ua.UpstoxAdapter()
    with pytest.raises(ValueError, match="resolution"):
        a.fetch_ohlcv("TCS.NS", years=1, resolution="5m")
