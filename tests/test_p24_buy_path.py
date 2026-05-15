"""Regression test for P24 — heap corruption on every BUY (Windows).

Reproduces the two races that caused ~6-second-after-BUY deaths with
``0xc0000374`` / ``0xc0000005`` exit codes (see ``logs/faulthandler.log``):

1. Windows SSL cert store loading is not thread-safe. Multiple worker
   threads calling ``urllib.request.urlopen`` concurrently each fire
   ``ssl._load_windows_store_certs``, racing on shared C state.
   The fix preloads a single ``ssl.SSLContext`` at module import time so
   workers reuse it instead of creating their own.

2. ``data.database.load_ohlcv`` was called concurrently from worker
   threads. Even with one-connection-per-call semantics, sharing the
   default ``check_same_thread=True`` connection plus a per-call WAL
   PRAGMA can leak C state across threads. The fix opens an isolated
   connection per call with ``check_same_thread=False``.

The tests fail on the parent commit because:
- ``alerts.telegram_bot._SSL_CONTEXT`` does not exist (ImportError).
- ``data.database.load_ohlcv`` under ThreadPoolExecutor crashes the
  interpreter (no Python exception — OS-level heap corruption).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import urllib.request
from concurrent.futures import ThreadPoolExecutor


def test_module_level_ssl_context_exists():
    """Fix A — the SSL context must be created at module import time, not per-call."""
    import ssl
    from alerts import telegram_bot
    assert hasattr(telegram_bot, "_SSL_CONTEXT"), (
        "P24 Fix A missing: alerts/telegram_bot.py must preload a module-level "
        "ssl.SSLContext so worker threads never race on _load_windows_store_certs."
    )
    assert isinstance(telegram_bot._SSL_CONTEXT, ssl.SSLContext)


def test_concurrent_telegram_sends_no_crash():
    """Reproduces P24 — N threads calling urlopen simultaneously must not crash."""
    from alerts.telegram_bot import _SSL_CONTEXT

    def _send(_):
        req = urllib.request.Request("https://api.telegram.org/")
        try:
            with urllib.request.urlopen(req, timeout=5, context=_SSL_CONTEXT) as resp:
                resp.read()
        except Exception:
            pass  # HTTP errors / no-network are fine — we only care about process survival

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_send, range(16)))
    # If we got here without OS-level death the SSL race is gone.


def test_concurrent_load_ohlcv_no_crash():
    """Reproduces P24's SQLite race — N threads loading data concurrently must not crash."""
    from data.database import load_ohlcv, list_tradeable_symbols
    # Use whatever symbols actually exist in the DB so the read paths run end-to-end.
    available = list_tradeable_symbols()
    pool = (available[:8] if available else ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS"]) * 4
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(load_ohlcv, pool))
    assert all(r is not None for r in results)
