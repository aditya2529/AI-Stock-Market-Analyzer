"""R13 Stage 3 — sector-momentum check using stock baskets.

WHY STOCK BASKETS, NOT ^CNXIT / ^NSEBANK / ETC. yfinance subindices:
  yfinance returns clean live data for the 9 NSE sector subindices
  (probed Fri May 29 2026, all 9 OK with fresh 15:25 bars). They're
  the right source for LIVE deployment.

  But ENGINE-REPLAY runs against the historical 5m DB. The R8 Upstox
  backfill populated DEFAULT_SYMBOLS (25 stocks) for 2-year history,
  NOT the sector subindices. Backfilling the 9 subindices would add
  ~10 min of Upstox fetches + the Upstox v3 endpoint may not support
  index instruments at 5m granularity (untested).

  Stock-basket sector proxies use data already in the DB. Each
  sector's "momentum" = mean of pct-change since today's open
  across that sector's 4-5 most-liquid DEFAULT_SYMBOLS. Bullish
  iff mean > 0. Matches "only allow BUYs in sectors with
  change > 0%" semantics from the R13 brief.

  Ops pre-approved this fallback when subindex availability was
  flagged at Stage 3 prep.

LIVE DEPLOYMENT (later, NOT R13 scope): a parallel helper using
yfinance ^CNXIT / ^NSEBANK / etc. can drop in via the same
_is_sector_bullish(symbol, clock) signature. Engine-replay uses
this stock-basket version; live uses subindices.
"""
from __future__ import annotations

import logging
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)


# ── Sector membership map (25 DEFAULT_SYMBOLS grouped by NSE classification)
# Built from live_trading.symbol_map.ALL_MAPPINGS sector annotations and
# config.DEFAULT_SYMBOLS order. Each sector has 2-5 stocks.
SYMBOL_TO_SECTOR: dict[str, str] = {
    # IT (4)
    "TCS.NS": "IT",
    "INFY.NS": "IT",
    "WIPRO.NS": "IT",
    "HCLTECH.NS": "IT",
    # Banking (5)
    "HDFCBANK.NS": "BANK",
    "ICICIBANK.NS": "BANK",
    "KOTAKBANK.NS": "BANK",
    "AXISBANK.NS": "BANK",
    "SBIN.NS": "BANK",
    # Energy (3)
    "RELIANCE.NS": "ENERGY",
    "ONGC.NS": "ENERGY",
    "BPCL.NS": "ENERGY",
    # Auto (3)
    "MARUTI.NS": "AUTO",
    "M&M.NS": "AUTO",
    "BAJAJ-AUTO.NS": "AUTO",
    # Pharma (3)
    "SUNPHARMA.NS": "PHARMA",
    "DRREDDY.NS": "PHARMA",
    "CIPLA.NS": "PHARMA",
    # FMCG (3)
    "HINDUNILVR.NS": "FMCG",
    "NESTLEIND.NS": "FMCG",
    "BRITANNIA.NS": "FMCG",
    # Metals (2)
    "TATASTEEL.NS": "METAL",
    "HINDALCO.NS": "METAL",
    # Telecom (1) — alone, sector momentum = self momentum
    "BHARTIARTL.NS": "TELECOM",
    # Industrials (1) — alone
    "LT.NS": "INDUSTRIALS",
}

# Reverse: sector -> list of constituent symbols.
SECTOR_TO_SYMBOLS: dict[str, list[str]] = {}
for _sym, _sec in SYMBOL_TO_SECTOR.items():
    SECTOR_TO_SYMBOLS.setdefault(_sec, []).append(_sym)


class SectorMomentumFilter:
    """Stateful sector-momentum filter for engine-replay.

    Callable: ``filter(symbol, clock) -> bool``. The filter fetches
    the SAME-DAY opening bars for the symbol's sector basket (from
    ``raw_by_symbol`` injected at init) and computes the mean
    pct-change from sector-open to the given clock. Returns True if
    mean > threshold (default 0%).

    A small cache keyed by (sector, date, current_clock) avoids
    recomputing for every symbol in the same sector at the same tick.

    Implemented as a callable class (not a closure) so the harness can
    introspect ``.n_calls`` / ``.n_bullish`` / ``.n_bearish`` for
    post-run diagnostics.
    """

    def __call__(self, symbol: str, clock: pd.Timestamp) -> bool:
        """Same as ``is_bullish`` — lets the harness pass this object
        wherever a ``Callable[[str, Timestamp], bool]`` is expected."""
        return self.is_bullish(symbol, clock)

    def __init__(self, raw_by_symbol: dict[str, pd.DataFrame],
                  threshold_pct: float = 0.0):
        self.raw_by_symbol = raw_by_symbol
        self.threshold_pct = threshold_pct
        # Cache: (sector, date_str, clock_iso) -> bool
        self._cache: dict = {}
        # Stats for diagnostics
        self.n_calls = 0
        self.n_bullish = 0
        self.n_bearish = 0
        self.n_unknown_symbol = 0

    def is_bullish(self, symbol: str, clock: pd.Timestamp) -> bool:
        """True if the symbol's sector basket is bullish at ``clock``.
        Returns True for unknown symbols (no filter applied) so the
        caller doesn't silently block trades on unmapped tickers.
        """
        self.n_calls += 1
        sector = SYMBOL_TO_SECTOR.get(symbol)
        if sector is None:
            self.n_unknown_symbol += 1
            return True   # unmapped symbol — no filter

        date_str = str(clock.date())
        clock_iso = clock.isoformat()
        key = (sector, date_str, clock_iso)
        if key in self._cache:
            verdict = self._cache[key]
            if verdict:
                self.n_bullish += 1
            else:
                self.n_bearish += 1
            return verdict

        # Compute mean pct-change for the sector basket from today's
        # 09:15 open to current clock.
        constituents = SECTOR_TO_SYMBOLS.get(sector, [])
        pct_changes = []
        day = pd.Timestamp(date_str)
        day_start = day  # midnight of clock's date
        day_end = day + pd.Timedelta(days=1)
        for sym in constituents:
            raw = self.raw_by_symbol.get(sym)
            if raw is None or raw.empty:
                continue
            day_slice = raw.loc[(raw.index >= day_start)
                                  & (raw.index < day_end)
                                  & (raw.index <= clock)]
            if len(day_slice) < 2:
                # Not enough bars yet (e.g., first bar of the day)
                continue
            open_px = float(day_slice["open"].iloc[0])
            current_px = float(day_slice["close"].iloc[-1])
            if open_px > 0:
                pct_changes.append((current_px - open_px) / open_px * 100.0)

        if not pct_changes:
            # No data yet — be conservative, allow the trade (no filter)
            self._cache[key] = True
            self.n_bullish += 1
            return True

        mean_pct = sum(pct_changes) / len(pct_changes)
        verdict = mean_pct > self.threshold_pct
        self._cache[key] = verdict
        if verdict:
            self.n_bullish += 1
        else:
            self.n_bearish += 1
        return verdict


def make_sector_filter(raw_by_symbol: dict[str, pd.DataFrame],
                        threshold_pct: float = 0.0
                        ) -> SectorMomentumFilter:
    """Convenience factory: builds and returns a SectorMomentumFilter.

    The returned object is callable (`f(symbol, clock)`) AND exposes
    diagnostic counters (`.n_calls`, `.n_bullish`, `.n_bearish`).
    """
    return SectorMomentumFilter(raw_by_symbol, threshold_pct=threshold_pct)
