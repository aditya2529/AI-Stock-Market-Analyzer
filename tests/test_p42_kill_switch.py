"""P42 — kill_switch + cap validators.

Tests are intentionally RED at commit #1 (live_trading/ doesn't exist yet).
Commit #2 ships the impl and these turn GREEN.

Contracts under test:
  - kill_switch.check() raises RuntimeError on:
      * LIVE_TRADING missing / empty / not "true"
      * UPSTOX_ENV missing / invalid (only "sandbox" | "prod" pass)
      * Active env's three credentials any-missing / empty
  - kill_switch.check() RE-READS .env on every call (the runtime kill
    requirement — mid-session flag flip must take effect immediately)
  - kill_switch.validate_notional(qty, price) raises if qty*price > 2000
  - kill_switch.validate_position_slot(open_count) raises if open_count >= 1
"""
from __future__ import annotations

import pytest

# RED at commit #1: live_trading/ does not exist yet, so importorskip marks
# every test in this file as "skipped — impl pending." Commit #2 lands the
# module and these auto-promote to active assertions.
kill_switch = pytest.importorskip(
    "live_trading.kill_switch",
    reason="P42 commit #2 lands the impl",
)


# ── Helpers ────────────────────────────────────────────────────────────────

# A complete "live ON, sandbox env, all creds set" .env dict — the baseline
# from which individual tests delete / mutate single keys to probe failure.
VALID_SANDBOX_ENV = {
    "LIVE_TRADING": "true",
    "UPSTOX_ENV": "sandbox",
    "UPSTOX_SANDBOX_API_KEY": "key-xxxx",
    "UPSTOX_SANDBOX_API_SECRET": "sec-xxxx",
    "UPSTOX_SANDBOX_ACCESS_TOKEN": "tok-xxxx",
    "UPSTOX_PROD_API_KEY": "",
    "UPSTOX_PROD_API_SECRET": "",
    "UPSTOX_PROD_ACCESS_TOKEN": "",
}

VALID_PROD_ENV = {
    "LIVE_TRADING": "true",
    "UPSTOX_ENV": "prod",
    "UPSTOX_PROD_API_KEY": "key-yyyy",
    "UPSTOX_PROD_API_SECRET": "sec-yyyy",
    "UPSTOX_PROD_ACCESS_TOKEN": "tok-yyyy",
    "UPSTOX_SANDBOX_API_KEY": "",
    "UPSTOX_SANDBOX_API_SECRET": "",
    "UPSTOX_SANDBOX_ACCESS_TOKEN": "",
}


def _write_env(tmp_path, mapping: dict) -> str:
    """Materialise a fake .env to disk; return its path as a string."""
    env_path = tmp_path / ".env"
    lines = [f"{k}={v}" for k, v in mapping.items()]
    env_path.write_text("\n".join(lines), encoding="utf-8")
    return str(env_path)


# ── LIVE_TRADING flag tests ────────────────────────────────────────────────

@pytest.mark.parametrize("flag_value", ["false", "False", "FALSE", "0", "no",
                                          "off", "disabled", "yes_kinda",
                                          "  true  ", "", "   "])
def test_check_raises_when_flag_not_exactly_true(tmp_path, flag_value):
    env = dict(VALID_SANDBOX_ENV, LIVE_TRADING=flag_value)
    path = _write_env(tmp_path, env)
    with pytest.raises(RuntimeError, match="LIVE_TRADING"):
        kill_switch.check(env_path=path)


def test_check_raises_when_flag_missing(tmp_path):
    env = dict(VALID_SANDBOX_ENV)
    env.pop("LIVE_TRADING")
    path = _write_env(tmp_path, env)
    with pytest.raises(RuntimeError, match="LIVE_TRADING"):
        kill_switch.check(env_path=path)


def test_check_passes_when_flag_true_sandbox(tmp_path):
    path = _write_env(tmp_path, VALID_SANDBOX_ENV)
    kill_switch.check(env_path=path)  # no raise


def test_check_passes_when_flag_true_prod(tmp_path):
    path = _write_env(tmp_path, VALID_PROD_ENV)
    kill_switch.check(env_path=path)  # no raise


# ── UPSTOX_ENV tests ───────────────────────────────────────────────────────

@pytest.mark.parametrize("env_value", ["", "live", "production", "test",
                                         "SANDBOX_TYPO", "  sandbox  "])
def test_check_raises_when_upstox_env_invalid(tmp_path, env_value):
    env = dict(VALID_SANDBOX_ENV, UPSTOX_ENV=env_value)
    path = _write_env(tmp_path, env)
    with pytest.raises(RuntimeError, match="UPSTOX_ENV"):
        kill_switch.check(env_path=path)


def test_check_raises_when_upstox_env_missing(tmp_path):
    env = dict(VALID_SANDBOX_ENV)
    env.pop("UPSTOX_ENV")
    path = _write_env(tmp_path, env)
    with pytest.raises(RuntimeError, match="UPSTOX_ENV"):
        kill_switch.check(env_path=path)


# ── Active-env credential validation ───────────────────────────────────────

@pytest.mark.parametrize("missing_key", ["UPSTOX_SANDBOX_API_KEY",
                                            "UPSTOX_SANDBOX_API_SECRET",
                                            "UPSTOX_SANDBOX_ACCESS_TOKEN"])
def test_check_raises_when_sandbox_active_but_a_cred_missing(tmp_path, missing_key):
    env = dict(VALID_SANDBOX_ENV)
    env[missing_key] = ""
    path = _write_env(tmp_path, env)
    with pytest.raises(RuntimeError, match=missing_key):
        kill_switch.check(env_path=path)


@pytest.mark.parametrize("missing_key", ["UPSTOX_PROD_API_KEY",
                                            "UPSTOX_PROD_API_SECRET",
                                            "UPSTOX_PROD_ACCESS_TOKEN"])
def test_check_raises_when_prod_active_but_a_cred_missing(tmp_path, missing_key):
    env = dict(VALID_PROD_ENV)
    env[missing_key] = ""
    path = _write_env(tmp_path, env)
    with pytest.raises(RuntimeError, match=missing_key):
        kill_switch.check(env_path=path)


def test_check_does_not_validate_inactive_env_creds(tmp_path):
    """Sandbox active → empty prod creds are fine (and vice versa)."""
    env = dict(VALID_SANDBOX_ENV)  # prod creds already empty in baseline
    path = _write_env(tmp_path, env)
    kill_switch.check(env_path=path)  # no raise


# ── Runtime re-read (load behavior) ────────────────────────────────────────

def test_check_rereads_env_on_every_call(tmp_path):
    """Critical: kill switch must use dotenv_values (per-call read),
    NOT dotenv.load_dotenv (one-shot at module import). A mid-session
    flag flip in .env must take effect on the very next check()."""
    path = _write_env(tmp_path, VALID_SANDBOX_ENV)
    kill_switch.check(env_path=path)  # initially passes

    # Flip flag off mid-session
    flipped = dict(VALID_SANDBOX_ENV, LIVE_TRADING="false")
    _write_env(tmp_path, flipped)
    with pytest.raises(RuntimeError, match="LIVE_TRADING"):
        kill_switch.check(env_path=path)


# ── Notional cap (₹2,000 hard limit) ───────────────────────────────────────

def test_validate_notional_rejects_2001():
    with pytest.raises(RuntimeError, match="notional"):
        kill_switch.validate_notional(qty=1, price=2001.0)


def test_validate_notional_accepts_2000_exact():
    kill_switch.validate_notional(qty=1, price=2000.0)  # no raise


def test_validate_notional_accepts_below():
    kill_switch.validate_notional(qty=1, price=1.0)
    kill_switch.validate_notional(qty=5, price=399.99)


def test_validate_notional_rejects_via_qty_x_price_product():
    """qty=3 × price=700 = 2100 — over the cap."""
    with pytest.raises(RuntimeError, match="notional"):
        kill_switch.validate_notional(qty=3, price=700.0)


@pytest.mark.parametrize("qty,price", [(0, 100.0), (-1, 100.0),
                                          (1, 0.0), (1, -5.0)])
def test_validate_notional_rejects_non_positive(qty, price):
    with pytest.raises(RuntimeError):
        kill_switch.validate_notional(qty=qty, price=price)


# ── Position-slot cap (MAX_LIVE_POSITIONS = 1) ─────────────────────────────

def test_validate_position_slot_accepts_zero_open():
    kill_switch.validate_position_slot(open_count=0)  # no raise


def test_validate_position_slot_rejects_one_open():
    with pytest.raises(RuntimeError, match="position"):
        kill_switch.validate_position_slot(open_count=1)


def test_validate_position_slot_rejects_two_open():
    with pytest.raises(RuntimeError, match="position"):
        kill_switch.validate_position_slot(open_count=2)


# ── Hard-coded cap constants (sanity) ──────────────────────────────────────

def test_max_live_notional_is_2000():
    assert kill_switch.MAX_LIVE_NOTIONAL == 2000


def test_max_live_positions_is_1():
    assert kill_switch.MAX_LIVE_POSITIONS == 1
