"""R8 — Upstox instrument-master cache + symbol lookup.

The historical adapter needs to translate yfinance symbols (TCS.NS) to
Upstox instrument keys (NSE_EQ|INE467B01029) at scale (~200 symbols).
P42's static dict in live_trading/symbol_map.py only covers 25 symbols
and is deliberately kept that way (deterministic live-order path).

This module fetches Upstox's complete.csv.gz instrument master once,
caches it locally at data/cache/upstox_instruments.csv, and serves
lookups from the cache. The cache is refreshed if older than 7 days.
On download failure with no cache available it raises RuntimeError
(no silent fallback to yfinance — matches the existing
_active_access_token() failure pattern in upstox_adapter.py).

Bisect-friendly: this commit lands RED — the module doesn't exist
yet. ImportError is the expected failure. Commit #2 turns it GREEN.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import time
from pathlib import Path

import pytest


@pytest.fixture
def tmp_cache_dir(tmp_path, monkeypatch):
    """Redirect the module's cache dir to a temp directory so the test
    can populate / inspect / time-travel the cache file without
    touching the real one in data/cache/."""
    import data.adapters.upstox_instruments as ui
    monkeypatch.setattr(ui, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ui, "CACHE_FILE", tmp_path / "upstox_instruments.csv")
    # Clear the module-level lookup memo so each test starts fresh.
    if hasattr(ui, "_LOOKUP_MEMO"):
        ui._LOOKUP_MEMO.clear()
    yield tmp_path


def _write_fake_master_csv(path: Path) -> None:
    """Drop a 4-row instruments CSV in the shape the Upstox master uses
    (subset of the real columns — only the ones our loader cares about).
    """
    rows = [
        "instrument_key,exchange,instrument_type,trading_symbol,name,isin",
        "NSE_EQ|INE467B01029,NSE_EQ,EQUITY,TCS,Tata Consultancy Services Limited,INE467B01029",
        "NSE_EQ|INE009A01021,NSE_EQ,EQUITY,INFY,Infosys Limited,INE009A01021",
        "NSE_EQ|INE002A01018,NSE_EQ,EQUITY,RELIANCE,Reliance Industries Limited,INE002A01018",
        "NSE_EQ|INE040A01034,NSE_EQ,EQUITY,HDFCBANK,HDFC Bank Limited,INE040A01034",
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_module_exposes_cache_constants():
    """The module must export CACHE_DIR + CACHE_FILE + TTL constants so
    callers + tests can monkey-patch them deterministically."""
    from data.adapters import upstox_instruments as ui
    assert hasattr(ui, "CACHE_DIR")
    assert hasattr(ui, "CACHE_FILE")
    assert hasattr(ui, "CACHE_TTL_SECONDS")
    # 7 days in seconds.
    assert ui.CACHE_TTL_SECONDS == 7 * 24 * 3600


def test_lookup_uses_cache_when_fresh(tmp_cache_dir, monkeypatch):
    """If a cache file exists AND is younger than CACHE_TTL_SECONDS, the
    loader must NOT make an HTTP request — pure cache hit."""
    _write_fake_master_csv(tmp_cache_dir / "upstox_instruments.csv")

    from data.adapters import upstox_instruments as ui

    download_was_called = {"yes": False}

    def _explode(*args, **kwargs):
        download_was_called["yes"] = True
        raise AssertionError("download_master must NOT be called on cache hit")

    monkeypatch.setattr(ui, "_download_master", _explode)
    key = ui.lookup_instrument_key("TCS.NS")
    assert key == "NSE_EQ|INE467B01029"
    assert not download_was_called["yes"]


def test_lookup_refetches_if_cache_older_than_7_days(tmp_cache_dir, monkeypatch):
    """A cache file with mtime > 7 days must trigger _download_master."""
    cache_file = tmp_cache_dir / "upstox_instruments.csv"
    _write_fake_master_csv(cache_file)
    # Set mtime to 8 days ago.
    eight_days_ago = time.time() - (8 * 24 * 3600)
    os.utime(cache_file, (eight_days_ago, eight_days_ago))

    from data.adapters import upstox_instruments as ui

    refetched = {"yes": False}

    def _fake_download(target: Path):
        refetched["yes"] = True
        _write_fake_master_csv(target)

    monkeypatch.setattr(ui, "_download_master", _fake_download)
    ui.lookup_instrument_key("TCS.NS")
    assert refetched["yes"], "stale cache (>7d) must trigger re-download"


def test_lookup_returns_known_nse_eq_format_for_default_symbols(tmp_cache_dir, monkeypatch):
    """Lookup must produce the same NSE_EQ|ISIN format that
    live_trading.symbol_map.lookup produces for the P42 25-symbol set.
    Validates schema compatibility between R8 dynamic lookup and the
    static P42 dict."""
    _write_fake_master_csv(tmp_cache_dir / "upstox_instruments.csv")

    from data.adapters import upstox_instruments as ui
    monkeypatch.setattr(ui, "_download_master",
                         lambda p: pytest.fail("should not download"))

    for ysym, expected in [
        ("TCS.NS", "NSE_EQ|INE467B01029"),
        ("INFY.NS", "NSE_EQ|INE009A01021"),
        ("RELIANCE.NS", "NSE_EQ|INE002A01018"),
        ("HDFCBANK.NS", "NSE_EQ|INE040A01034"),
    ]:
        assert ui.lookup_instrument_key(ysym) == expected


def test_lookup_unknown_symbol_raises_keyerror(tmp_cache_dir, monkeypatch):
    """Lookup for a symbol that's not in the master must raise KeyError
    with the symbol embedded — matches live_trading.symbol_map.lookup
    semantics for grep-friendly tracebacks."""
    _write_fake_master_csv(tmp_cache_dir / "upstox_instruments.csv")

    from data.adapters import upstox_instruments as ui
    monkeypatch.setattr(ui, "_download_master",
                         lambda p: pytest.fail("should not download"))

    with pytest.raises(KeyError, match="UNKNOWNSYMBOL.NS"):
        ui.lookup_instrument_key("UNKNOWNSYMBOL.NS")


def test_lookup_raises_runtimeerror_on_download_failure(tmp_cache_dir, monkeypatch):
    """When there's NO cache AND download fails, the loader must raise
    RuntimeError with an actionable message — NOT fall back silently
    to yfinance, NOT leave half-state in the cache file.
    """
    from data.adapters import upstox_instruments as ui

    def _fail_download(target: Path):
        raise ConnectionError("simulated DNS failure")

    monkeypatch.setattr(ui, "_download_master", _fail_download)

    with pytest.raises(RuntimeError, match="Upstox instrument master"):
        ui.lookup_instrument_key("TCS.NS")


def test_download_failure_does_not_leave_partial_cache(tmp_cache_dir, monkeypatch):
    """If the download writes a partial file and then fails, the loader
    must not leave that partial file masquerading as a valid cache.
    Verified via: trigger failure, then call lookup again — it must
    retry the download (not return stale partial data)."""
    cache_file = tmp_cache_dir / "upstox_instruments.csv"
    from data.adapters import upstox_instruments as ui

    call_count = {"n": 0}

    def _flaky_download(target: Path):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call simulates partial write then crash.
            target.write_text("instrument_key,exchange\n", encoding="utf-8")
            raise IOError("disk full, partial write")
        # Second call succeeds.
        _write_fake_master_csv(target)

    monkeypatch.setattr(ui, "_download_master", _flaky_download)

    with pytest.raises(RuntimeError):
        ui.lookup_instrument_key("TCS.NS")

    # Second invocation must retry, not trust the partial file.
    key = ui.lookup_instrument_key("TCS.NS")
    assert key == "NSE_EQ|INE467B01029"
    assert call_count["n"] == 2, (
        "loader trusted a partial cache instead of retrying download"
    )
