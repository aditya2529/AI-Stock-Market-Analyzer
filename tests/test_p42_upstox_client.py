"""P42 — upstox_client unit tests (mock-based, no real API calls).

The defining safety property tested here: ``kill_switch.check()`` fires
BEFORE any HTTP request. If LIVE_TRADING is anything other than exactly
"true", every public function raises RuntimeError without touching the
network. Mocks for ``requests.get``/``requests.post`` get zero calls in
the disabled state.

Integration tests against real Upstox sandbox live in a separate file
(``test_p42_upstox_integration.py``, gated by env marker) so commit #3's
unit suite never hits the network.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

upstox_client = pytest.importorskip(
    "live_trading.upstox_client",
    reason="P42 commit #3 lands the impl",
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def live_env(tmp_path):
    """A .env that passes kill_switch.check (LIVE_TRADING=true, sandbox)."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        '\n'.join([
            'LIVE_TRADING="true"',
            'UPSTOX_ENV="sandbox"',
            'UPSTOX_SANDBOX_API_KEY="key"',
            'UPSTOX_SANDBOX_API_SECRET="sec"',
            'UPSTOX_SANDBOX_ACCESS_TOKEN="tok-sandbox"',
            'UPSTOX_PROD_API_KEY=""',
            'UPSTOX_PROD_API_SECRET=""',
            'UPSTOX_PROD_ACCESS_TOKEN=""',
        ]),
        encoding="utf-8",
    )
    upstox_client.reset_profile_cache()
    return str(env_path)


@pytest.fixture
def prod_env(tmp_path):
    """A .env that passes kill_switch.check with UPSTOX_ENV=prod."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        '\n'.join([
            'LIVE_TRADING="true"',
            'UPSTOX_ENV="prod"',
            'UPSTOX_PROD_API_KEY="prod-key"',
            'UPSTOX_PROD_API_SECRET="prod-sec"',
            'UPSTOX_PROD_ACCESS_TOKEN="tok-prod"',
            'UPSTOX_SANDBOX_API_KEY=""',
            'UPSTOX_SANDBOX_API_SECRET=""',
            'UPSTOX_SANDBOX_ACCESS_TOKEN=""',
        ]),
        encoding="utf-8",
    )
    upstox_client.reset_profile_cache()
    return str(env_path)


@pytest.fixture
def disabled_env(tmp_path):
    """A .env with LIVE_TRADING=false. Every API call must refuse."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        '\n'.join([
            'LIVE_TRADING="false"',
            'UPSTOX_ENV="sandbox"',
            'UPSTOX_SANDBOX_API_KEY="key"',
            'UPSTOX_SANDBOX_API_SECRET="sec"',
            'UPSTOX_SANDBOX_ACCESS_TOKEN="tok-sandbox"',
        ]),
        encoding="utf-8",
    )
    return str(env_path)


def _mock_resp(status: int = 200, json_data=None, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data or {}
    r.text = text
    return r


# ── Kill-switch fires first (the prime safety property) ─────────────────────

def test_kill_switch_blocks_get_profile_before_http(disabled_env):
    with patch("live_trading.upstox_client.requests.get") as mget:
        with pytest.raises(RuntimeError, match="LIVE_TRADING"):
            upstox_client.get_profile(env_path=disabled_env)
    assert mget.call_count == 0


def test_kill_switch_blocks_get_quote_before_http(disabled_env):
    with patch("live_trading.upstox_client.requests.get") as mget:
        with pytest.raises(RuntimeError, match="LIVE_TRADING"):
            upstox_client.get_quote_ltp("NSE_EQ|INE002A01018",
                                          env_path=disabled_env)
    assert mget.call_count == 0


def test_kill_switch_blocks_place_order_before_http(disabled_env):
    with patch("live_trading.upstox_client.requests.post") as mpost:
        with pytest.raises(RuntimeError, match="LIVE_TRADING"):
            upstox_client.place_order(
                instrument_key="NSE_EQ|INE002A01018",
                qty=1, env_path=disabled_env,
            )
    assert mpost.call_count == 0


def test_kill_switch_blocks_get_order_details_before_http(disabled_env):
    with patch("live_trading.upstox_client.requests.get") as mget:
        with pytest.raises(RuntimeError, match="LIVE_TRADING"):
            upstox_client.get_order_details("ORD-X", env_path=disabled_env)
    assert mget.call_count == 0


def test_kill_switch_blocks_cancel_before_http(disabled_env):
    with patch("live_trading.upstox_client.requests.delete") as mdel:
        with pytest.raises(RuntimeError, match="LIVE_TRADING"):
            upstox_client.cancel_order("ORD-X", env_path=disabled_env)
    assert mdel.call_count == 0


# ── Profile fetch + cache ───────────────────────────────────────────────────

def test_get_profile_hits_user_profile_endpoint(live_env):
    with patch("live_trading.upstox_client.requests.get") as mget:
        mget.return_value = _mock_resp(json_data={
            "data": {"user_id": "U-7BAZW5", "user_name": "Test User",
                     "email": "x@y.z", "products": ["D"]}
        })
        profile = upstox_client.get_profile(env_path=live_env)
    args, kwargs = mget.call_args
    assert args[0].endswith("/v2/user/profile")
    assert kwargs["headers"]["Authorization"] == "Bearer tok-sandbox"
    assert profile["user_id"] == "U-7BAZW5"
    assert profile["user_name"] == "Test User"


def test_get_profile_caches_session_scoped(live_env):
    with patch("live_trading.upstox_client.requests.get") as mget:
        mget.return_value = _mock_resp(json_data={
            "data": {"user_id": "U", "user_name": "X"}
        })
        upstox_client.get_profile(env_path=live_env)
        upstox_client.get_profile(env_path=live_env)
        upstox_client.get_profile(env_path=live_env)
    assert mget.call_count == 1


def test_reset_profile_cache_forces_refetch(live_env):
    with patch("live_trading.upstox_client.requests.get") as mget:
        mget.return_value = _mock_resp(json_data={
            "data": {"user_id": "U", "user_name": "X"}
        })
        upstox_client.get_profile(env_path=live_env)
        upstox_client.reset_profile_cache()
        upstox_client.get_profile(env_path=live_env)
    assert mget.call_count == 2


def test_get_profile_raises_on_401(live_env):
    with patch("live_trading.upstox_client.requests.get") as mget:
        mget.return_value = _mock_resp(status=401, text="token expired")
        with pytest.raises(upstox_client.UpstoxError, match="401"):
            upstox_client.get_profile(env_path=live_env)


# ── Env routing (sandbox vs prod creds) ─────────────────────────────────────

def test_prod_env_uses_prod_access_token(prod_env):
    with patch("live_trading.upstox_client.requests.get") as mget:
        mget.return_value = _mock_resp(json_data={
            "data": {"user_id": "U-prod", "user_name": "Prod User"}
        })
        upstox_client.get_profile(env_path=prod_env)
    assert mget.call_args.kwargs["headers"]["Authorization"] == "Bearer tok-prod"


# ── Market quote ────────────────────────────────────────────────────────────

def test_get_quote_ltp_returns_last_price(live_env):
    with patch("live_trading.upstox_client.requests.get") as mget:
        mget.return_value = _mock_resp(json_data={
            "data": {"NSE_EQ:RELIANCE": {"last_price": 1463.10}}
        })
        ltp = upstox_client.get_quote_ltp("NSE_EQ|INE002A01018",
                                            env_path=live_env)
    assert ltp == 1463.10
    args, kwargs = mget.call_args
    assert args[0].endswith("/v2/market-quote/ltp")
    assert kwargs["params"]["instrument_key"] == "NSE_EQ|INE002A01018"


def test_get_quote_ltp_raises_on_empty_data(live_env):
    with patch("live_trading.upstox_client.requests.get") as mget:
        mget.return_value = _mock_resp(json_data={"data": {}})
        with pytest.raises(upstox_client.UpstoxError, match="empty"):
            upstox_client.get_quote_ltp("NSE_EQ|INE002A01018",
                                          env_path=live_env)


# ── Place order ─────────────────────────────────────────────────────────────

def test_place_order_market_payload_shape(live_env):
    with patch("live_trading.upstox_client.requests.post") as mpost:
        mpost.return_value = _mock_resp(json_data={
            "data": {"order_id": "ORD-X"}, "status": "success"
        })
        resp = upstox_client.place_order(
            instrument_key="NSE_EQ|INE002A01018",
            qty=1, side="BUY", order_type="MARKET",
            env_path=live_env,
        )
    args, kwargs = mpost.call_args
    assert args[0].endswith("/v2/order/place")
    body = kwargs["json"]
    assert body["order_type"] == "MARKET"
    assert body["price"] == 0
    assert body["quantity"] == 1
    assert body["transaction_type"] == "BUY"
    assert body["instrument_token"] == "NSE_EQ|INE002A01018"
    assert body["tag"] == "p42-demo"
    assert body["product"] == "D"
    assert body["is_amo"] is False
    assert resp["data"]["order_id"] == "ORD-X"


def test_place_order_limit_uses_limit_price(live_env):
    with patch("live_trading.upstox_client.requests.post") as mpost:
        mpost.return_value = _mock_resp(json_data={
            "data": {"order_id": "ORD-L"}
        })
        upstox_client.place_order(
            instrument_key="NSE_EQ|INE002A01018",
            qty=1, side="BUY", order_type="LIMIT", limit_price=1450.0,
            env_path=live_env,
        )
    body = mpost.call_args.kwargs["json"]
    assert body["order_type"] == "LIMIT"
    assert body["price"] == 1450.0


def test_place_order_raises_on_4xx(live_env):
    with patch("live_trading.upstox_client.requests.post") as mpost:
        mpost.return_value = _mock_resp(status=400,
                                          text="insufficient margin")
        with pytest.raises(upstox_client.UpstoxError, match="400"):
            upstox_client.place_order(
                instrument_key="NSE_EQ|INE002A01018",
                qty=1, env_path=live_env,
            )


# ── Order details (for fill-status poll) ────────────────────────────────────

def test_get_order_details_returns_data_envelope(live_env):
    with patch("live_trading.upstox_client.requests.get") as mget:
        mget.return_value = _mock_resp(json_data={
            "data": {"order_id": "ORD-X", "status": "complete",
                       "average_fill_price": 1462.50, "filled_quantity": 1}
        })
        details = upstox_client.get_order_details("ORD-X", env_path=live_env)
    assert details["status"] == "complete"
    assert details["average_fill_price"] == 1462.50
    args, kwargs = mget.call_args
    assert args[0].endswith("/v2/order/details")
    assert kwargs["params"]["order_id"] == "ORD-X"
