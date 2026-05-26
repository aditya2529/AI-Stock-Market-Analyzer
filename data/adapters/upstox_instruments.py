"""R8 — Upstox instrument master CSV cache + symbol -> instrument-key lookup.

The historical adapter needs to translate yfinance tickers (TCS.NS) to
Upstox instrument keys (NSE_EQ|INE467B01029) at scale (~200-symbol
universe). P42's static dict in ``live_trading.symbol_map`` only
covers 25 symbols and deliberately stays that way (deterministic
live-order path, ops-signed-off).

This module:
  1. Downloads Upstox's instrument master CSV (complete.csv.gz) the
     first time a lookup is requested.
  2. Caches it at ``data/cache/upstox_instruments.csv`` (CSV, not
     parquet — keeps requirements.txt unchanged: pandas alone reads
     CSV without pyarrow / fastparquet).
  3. Refreshes the cache if the file is older than 7 days (mtime
     based — ops asked for 7 days TTL exactly).
  4. On download failure with no usable cache, raises ``RuntimeError``
     with an actionable message. Does NOT fall back silently to
     yfinance, does NOT leave a half-written file masquerading as a
     valid cache.

Cache schema (the columns we use; the master has more we ignore):
    instrument_key   (str, e.g. "NSE_EQ|INE467B01029")
    exchange         (str, e.g. "NSE_EQ")
    instrument_type  (str, e.g. "EQUITY")
    trading_symbol   (str, e.g. "TCS")
    name             (str, e.g. "Tata Consultancy Services Limited")
    isin             (str, e.g. "INE467B01029")

Failure model — matches the pattern ``_active_access_token`` uses in
upstox_adapter.py: fail loudly at the call site so the caller knows
to set up the network / restore the cache / fall back manually.
"""
from __future__ import annotations

import gzip
import io
import logging
import time
import urllib.request
from pathlib import Path

import pandas as pd


logger = logging.getLogger(__name__)


# ── Cache config (constants — overridable in tests via monkeypatch) ──

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = _PROJECT_ROOT / "data" / "cache"
CACHE_FILE = CACHE_DIR / "upstox_instruments.csv"
CACHE_TTL_SECONDS = 7 * 24 * 3600   # 7 days — ops spec
MASTER_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
)
DOWNLOAD_TIMEOUT_SECONDS = 60

# Module-level memo so repeated lookups in the same process don't
# re-read the CSV from disk. Cleared via reset_cache() in tests.
_LOOKUP_MEMO: dict[str, str] = {}


# ── Internal helpers ─────────────────────────────────────────────────


def _cache_is_fresh() -> bool:
    """True iff CACHE_FILE exists, mtime is within TTL, AND the file's
    schema is parseable as the expected master format.

    The schema check defends against partial-write corruption — e.g.
    a download that wrote some bytes then raised mid-stream. Without
    it, a stub-shaped CSV with mtime in the last 7 days masquerades
    as a valid cache and every lookup raises ``RuntimeError`` on
    missing columns until the operator manually deletes the file.
    """
    if not CACHE_FILE.exists():
        return False
    age = time.time() - CACHE_FILE.stat().st_mtime
    if age > CACHE_TTL_SECONDS:
        return False
    # Cheap header-only parse — read first row to validate schema.
    try:
        head = pd.read_csv(CACHE_FILE, nrows=1, dtype=str)
    except Exception:
        return False
    required = {"instrument_key", "exchange", "trading_symbol"}
    return required.issubset(set(head.columns))


def _download_master(target: Path) -> None:
    """Download + decompress the Upstox master CSV into ``target``.

    Writes to a temp sibling first, then atomically renames into place.
    This prevents a half-written file from masquerading as a valid
    cache if the download is interrupted (network drop, disk full).
    Raises on any failure — the caller is responsible for surfacing
    the RuntimeError to the operator.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + ".partial")
    try:
        with urllib.request.urlopen(MASTER_URL,
                                      timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
            gz_bytes = resp.read()
        # Decompress in memory — the master is ~5 MB compressed,
        # ~25 MB uncompressed. Fits comfortably.
        with gzip.GzipFile(fileobj=io.BytesIO(gz_bytes)) as gz:
            csv_bytes = gz.read()
        tmp_path.write_bytes(csv_bytes)
        tmp_path.replace(target)
    except Exception:
        # Clean up the partial file so the next call retries fresh
        # instead of trusting a half-written cache.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _ensure_cache_fresh() -> None:
    """Ensure the cache file is present and within TTL. Refresh by
    re-downloading if needed. On any download failure, removes the
    partial file (already handled in _download_master) AND clears the
    memo, then raises RuntimeError with an actionable hint.
    """
    if _cache_is_fresh():
        return

    logger.info("Upstox instrument cache stale or missing — downloading "
                "%s", MASTER_URL)
    try:
        _download_master(CACHE_FILE)
    except Exception as exc:
        # Reset memo so a subsequent lookup tries again instead of
        # serving stale/empty results.
        _LOOKUP_MEMO.clear()
        raise RuntimeError(
            f"Upstox instrument master download failed ({type(exc).__name__}: "
            f"{exc}). No usable cache at {CACHE_FILE}. To unblock: confirm "
            f"network access to {MASTER_URL}, or place a valid CSV at that "
            f"path manually. Falling back to yfinance is NOT automatic — "
            f"caller must opt in to --source yfinance."
        ) from exc

    logger.info("Upstox instrument cache refreshed -> %s "
                "(%.1f MB)", CACHE_FILE,
                CACHE_FILE.stat().st_size / 1024 / 1024)


def _load_lookup_table() -> dict[str, str]:
    """Read CACHE_FILE and build the yfinance -> instrument-key map.

    yfinance NSE tickers are ``<symbol>.NS``. Upstox master rows have
    ``trading_symbol`` like ``TCS`` and ``exchange`` like ``NSE_EQ``.
    So the key derivation is ``f"{trading_symbol}.NS"`` for NSE_EQ
    rows. We also map BSE rows under ``.BO`` for completeness (rare
    in our universe today but cheap to include).

    Returns a fresh dict on each call — the caller is expected to
    populate _LOOKUP_MEMO from it once and reuse the memo for
    subsequent same-process lookups.
    """
    df = pd.read_csv(CACHE_FILE, dtype=str, low_memory=False)
    # Defensive: master CSV column names sometimes shift across
    # Upstox doc revisions. Only require the columns we use.
    required = {"instrument_key", "exchange", "trading_symbol"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(
            f"Upstox master CSV at {CACHE_FILE} is missing required "
            f"columns: {sorted(missing)}. Schema may have changed; "
            f"delete the file to force a fresh download next call."
        )

    mapping: dict[str, str] = {}
    nse_mask = df["exchange"].astype(str).str.upper().eq("NSE_EQ")
    for sym, key in zip(df.loc[nse_mask, "trading_symbol"].astype(str),
                          df.loc[nse_mask, "instrument_key"].astype(str)):
        mapping[f"{sym.upper()}.NS"] = key

    if "BSE_EQ" in df["exchange"].astype(str).str.upper().unique():
        bse_mask = df["exchange"].astype(str).str.upper().eq("BSE_EQ")
        for sym, key in zip(df.loc[bse_mask, "trading_symbol"].astype(str),
                              df.loc[bse_mask, "instrument_key"].astype(str)):
            # Don't overwrite an NSE entry of the same trading_symbol —
            # the engine universe is .NS-first.
            yf_key = f"{sym.upper()}.BO"
            if yf_key not in mapping:
                mapping[yf_key] = key
    return mapping


# ── Public surface ───────────────────────────────────────────────────


def lookup_instrument_key(symbol: str) -> str:
    """Translate a yfinance symbol to an Upstox instrument key.

    Args:
        symbol: yfinance-style ticker, e.g. ``"TCS.NS"`` or ``"RELIANCE.NS"``.
                Case-sensitive in the input.

    Returns:
        Upstox instrument key, e.g. ``"NSE_EQ|INE467B01029"``.

    Raises:
        KeyError: the symbol isn't in the Upstox master. Message
            embeds the symbol so it's easy to grep in tracebacks.
        RuntimeError: the cache is stale AND the re-download failed.
            Message includes the underlying error so the operator
            knows what to fix.
    """
    if not symbol:
        raise KeyError(f"empty symbol (got {symbol!r})")

    if symbol in _LOOKUP_MEMO:
        return _LOOKUP_MEMO[symbol]

    _ensure_cache_fresh()

    table = _load_lookup_table()
    _LOOKUP_MEMO.update(table)

    if symbol not in _LOOKUP_MEMO:
        raise KeyError(
            f"unknown symbol {symbol!r} in Upstox master (cached at "
            f"{CACHE_FILE}); verify the ticker or delete the cache to "
            f"force a fresh download next call."
        )
    return _LOOKUP_MEMO[symbol]


def reset_cache() -> None:
    """Clear the in-process memo. Mostly for tests; the on-disk cache
    is untouched."""
    _LOOKUP_MEMO.clear()
