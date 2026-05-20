"""P34 stress test — urllib SSL race on the telegram_bot send path.

Inherits the SSL-context + concurrent-urlopen tests from the old
``tests/test_p24_buy_path.py`` (deleted in this commit) and bumps to the
brief-mandated 8 workers / 200 iterations. The test deliberately does NOT
go through ``telegram_bot.send_message`` because that function requires a
valid bot token + chat id and short-circuits when neither is configured;
we exercise the underlying ``urllib.request.urlopen`` directly with the
module-preloaded ``_SSL_CONTEXT`` so the SSL race surface is hit
regardless of credential state.
"""
from __future__ import annotations
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor


WORKERS = 8
ITERATIONS = 200


def test_module_level_ssl_context_exists():
    """alerts.telegram_bot must preload a module-level SSLContext."""
    from alerts import telegram_bot
    assert hasattr(telegram_bot, "_SSL_CONTEXT"), (
        "alerts/telegram_bot.py must preload a module-level ssl.SSLContext "
        "so worker threads never race on _load_windows_store_certs."
    )
    assert isinstance(telegram_bot._SSL_CONTEXT, ssl.SSLContext)


def _hit_api_root(_):
    from alerts.telegram_bot import _SSL_CONTEXT
    req = urllib.request.Request("https://api.telegram.org/")
    try:
        with urllib.request.urlopen(req, timeout=5, context=_SSL_CONTEXT) as resp:
            resp.read()
    except Exception:
        pass  # HTTP errors / no-network are fine; we only care about process survival


def test_concurrent_telegram_urlopen_no_crash():
    """N threads hitting telegram api root concurrently must survive 200 calls."""
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(_hit_api_root, range(ITERATIONS)))
    # If we got here without OS-level death, the SSL race is gone.
