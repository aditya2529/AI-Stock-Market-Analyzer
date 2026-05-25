"""P42 kill switch + hard caps.

Single entry point ``check()`` validates that the live path is authorized
to run. Re-reads ``.env`` on EVERY call (via ``dotenv_values``, NOT
``load_dotenv``) so an operator can flip ``LIVE_TRADING=false`` mid-session
and the very next call refuses to fire. This is a deliberate runtime kill
behavior, not a one-shot startup check.

Hard caps are module-level constants by design — kept out of ``config.py``
so they aren't anyone's "let me bump this real quick" surface. Any change
requires a code edit + commit + review.

Contract surface (matches ``tests/test_p42_kill_switch.py``):
    check(env_path: str | None = None) -> None
    validate_notional(qty: int | float, price: float) -> None
    validate_position_slot(open_count: int) -> None
    MAX_LIVE_NOTIONAL = 2000
    MAX_LIVE_POSITIONS = 1
"""
from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

# Hard caps — see module docstring for the "not in config.py" rationale.
MAX_LIVE_NOTIONAL = 2000     # rupees, qty * price <= this
MAX_LIVE_POSITIONS = 1       # one trade open at a time

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"


def check(env_path: str | None = None) -> None:
    """Raise ``RuntimeError`` if the live path is not authorized to run.

    Validations, in order:
      1. ``LIVE_TRADING`` must be EXACTLY ``"true"`` (no whitespace, no
         capitalization variants). Anything else — including missing, empty,
         "True", "TRUE", "1", "yes" — is treated as off.
      2. ``UPSTOX_ENV`` must be exactly ``"sandbox"`` or ``"prod"``.
      3. The three credentials for the ACTIVE env (``UPSTOX_{ENV}_API_KEY``,
         ``UPSTOX_{ENV}_API_SECRET``, ``UPSTOX_{ENV}_ACCESS_TOKEN``) must all
         be present + non-empty (whitespace-only counts as empty for creds).

    Inactive-env credentials are NOT validated. Sandbox active with empty
    prod creds is fine; prod active with empty sandbox creds is fine.

    ``env_path`` lets tests inject a temp .env path. Production callers
    omit it and we read from the project-root ``.env``.
    """
    path = Path(env_path) if env_path else _DEFAULT_ENV_PATH
    env = dotenv_values(str(path))

    flag = env.get("LIVE_TRADING")
    if flag != "true":
        raise RuntimeError(
            f"LIVE_TRADING disabled (value={flag!r}); refusing live API call"
        )

    upstox_env = env.get("UPSTOX_ENV")
    if upstox_env not in ("sandbox", "prod"):
        raise RuntimeError(
            f"UPSTOX_ENV invalid: {upstox_env!r} (must be 'sandbox' or 'prod')"
        )

    prefix = f"UPSTOX_{upstox_env.upper()}"
    for cred_kind in ("API_KEY", "API_SECRET", "ACCESS_TOKEN"):
        key = f"{prefix}_{cred_kind}"
        val = env.get(key)
        if not val or not val.strip():
            raise RuntimeError(f"{key} missing or empty in .env")


def validate_notional(qty: int | float, price: float) -> None:
    """Raise if ``qty * price > MAX_LIVE_NOTIONAL`` or either input is non-positive.

    Used in the pre-order safety flow with the LTP fetched from
    ``upstox_client.get_quote``. Post-fill the order_manager runs the same
    check against the actual fill price and fires a loud Telegram alert if
    the real notional crossed the cap.
    """
    if qty is None or qty <= 0:
        raise RuntimeError(f"qty must be positive (got {qty!r})")
    if price is None or price <= 0:
        raise RuntimeError(f"price must be positive (got {price!r})")
    notional = qty * price
    if notional > MAX_LIVE_NOTIONAL:
        raise RuntimeError(
            f"notional {notional} > MAX_LIVE_NOTIONAL {MAX_LIVE_NOTIONAL} "
            f"(qty={qty}, price={price})"
        )


def validate_position_slot(open_count: int) -> None:
    """Raise if there's already a live position open.

    Caller queries ``live_portfolio.count_open_positions`` and passes the
    result here. Kept separate from the db layer so the policy decision
    (1 position max) lives next to the other hard caps.
    """
    if open_count >= MAX_LIVE_POSITIONS:
        raise RuntimeError(
            f"position slot full: {open_count} open >= "
            f"MAX_LIVE_POSITIONS {MAX_LIVE_POSITIONS}"
        )
