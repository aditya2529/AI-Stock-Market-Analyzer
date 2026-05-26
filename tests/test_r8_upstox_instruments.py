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
    """Drop a 6-row instruments CSV in the shape the real Upstox
    master uses (subset of the real columns — only the ones our
    loader cares about). Real column names:
        instrument_key, exchange_token, tradingsymbol, name,
        last_price, expiry, strike, tick_size, lot_size,
        instrument_type, option_type, exchange

    The fixture includes one bond row (instrument_type=BOND) and one
    F&O row to verify the loader's EQUITY filter excludes them — they
    must NOT shadow real equity tickers.
    """
    rows = [
        "instrument_key,exchange_token,tradingsymbol,name,last_price,"
        "expiry,strike,tick_size,lot_size,instrument_type,option_type,exchange",
        "NSE_EQ|INE467B01029,11536,TCS,Tata Consultancy Services Limited,"
        "0,,,0.05,1,EQUITY,,NSE_EQ",
        "NSE_EQ|INE009A01021,408,INFY,Infosys Limited,"
        "0,,,0.05,1,EQUITY,,NSE_EQ",
        "NSE_EQ|INE002A01018,2885,RELIANCE,Reliance Industries Limited,"
        "0,,,0.05,1,EQUITY,,NSE_EQ",
        "NSE_EQ|INE040A01034,1333,HDFCBANK,HDFC Bank Limited,"
        "0,,,0.05,1,EQUITY,,NSE_EQ",
        # Bond row — must NOT show up in lookup (instrument_type != EQUITY).
        "NSE_EQ|IN2920250163,758718,749RJ35,SDL RJ 7.49% 2035,"
        "0,,,0.01,100,BOND,,NSE_EQ",
        # F&O row — wrong exchange + wrong instrument_type, also excluded.
        "NSE_FO|49000,49000,NIFTY26AUG26000CE,NIFTY Aug 26000 CE,"
        "0,2026-08-28,26000,0.05,50,OPTIDX,CE,NSE_FO",
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


def test_lookup_filters_out_bonds_and_fno_rows(tmp_cache_dir, monkeypatch):
    """The Upstox NSE_EQ exchange code includes bonds + government
    securities (SDL series, IN2920... ISINs) in addition to equities.
    The loader MUST filter on ``instrument_type == 'EQUITY'`` so a
    bond's tradingsymbol (e.g. '749RJ35') doesn't shadow a real
    equity ticker or pollute the .NS lookup namespace.
    """
    _write_fake_master_csv(tmp_cache_dir / "upstox_instruments.csv")

    from data.adapters import upstox_instruments as ui
    monkeypatch.setattr(ui, "_download_master",
                         lambda p: pytest.fail("should not download"))

    # Bond should NOT be lookup-able under a .NS key (it's not equity).
    with pytest.raises(KeyError):
        ui.lookup_instrument_key("749RJ35.NS")

    # F&O row in NSE_FO exchange should NOT pollute .NS namespace either.
    with pytest.raises(KeyError):
        ui.lookup_instrument_key("NIFTY26AUG26000CE.NS")


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
