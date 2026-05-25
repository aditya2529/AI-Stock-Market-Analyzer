"""P42 — Upstox v2 REST client.

Single base URL for sandbox AND prod (per ops addendum 2026-05-24): routing
to the sandbox simulator vs real exchange happens via which API key set
authenticated the call. UPSTOX_ENV in .env selects WHICH key set is loaded
inside the live_trading code; the URL is constant.

Every public function calls ``kill_switch.check()`` FIRST. If LIVE_TRADING
is anything other than exactly "true", the call raises ``RuntimeError``
BEFORE any HTTP request is issued. This is the defining safety property
of the live path — see ``tests/test_p42_upstox_client.py`` for the
"network sees zero calls when disabled" assertions.

Public surface:
    UpstoxError                              — any 4xx/5xx or empty envelope
    UPSTOX_BASE_URL                          — single base for sandbox + prod
    get_profile(env_path=None) -> dict       — user info, cached per session
    get_quote_ltp(key, env_path=None) -> float
    place_order(key, qty, side, order_type, limit_price=None, env_path=None) -> dict
    get_order_details(order_id, env_path=None) -> dict
    cancel_order(order_id, env_path=None) -> dict
    reset_profile_cache() -> None
"""
from __future__ import annotations

from pathlib import Path

import requests
from dotenv import dotenv_values

from live_trading import kill_switch


UPSTOX_BASE_URL = "https://api.upstox.com/v2"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"
_TIMEOUT_SECONDS = 15

# Session-scoped profile cache. Populated on first get_profile() call,
# cleared by reset_profile_cache() between env flips within the same
# Python process (rare; the runbook tells the operator to restart the
# shell when flipping prod/sandbox).
_PROFILE_CACHE: dict | None = None


class UpstoxError(Exception):
    """Any failure from the Upstox API — non-2xx status, empty data envelope,
    or malformed response. Raised so the order_manager can decide whether to
    surface the error to the operator + skip the live_trades.db row write."""


# ── Internal helpers ───────────────────────────────────────────────────────


def _resolve_env_path(env_path: str | None) -> str:
    return str(Path(env_path) if env_path else _DEFAULT_ENV_PATH)


def _load_active_creds(env_path: str | None = None) -> dict:
    """Return the active env's credentials.

    Caller has already passed ``kill_switch.check()`` so the values are
    guaranteed non-empty and ``UPSTOX_ENV`` is guaranteed sandbox|prod.
    """
    env = dotenv_values(_resolve_env_path(env_path))
    upstox_env = env["UPSTOX_ENV"]
    prefix = f"UPSTOX_{upstox_env.upper()}"
    return {
        "env": upstox_env,
        "api_key": env[f"{prefix}_API_KEY"],
        "api_secret": env[f"{prefix}_API_SECRET"],
        "access_token": env[f"{prefix}_ACCESS_TOKEN"],
    }


def _headers(env_path: str | None = None,
              content_type: str | None = None) -> dict:
    creds = _load_active_creds(env_path)
    h = {
        "Authorization": f"Bearer {creds['access_token']}",
        "Accept": "application/json",
    }
    if content_type:
        h["Content-Type"] = content_type
    return h


def _check_response(resp, what: str) -> dict:
    if resp.status_code >= 400:
        raise UpstoxError(
            f"{what} failed: HTTP {resp.status_code}: {resp.text[:500]}"
        )
    try:
        return resp.json()
    except Exception as exc:
        raise UpstoxError(f"{what} non-JSON response: {resp.text[:200]}") from exc


# ── Public API ─────────────────────────────────────────────────────────────


def reset_profile_cache() -> None:
    """Force the next ``get_profile()`` to re-fetch. Test hook + safety
    valve for env flips inside one Python process."""
    global _PROFILE_CACHE
    _PROFILE_CACHE = None


def get_profile(env_path: str | None = None) -> dict:
    """Fetch the active env's user profile, cached for the session.

    The CONFIRM prompt in the CLI displays ``user_name`` + ``user_id`` from
    this dict so the operator catches a paste-the-wrong-token mistake
    BEFORE typing CONFIRM. Caching is intentional: the profile is identity
    metadata that doesn't change mid-session, and a re-fetch on every
    preview is wasteful.
    """
    global _PROFILE_CACHE
    kill_switch.check(env_path)
    if _PROFILE_CACHE is None:
        resp = requests.get(
            f"{UPSTOX_BASE_URL}/user/profile",
            headers=_headers(env_path),
            timeout=_TIMEOUT_SECONDS,
        )
        data = _check_response(resp, "profile fetch").get("data", {})
        _PROFILE_CACHE = {
            "user_id": data.get("user_id"),
            "user_name": data.get("user_name"),
            "email": data.get("email"),
            "products": data.get("products", []),
        }
    return _PROFILE_CACHE


def get_quote_ltp(instrument_key: str,
                   env_path: str | None = None) -> float:
    """Return last traded price for one instrument key.

    Used by the order_manager pre-check: ``qty * get_quote_ltp(key) <= 2000``
    is the gate that runs before ``place_order``. The post-fill check then
    repeats the math against the actual ``average_fill_price`` from
    ``get_order_details`` to catch MARKET slippage past the cap.
    """
    kill_switch.check(env_path)
    resp = requests.get(
        f"{UPSTOX_BASE_URL}/market-quote/ltp",
        headers=_headers(env_path),
        params={"instrument_key": instrument_key},
        timeout=_TIMEOUT_SECONDS,
    )
    payload = _check_response(resp, "ltp fetch")
    data = payload.get("data") or {}
    if not data:
        raise UpstoxError(f"ltp empty response: {resp.text[:200]}")
    # Upstox returns {"data": {"NSE_EQ:RELIANCE": {"last_price": 1463.10}}}.
    # The response key format differs slightly from the request
    # instrument_key, so we read the first (and only) entry.
    first_entry = next(iter(data.values()))
    last_price = first_entry.get("last_price")
    if last_price is None:
        raise UpstoxError(f"ltp missing last_price: {first_entry}")
    return float(last_price)


def place_order(instrument_key: str,
                 qty: int,
                 side: str = "BUY",
                 order_type: str = "MARKET",
                 limit_price: float | None = None,
                 env_path: str | None = None) -> dict:
    """POST /v2/order/place.

    Caller (order_manager) is responsible for the ₹2,000 notional pre-check
    via ``kill_switch.validate_notional``; this function only enforces the
    kill switch + delegates to Upstox for the broker-side validations
    (margin, instrument tradability, market hours, etc.).

    ``product="D"`` (delivery) is hardcoded — project is long-only equity
    per CLAUDE.md and the brief's "no shorting" out-of-scope rule.
    """
    kill_switch.check(env_path)
    payload = {
        "quantity": int(qty),
        "product": "D",
        "validity": "DAY",
        "price": float(limit_price) if order_type == "LIMIT" else 0,
        "tag": "p42-demo",
        "instrument_token": instrument_key,
        "order_type": order_type,
        "transaction_type": side,
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
    }
    resp = requests.post(
        f"{UPSTOX_BASE_URL}/order/place",
        headers=_headers(env_path, content_type="application/json"),
        json=payload,
        timeout=_TIMEOUT_SECONDS,
    )
    return _check_response(resp, "order place")


def get_order_details(order_id: str,
                       env_path: str | None = None) -> dict:
    """GET /v2/order/details — used by the post-place 5s fill-status poll
    in order_manager. Returns the ``data`` envelope directly (caller doesn't
    care about the outer response wrapper)."""
    kill_switch.check(env_path)
    resp = requests.get(
        f"{UPSTOX_BASE_URL}/order/details",
        headers=_headers(env_path),
        params={"order_id": order_id},
        timeout=_TIMEOUT_SECONDS,
    )
    return _check_response(resp, "order details").get("data", {})


def cancel_order(order_id: str, env_path: str | None = None) -> dict:
    """DELETE /v2/order/cancel?order_id=... Used by ``live close`` if a
    pending LIMIT needs to be retracted before the square-off MARKET."""
    kill_switch.check(env_path)
    resp = requests.delete(
        f"{UPSTOX_BASE_URL}/order/cancel",
        headers=_headers(env_path, content_type="application/json"),
        params={"order_id": order_id},
        timeout=_TIMEOUT_SECONDS,
    )
    return _check_response(resp, "order cancel")
