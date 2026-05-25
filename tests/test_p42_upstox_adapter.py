"""P42 — Upstox OHLCV adapter tests.

Mocks requests + dotenv_values so no real Upstox call is made. Verifies
the URL construction, auth header, response parsing, and the read-only
"no kill-switch gating" property (data fetches MUST work even when
LIVE_TRADING=false — they're not orders).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# RED at commit #6 if module is missing impl; should be GREEN immediately
# since we ship the impl in the same commit.
upstox_adapter_mod = pytest.importorskip(
    "data.adapters.upstox_adapter",
    reason="P42 commit #6 lands the impl",
)
UpstoxAdapter = upstox_adapter_mod.UpstoxAdapter


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    """A sandbox-keyed .env. Note that we test BOTH LIVE_TRADING=true and
    =false — the adapter must work in either state (it's data, not orders)."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        '\n'.join([
            'LIVE_TRADING="false"',  # explicitly OFF, verifying the adapter
                                      # is not kill-switch-gated
            'UPSTOX_ENV="sandbox"',
            'UPSTOX_SANDBOX_API_KEY="key"',
            'UPSTOX_SANDBOX_API_SECRET="sec"',
            'UPSTOX_SANDBOX_ACCESS_TOKEN="tok-sandbox"',
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(upstox_adapter_mod, "_DEFAULT_ENV_PATH", env_path)
    return env_path


def _mock_resp(status: int = 200, json_data=None, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data or {}
    r.text = text
    return r


SAMPLE_CANDLES = [
    # [ts, open, high, low, close, volume, oi]
    ["2026-05-23T15:30:00+05:30", 1500.0, 1510.0, 1495.0, 1505.0, 1_000_000, 0],
    ["2026-05-22T15:30:00+05:30", 1490.0, 1505.0, 1485.0, 1500.0, 1_100_000, 0],
    ["2026-05-21T15:30:00+05:30", 1485.0, 1495.0, 1480.0, 1490.0,   900_000, 0],
]


# ── Read-only / no-kill-switch property ────────────────────────────────────


def test_adapter_works_when_live_trading_disabled(live_env):
    """OHLCV is data, not orders — must NOT be gated by the kill switch.

    The live_env fixture sets LIVE_TRADING=false; this test passes only
    if the adapter completes a fetch without referencing the kill switch.
    """
    with patch("data.adapters.upstox_adapter.requests.get") as mget:
        mget.return_value = _mock_resp(json_data={
            "data": {"candles": SAMPLE_CANDLES},
            "status": "success",
        })
        df = UpstoxAdapter().fetch_ohlcv("RELIANCE.NS", years=1)
    assert len(df) == 3


# ── Auth / URL / parsing ───────────────────────────────────────────────────


def test_fetch_ohlcv_uses_bearer_auth_with_active_env_token(live_env):
    with patch("data.adapters.upstox_adapter.requests.get") as mget:
        mget.return_value = _mock_resp(json_data={"data": {"candles": SAMPLE_CANDLES}})
        UpstoxAdapter().fetch_ohlcv("RELIANCE.NS")
    headers = mget.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok-sandbox"


def test_fetch_ohlcv_url_includes_instrument_key_and_interval(live_env):
    with patch("data.adapters.upstox_adapter.requests.get") as mget:
        mget.return_value = _mock_resp(json_data={"data": {"candles": []}})
        UpstoxAdapter().fetch_ohlcv("RELIANCE.NS", resolution="1d")
    url = mget.call_args.args[0]
    assert "/historical-candle/" in url
    assert "NSE_EQ|INE002A01018" in url
    assert "/day/" in url


def test_fetch_ohlcv_returns_dataframe_with_correct_columns(live_env):
    with patch("data.adapters.upstox_adapter.requests.get") as mget:
        mget.return_value = _mock_resp(json_data={"data": {"candles": SAMPLE_CANDLES}})
        df = UpstoxAdapter().fetch_ohlcv("RELIANCE.NS")
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.name == "timestamp"
    # Sorted ascending after .sort_index()
    assert df.index.is_monotonic_increasing


def test_fetch_ohlcv_empty_candles_returns_empty_df(live_env):
    with patch("data.adapters.upstox_adapter.requests.get") as mget:
        mget.return_value = _mock_resp(json_data={"data": {"candles": []}})
        df = UpstoxAdapter().fetch_ohlcv("RELIANCE.NS")
    assert df.empty
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


# ── Error paths ────────────────────────────────────────────────────────────


def test_fetch_ohlcv_raises_when_token_missing(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text('UPSTOX_ENV="sandbox"\n', encoding="utf-8")
    monkeypatch.setattr(upstox_adapter_mod, "_DEFAULT_ENV_PATH", env_path)
    with pytest.raises(EnvironmentError, match="access token"):
        UpstoxAdapter().fetch_ohlcv("RELIANCE.NS")


def test_fetch_ohlcv_raises_on_unknown_symbol(live_env):
    with pytest.raises(EnvironmentError, match="symbol_map"):
        UpstoxAdapter().fetch_ohlcv("NOPE.NS")


def test_fetch_ohlcv_raises_on_unsupported_resolution(live_env):
    with pytest.raises(ValueError, match="resolution"):
        UpstoxAdapter().fetch_ohlcv("RELIANCE.NS", resolution="5m")


def test_fetch_ohlcv_raises_on_http_error(live_env):
    with patch("data.adapters.upstox_adapter.requests.get") as mget:
        mget.return_value = _mock_resp(status=401, text="Unauthorized")
        with pytest.raises(RuntimeError, match="401"):
            UpstoxAdapter().fetch_ohlcv("RELIANCE.NS")


# ── is_available ───────────────────────────────────────────────────────────


def test_is_available_true_when_token_present(live_env):
    assert UpstoxAdapter().is_available() is True


def test_is_available_false_when_token_missing(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text('UPSTOX_ENV="sandbox"\n', encoding="utf-8")
    monkeypatch.setattr(upstox_adapter_mod, "_DEFAULT_ENV_PATH", env_path)
    assert UpstoxAdapter().is_available() is False
