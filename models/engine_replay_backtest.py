"""R11 — engine-replay backtest harness.

Drives ``intraday.engine`` functions chronologically through
historical 5m bars to diagnose the backtest-vs-live PF gap (P49).

WHY THIS EXISTS
===============
The R9 shortcut harness reported v1 PF = 2.39 on the 2026-03-01 to
2026-05-26 holdout. Live engine lifetime PF was ~0.69 across 66
trades. Same model. Similar window. 3.5x gap.

The shortcut harness skips most engine-side gates (P28 daily caps,
slot caps, cooldowns, 14:00 cutoff, exposure caps) — it just runs
``ensemble.predict_with_confidence`` per bar and simulates trades on
the raw output. That's the prime suspect for the gap.

This file runs the EXACT live engine event loop, with all gates,
on the same holdout. The output PF tells us:

  replay PF ~ 0.69  ->  shortcut harness is wrong, gap is in the
                        gates we skipped
  replay PF ~ 2.39  ->  shortcut harness is right; gap is somewhere
                        else (slippage, latency, yfinance vs Upstox
                        data drift)
  somewhere in between -> multi-source gap

PRIME DIRECTIVE
===============
``intraday/engine.py`` is NOT modified by R11. The replay imports
the engine's functions and monkey-patches 8 named dependencies
that point outward (data fetch, system clock, alert dispatch).
Every gate, every cooldown, every sizing decision runs UNCHANGED.

THE 8 PATCH POINTS
==================
1. ``intraday.engine._fetch_intraday``     -> historical slice ending at replay clock
2. ``intraday.engine.engineer_features``    -> precomputed featured slice (5x perf)
3. ``intraday.engine._ist_now``             -> replay clock as tz-aware IST
4. ``intraday.engine._market_open``         -> always True during replay
5. ``intraday.engine._seconds_to_next_bar`` -> 0 (replay drives clock manually)
6. ``alerts.dispatcher.on_signal``          -> no-op
7. ``alerts.dispatcher.on_trade_closed``    -> no-op
8. ``intraday.engine.date``                 -> FakeDate.today() returns replay date,
                                                so cooldown keys (sl_cooldown_<date>,
                                                target_cooldown_<date>) align with
                                                the replayed bar's calendar day

Plus ``alerts.dispatcher.on_portfolio_snapshot`` and
``alerts.dispatcher.send_engine_pulse`` -> no-ops (defensive).

STATE SANDBOX
=============
Engine writes (paper_config, paper_positions, paper_trades) flow
through ``data.database.DB_PATH``. Caller redirects that to a temp
SQLite file before invoking ``run_replay`` — same pattern as the
P30 cooldown regression test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _real_date
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")


# ── Context ──────────────────────────────────────────────────────────


@dataclass
class ReplayContext:
    """Mutable state shared between the replay loop and the patched
    engine helpers. The loop sets current_symbol + current_clock before
    each ``_process_symbol`` call; the patched functions read them at
    call time to return the right historical slice / wall-clock."""
    current_symbol: Optional[str] = None
    current_clock: Optional[pd.Timestamp] = None  # tz-naive
    raw_by_symbol: dict = field(default_factory=dict)
    featured_by_symbol: dict = field(default_factory=dict)


# ── Feature pre-computation ─────────────────────────────────────────


def precompute_features(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Call engineer_features once per symbol on the full holdout
    window. Returned df is cached in ReplayContext.featured_by_symbol
    and sliced by clock during replay (vs per-tick recompute, which
    on 4,725 ticks × 25 symbols would dominate wall-clock)."""
    from features.engineer import engineer_features
    return engineer_features(raw_df)


# ── R12 block-reason instrumentation ─────────────────────────────────


class BlockReasonLogger:
    """Capture engine-side block actions during replay for post-hoc
    analysis. Each call to ``maybe_record`` inspects the
    ``_process_symbol`` return dict; if the ``_action`` is one of the
    known engine gates, a row is appended. Records are in-memory
    until ``flush`` writes them to CSV at run end.

    R12 use case: drives the `logs/r12_block_reasons.csv` artifact
    that informs R10 SL-widening + P45 adaptive-cooldown priorities.
    """

    # Mirrors the block-action strings _process_symbol returns + the
    # tick_counts dict keys in run_intraday_session. If a future engine
    # commit adds a new gate, the test
    # test_block_reason_logger_recognises_all_engine_gate_actions flags
    # the missing coverage.
    BLOCK_ACTIONS = frozenset({
        "regime_blocked",
        "conf_blocked",
        "cooldown",            # P30 SL cooldown
        "target_cooldown",     # R7-A target cooldown
        "max_pos",
        "time_cutoff",         # R7-B 14:00 IST
        "exposure_capped",     # P28
        "daily_loss_halt",     # P28
        "daily_count_capped",  # P28
    })

    CSV_HEADER = "symbol,timestamp,block_reason,signal,confidence,regime\n"

    def __init__(self) -> None:
        self.records: list[dict] = []

    def maybe_record(self, symbol: str, clock,
                       result, prediction_row=None) -> None:
        """If ``result`` looks like a block, append a row. Silently
        no-ops on None / non-dict / non-block returns."""
        if not isinstance(result, dict):
            return
        action = result.get("_action")
        if action not in self.BLOCK_ACTIONS:
            return
        rec = {
            "symbol": symbol,
            "timestamp": str(clock),
            "block_reason": action,
            "signal": None,
            "confidence": None,
            "regime": None,
        }
        if prediction_row is not None:
            try:
                rec["signal"] = prediction_row["signal"]
                rec["confidence"] = float(prediction_row["confidence"])
                rec["regime"] = prediction_row["regime"]
            except Exception:
                pass
        self.records.append(rec)

    def flush(self, path: Path) -> int:
        """Write records to CSV. Returns the row count (data rows
        only, not counting the header). Always writes the header
        even when records is empty, so downstream readers don't have
        to special-case missing files."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self.records:
            path.write_text(self.CSV_HEADER, encoding="utf-8")
            return 0
        df = pd.DataFrame(self.records)
        df.to_csv(path, index=False)
        return len(df)


# ── Patch builder ───────────────────────────────────────────────────


def _build_patches(ctx: ReplayContext) -> list[tuple]:
    """Return [(module, name, new_value), ...] for the 8+ patches.
    Centralised so both the pytest entry (apply_engine_patches) and
    the runtime entry (_install_engine_patches) build identical lists.
    """
    import intraday.engine as eng
    import alerts.dispatcher as ad
    import features.engineer as feat_mod

    # 1. _fetch_intraday — historical slice ending at ctx.current_clock
    def _patched_fetch_intraday(symbol: str):
        raw = ctx.raw_by_symbol.get(symbol)
        if raw is None or ctx.current_clock is None:
            return None
        slice_df = raw.loc[raw.index <= ctx.current_clock]
        # Match engine contract: returns None when not enough bars for
        # the 30-bar minimum _process_symbol enforces just below.
        return slice_df if len(slice_df) >= 30 else None

    # 2. engineer_features (engine namespace) — precomputed slice
    def _patched_engineer_features(df: pd.DataFrame):
        sym = ctx.current_symbol
        clock = ctx.current_clock
        if (sym is not None and sym in ctx.featured_by_symbol
                and clock is not None):
            feat = ctx.featured_by_symbol[sym]
            return feat.loc[feat.index <= clock]
        # Fallback for tests that don't set ctx — call the real one.
        from features.engineer import engineer_features as _real
        return _real(df)

    # 3. _ist_now — replay clock as tz-aware IST timestamp
    def _patched_ist_now():
        if ctx.current_clock is None:
            return datetime.now(IST)
        ts = ctx.current_clock
        if ts.tz is None:
            return ts.tz_localize(IST).to_pydatetime()
        return ts.to_pydatetime()

    # 4. _market_open — always True (we drive the clock)
    def _patched_market_open():
        return True

    # 5. _seconds_to_next_bar — 0 (no sleep)
    def _patched_seconds_to_next_bar():
        return 0

    # 6-7. alerts.dispatcher.* — no-ops
    def _noop(*args, **kwargs):
        return None

    # 8. date.today() in engine namespace — return replay date so
    #    cooldown keys (sl_cooldown_<date>, target_cooldown_<date>)
    #    track the replayed bar's calendar day.
    class _FakeDate:
        @classmethod
        def today(cls):
            if ctx.current_clock is not None:
                return ctx.current_clock.date()
            return _real_date.today()
        # Preserve fromisoformat / strftime for any defensive caller
        # that reaches for them on the date class itself.
        fromisoformat = staticmethod(_real_date.fromisoformat)

    # 9. datetime in engine namespace — _is_buy_cutoff_active calls
    #    `datetime.now(IST).hour`, NOT _ist_now(), so we have to
    #    patch the datetime class too. Without this, R7-B's 14:00
    #    cutoff would always check wall-clock-now instead of the
    #    replay clock.
    class _FakeDatetime:
        @classmethod
        def now(cls, tz=None):
            if ctx.current_clock is None:
                return datetime.now(tz)
            ts = ctx.current_clock
            if tz is not None:
                if ts.tz is None:
                    ts = ts.tz_localize(tz)
                else:
                    ts = ts.tz_convert(tz)
            return ts.to_pydatetime()
        fromisoformat = staticmethod(datetime.fromisoformat)
        fromtimestamp = staticmethod(datetime.fromtimestamp)
        # __sub__ / __add__ on the class itself aren't used by engine
        # code (those operate on instances) — instances flow through
        # to_pydatetime() above which is a real datetime, so arithmetic
        # works without further hooks.

    # NOTE: engineer_features is local-imported inside
    # intraday.engine._process_symbol (line 271):
    #     from features.engineer import engineer_features
    # So we patch at the SOURCE (features.engineer module) — that's
    # where the fresh import lookup resolves. Patching the engine
    # namespace would create an unused attribute.
    patches = [
        (eng, "_fetch_intraday", _patched_fetch_intraday),
        (feat_mod, "engineer_features", _patched_engineer_features),
        (eng, "_ist_now", _patched_ist_now),
        (eng, "_market_open", _patched_market_open),
        (eng, "_seconds_to_next_bar", _patched_seconds_to_next_bar),
        (eng, "date", _FakeDate),
        (eng, "datetime", _FakeDatetime),
        (ad, "on_signal", _noop),
        (ad, "on_trade_closed", _noop),
        (ad, "on_portfolio_snapshot", _noop),
    ]
    # send_engine_pulse only patched when present
    if hasattr(ad, "send_engine_pulse"):
        patches.append((ad, "send_engine_pulse", _noop))

    return patches


def apply_engine_patches(monkeypatch, ctx: ReplayContext) -> None:
    """Pytest-friendly patch installer using ``monkeypatch.setattr``
    so auto-cleanup runs at test teardown."""
    import intraday.engine as eng
    for module, name, new_val in _build_patches(ctx):
        monkeypatch.setattr(module, name, new_val)
    # Clear the engine's TTL fetch cache so a prior test's stale frame
    # doesn't leak into this run.
    eng._FETCH_CACHE.clear()


def _install_engine_patches(ctx: ReplayContext) -> list:
    """Runtime patch installer (no pytest). Returns a list of
    (module, name, original) tuples that ``_restore_engine_patches``
    consumes to unwind the patches in finally blocks."""
    import intraday.engine as eng
    originals = []
    for module, name, new_val in _build_patches(ctx):
        originals.append((module, name, getattr(module, name)))
        setattr(module, name, new_val)
    eng._FETCH_CACHE.clear()
    return originals


def _restore_engine_patches(originals: list) -> None:
    for module, name, original in originals:
        setattr(module, name, original)


# ── Main driver ─────────────────────────────────────────────────────


def run_replay(
    symbols: list[str],
    holdout_start: pd.Timestamp,
    holdout_end: pd.Timestamp,
    ensemble_path: Path,
    sandbox_db_path: Path,
    portfolio_value: float = 500_000.0,
    progress_every: int = 100,
    block_log_path: Path | None = None,
    conf_floor_override: float | None = None,
    sector_filter=None,
) -> dict:
    """Replay the engine across ``holdout_start -> holdout_end`` 5m
    bars for the given ``symbols``. Returns a metrics dict matching
    the R9 shortcut harness shape so the comparison report can diff
    them directly.

    Args:
        symbols: NSE tickers (yfinance format, e.g. ``"RELIANCE.NS"``)
        holdout_start: inclusive start of the replay window (tz-naive)
        holdout_end: inclusive end of the replay window (tz-naive)
        ensemble_path: absolute path to ``ensemble_intraday.pkl``
            (or v2 .pkl for a different model). Engine code consumes
            via the standard ``Ensemble.load(name=...)`` API.
        sandbox_db_path: absolute path to a tmp SQLite file the
            engine's paper_trading writes flow into.
        portfolio_value: starting cash in the sandbox; defaults to
            Rs 500K (matches the May 26 cutover reset).
        progress_every: print a one-line progress update every N
            timeline ticks (default 100; ~every ~500 sec of replayed
            wall-clock for 5m bars).

    Returns:
        {
          "metrics": {profit_factor, sharpe, win_rate, max_drawdown, cagr, n_trades},
          "per_symbol": {sym: {n_trades, pf, win_rate}, ...},
          "n_ticks": int,
          "n_symbol_evaluations": int,
          "wall_clock_secs": float,
          "sandbox_db_path": str,
        }
    """
    import time

    import data.database as db_mod
    import intraday.engine as eng
    from data.database import load_ohlcv, init_db
    from models.ensemble import Ensemble
    from paper_trading.portfolio import (
        init_paper_tables, set_cash, set_config,
    )
    from backtesting.metrics import compute_all

    # ── 1. Load OHLCV from the REAL market_data.db BEFORE redirect ───
    # Critical ordering: load_ohlcv reads through db_mod.DB_PATH; if we
    # redirected to the empty sandbox first, load_ohlcv would return
    # empty frames for every symbol and the replay would have nothing
    # to drive.
    ctx = ReplayContext()
    skipped = []
    for sym in symbols:
        raw_full = load_ohlcv(sym, resolution="5m")
        if raw_full.empty:
            skipped.append((sym, "no 5m data in DB"))
            continue
        # Engineer features on FULL history first so rolling windows
        # are warm; then slice. Matches the R9 shortcut harness pattern.
        try:
            featured_full = precompute_features(raw_full)
        except Exception as e:
            skipped.append((sym, f"engineer_features failed: {e}"))
            continue
        raw_slice = raw_full.loc[
            (raw_full.index >= holdout_start) & (raw_full.index <= holdout_end)
        ]
        feat_slice = featured_full.loc[
            (featured_full.index >= holdout_start) & (featured_full.index <= holdout_end)
        ]
        if len(raw_slice) < 30 or len(feat_slice) < 30:
            skipped.append((sym, f"thin slice raw={len(raw_slice)} feat={len(feat_slice)}"))
            continue
        # Stash FULL raw + FULL featured so the engine sees enough
        # warm-up history when slicing by clock.
        ctx.raw_by_symbol[sym] = raw_full
        ctx.featured_by_symbol[sym] = featured_full
    if not ctx.raw_by_symbol:
        raise RuntimeError(
            "no symbols had usable 5m data in the holdout window — "
            "check market_data.db population")

    # ── 2. NOW redirect DB to sandbox + bootstrap paper-trading state
    db_mod.DB_PATH = str(sandbox_db_path)
    init_db()
    init_paper_tables()
    set_cash(portfolio_value)
    set_config("peak_value", portfolio_value)
    set_config("initial_cash", portfolio_value)
    set_config("nse_cash", portfolio_value)
    set_config("nse_initial_cash", portfolio_value)
    set_config("nse_peak_value", portfolio_value)

    # ── 3. Build chronological timeline from holdout slice only ──────
    timeline = set()
    for sym, raw in ctx.raw_by_symbol.items():
        in_window = raw.loc[(raw.index >= holdout_start) & (raw.index <= holdout_end)]
        timeline.update(in_window.index)
    timeline = sorted(timeline)
    print(f"[replay] timeline: {len(timeline)} 5m bars across "
          f"{len(ctx.raw_by_symbol)} symbols, "
          f"{holdout_start.date()} -> {holdout_end.date()}")
    if skipped:
        print(f"[replay] skipped {len(skipped)} symbols:")
        for s, r in skipped[:5]:
            print(f"  {s}: {r}")

    # ── 4. Load the ensemble ─────────────────────────────────────────
    print(f"[replay] loading ensemble: {ensemble_path}")
    import pickle
    with open(ensemble_path, "rb") as f:
        ensemble = pickle.load(f)
    # Sanity: confirm predict_with_confidence is callable
    if not callable(getattr(ensemble, "predict_with_confidence", None)):
        raise RuntimeError(
            f"ensemble at {ensemble_path} is missing predict_with_confidence — "
            f"is this a valid Ensemble pickle?")

    # ── 4b. Precompute predictions per symbol (perf optimisation) ────
    # The engine calls ensemble.predict_with_confidence(featured) on a
    # sliced featured df every tick. Per-call cost (~1s in the smoke
    # run) dominates wall-clock: 25 symbols × 4725 ticks × 1s = ~34 hr
    # without this optimisation.
    #
    # LSTM inference is causal — prediction at bar T uses only bars
    # <= T. So pre-computing once on the FULL featured df, then
    # slicing on demand, is mathematically equivalent to per-bar
    # re-prediction. Per-eval cost drops to a Series .loc lookup
    # (~10 us) → full holdout ~10-20 min.
    print(f"[replay] precomputing predictions per symbol …")
    import time
    precompute_start = time.time()
    predictions_by_symbol = {}
    for sym, feat_full in ctx.featured_by_symbol.items():
        predictions_by_symbol[sym] = ensemble.predict_with_confidence(feat_full)
    print(f"[replay] precompute done in "
          f"{time.time() - precompute_start:.1f}s "
          f"({len(predictions_by_symbol)} symbols)")

    # Sanity check causality on ONE symbol/timestamp — confirms the
    # precompute is mathematically equivalent to per-bar inference.
    # If this fails the precompute is unsafe and we fall back.
    causal_safe = True
    try:
        sample_sym = next(iter(predictions_by_symbol))
        sample_feat = ctx.featured_by_symbol[sample_sym]
        # Pick a bar ~80% into the precomputed range (avoids warm-up
        # edge but stays inside the data) — first cast to a position.
        mid_pos = int(len(sample_feat) * 0.8)
        mid_ts = sample_feat.index[mid_pos]
        sliced = sample_feat.loc[sample_feat.index <= mid_ts]
        per_bar = ensemble.predict_with_confidence(sliced).iloc[-1]
        from_precomp = predictions_by_symbol[sample_sym].loc[mid_ts]
        if (per_bar["signal"] != from_precomp["signal"]
                or abs(per_bar["confidence"] - from_precomp["confidence"]) > 1e-6):
            causal_safe = False
            print(f"  [replay] WARN: causality check failed at "
                  f"{sample_sym}@{mid_ts}; precompute will be bypassed.")
            print(f"    per-bar:    sig={per_bar['signal']} conf={per_bar['confidence']:.4f}")
            print(f"    precompute: sig={from_precomp['signal']} conf={from_precomp['confidence']:.4f}")
    except Exception as e:
        causal_safe = False
        print(f"  [replay] WARN: causality check raised {e!r}; bypassing precompute.")

    # Patch ensemble.predict_with_confidence on this instance only.
    # Restored in the finally block below.
    import types
    _original_predict = ensemble.predict_with_confidence
    if causal_safe:
        def _patched_predict(self, df):
            sym = ctx.current_symbol
            clock = ctx.current_clock
            if (sym is None or sym not in predictions_by_symbol
                    or clock is None):
                return _original_predict(df)
            pred_full = predictions_by_symbol[sym]
            return pred_full.loc[pred_full.index <= clock]
        ensemble.predict_with_confidence = types.MethodType(_patched_predict, ensemble)

    # ── 5. Install patches manually + run replay loop ────────────────
    # R12 — optional block-reason logger. None when block_log_path is
    # not set, so existing callers see zero behaviour change.
    block_logger = BlockReasonLogger() if block_log_path is not None else None

    # R13 Stage 2 — conf-floor override. Engine's _process_symbol reads
    # SIGNAL_MIN_CONFIDENCE from os.getenv on every tick, so setting
    # the env var once at the start of the replay is sufficient. We
    # save the prior value and restore it in the finally block so
    # this run doesn't leak the override into other replays / tests.
    import os as _os
    _saved_conf_floor_env = _os.environ.get("SIGNAL_MIN_CONFIDENCE")
    if conf_floor_override is not None:
        _os.environ["SIGNAL_MIN_CONFIDENCE"] = f"{float(conf_floor_override):.4f}"
        print(f"[replay] conf-floor override active: "
              f"SIGNAL_MIN_CONFIDENCE={_os.environ['SIGNAL_MIN_CONFIDENCE']}")

    # R13 Stage 3 — sector-momentum filter. Monkey-patches
    # paper_trading.executor.try_open so BUYs in bearish sectors get
    # silently refused (return None). Engine's existing "if opened: …"
    # branch handles None as "open failed, move on" cleanly. Tracked
    # in the harness for post-run diagnostics; NOT routed through the
    # block-reason logger (those are engine-side gates; this is an
    # executor-side filter).
    _saved_try_open = None
    n_sector_refused = 0
    if sector_filter is not None:
        from paper_trading import executor as _exec_mod
        _saved_try_open = _exec_mod.try_open

        def _patched_try_open(symbol, signal_row, next_open, portfolio_value):
            nonlocal n_sector_refused
            try:
                ok = sector_filter(symbol, ctx.current_clock)
            except Exception as e:
                # Sector filter errors must NOT crash the replay —
                # fall through to original behaviour.
                print(f"  [replay] sector filter error for {symbol}: {e}")
                ok = True
            if not ok:
                n_sector_refused += 1
                return None
            return _saved_try_open(symbol, signal_row, next_open, portfolio_value)

        _exec_mod.try_open = _patched_try_open
        print(f"[replay] sector_filter active "
              f"({type(sector_filter).__name__})")

    wall_start = time.time()
    originals = _install_engine_patches(ctx)
    n_eval = 0
    force_closed_dates = set()
    try:
        # Initial cooldown rehydrate (will be empty since sandbox is fresh)
        eng._sl_cooldown.clear()
        eng._target_cooldown.clear()

        prev_date = None
        for i, t in enumerate(timeline):
            ctx.current_clock = t

            # Day rollover: rehydrate cooldowns from sandbox DB so the
            # P30 / T2.5W1-A "survives restart" semantics work in replay.
            current_date = t.date()
            if prev_date is not None and current_date != prev_date:
                eng._sl_cooldown.clear()
                eng._target_cooldown.clear()
                eng._sl_cooldown.update(eng._load_sl_cooldown_for_today())
                eng._target_cooldown.update(eng._load_target_cooldown_for_today())
            prev_date = current_date

            # Process each symbol whose data has a bar at THIS timestamp
            for sym in ctx.raw_by_symbol.keys():
                raw = ctx.raw_by_symbol[sym]
                if t not in raw.index:
                    continue
                ctx.current_symbol = sym
                try:
                    result = eng._process_symbol(
                        sym, ensemble, portfolio_value=portfolio_value)
                    n_eval += 1
                    # R12: capture block reasons. Pull the prediction
                    # row for this (sym, clock) so the CSV carries
                    # confidence + regime for each blocked decision.
                    if block_logger is not None:
                        pred_row = None
                        if causal_safe and sym in predictions_by_symbol:
                            try:
                                pred_row = predictions_by_symbol[sym].loc[t]
                            except KeyError:
                                pred_row = None
                        block_logger.maybe_record(sym, t, result, pred_row)
                except Exception as e:
                    print(f"  [replay] WARN {sym}@{t}: {type(e).__name__}: {e}")

            # 15:15 IST force-close — once per session day, on the
            # first tick at or past 15:15 for that date.
            if (t.time() >= pd.Timestamp("15:15").time()
                    and current_date not in force_closed_dates):
                try:
                    eng._force_close_all()
                except Exception as e:
                    print(f"  [replay] WARN force_close@{current_date}: {e}")
                force_closed_dates.add(current_date)

            if (i + 1) % progress_every == 0:
                elapsed = time.time() - wall_start
                print(f"  [replay] tick {i + 1}/{len(timeline)} "
                      f"({100*(i+1)/len(timeline):.1f}%) "
                      f"elapsed {elapsed:.0f}s, "
                      f"symbol-evals {n_eval}")
    finally:
        _restore_engine_patches(originals)
        # Restore the ensemble's original predict method (we patched
        # it on the instance, not the class).
        try:
            ensemble.predict_with_confidence = _original_predict
        except Exception:
            pass
        # R13 — restore conf-floor env var
        if conf_floor_override is not None:
            if _saved_conf_floor_env is None:
                _os.environ.pop("SIGNAL_MIN_CONFIDENCE", None)
            else:
                _os.environ["SIGNAL_MIN_CONFIDENCE"] = _saved_conf_floor_env
        # R13 — restore try_open
        if sector_filter is not None and _saved_try_open is not None:
            from paper_trading import executor as _exec_mod
            _exec_mod.try_open = _saved_try_open

    wall_clock_secs = time.time() - wall_start
    print(f"[replay] complete: {len(timeline)} ticks, {n_eval} "
          f"symbol-evals, {wall_clock_secs:.0f}s wall-clock")

    # ── 5b. Flush block-reason log if requested (R12) ────────────────
    n_blocks_logged = 0
    if block_logger is not None and block_log_path is not None:
        n_blocks_logged = block_logger.flush(Path(block_log_path))
        print(f"[replay] block-reason log: {n_blocks_logged} rows -> "
              f"{block_log_path}")

    # ── 6. Compute metrics from sandbox paper_trades ─────────────────
    import sqlite3
    con = sqlite3.connect(str(sandbox_db_path))
    trades_df = pd.read_sql_query(
        "SELECT * FROM paper_trades WHERE exit_time IS NOT NULL "
        "ORDER BY exit_time",
        con,
    )
    con.close()
    print(f"[replay] closed trades in sandbox DB: {len(trades_df)}")

    # Map paper_trades schema (net_pnl, return_pct) onto compute_all's
    # expected shape (pnl, return). net_pnl is the AFTER-fees P&L —
    # matches what the engine writes for the live engine. We rename
    # rather than copy to keep the DataFrame slim.
    if not trades_df.empty:
        trades_df = trades_df.rename(columns={
            "net_pnl": "pnl",
            "return_pct": "return",
        })
        # Equity curve from cumulative trade returns
        eq = (1.0 + trades_df["return"]).cumprod()
        eq = pd.concat([pd.Series([1.0]), eq], ignore_index=True)
        metrics = compute_all(trades_df, eq)
    else:
        metrics = {
            "profit_factor": float("nan"),
            "sharpe": 0.0,
            "win_rate": 0.0,
            "max_drawdown": 0.0,
            "cagr": 0.0,
            "n_trades": 0,
        }

    # Per-symbol breakdown
    per_symbol = {}
    if not trades_df.empty:
        for sym, group in trades_df.groupby("symbol"):
            pos = group.loc[group["pnl"] > 0, "pnl"].sum()
            neg = group.loc[group["pnl"] < 0, "pnl"].abs().sum()
            pf = float("inf") if neg == 0 else pos / neg
            per_symbol[sym] = {
                "n_trades": int(len(group)),
                "pf": pf,
                "win_rate": float((group["pnl"] > 0).mean()),
                "total_pnl": float(group["pnl"].sum()),
            }

    return {
        "metrics": metrics,
        "per_symbol": per_symbol,
        "n_ticks": len(timeline),
        "n_symbol_evaluations": n_eval,
        "wall_clock_secs": wall_clock_secs,
        "sandbox_db_path": str(sandbox_db_path),
        "holdout_start": str(holdout_start.date()),
        "holdout_end": str(holdout_end.date()),
        "ensemble_path": str(ensemble_path),
        "skipped_symbols": skipped,
        "block_log_path": str(block_log_path) if block_log_path else None,
        "n_blocks_logged": n_blocks_logged,
        # R13 diagnostics
        "conf_floor_override": conf_floor_override,
        "sector_filter_active": sector_filter is not None,
        "n_sector_refused": n_sector_refused,
    }


# ── CLI entry ────────────────────────────────────────────────────────


def _cli() -> int:
    """Thin argparse wrapper over run_replay so ops can reproduce
    R11 results without writing a driver script.

    Examples:
      # Replay the R11 holdout against current production ensemble
      python models/engine_replay_backtest.py \\
          --symbols default \\
          --holdout-start 2026-03-01 \\
          --holdout-end 2026-05-26 \\
          --sandbox-db logs/r11_replay.db \\
          --output logs/r11_result.json

      # Replay a custom 38-symbol universe (Part 3 style)
      python models/engine_replay_backtest.py \\
          --symbols @scripts/symbols_38.txt \\
          --holdout-start 2026-05-14 \\
          --holdout-end 2026-05-26 \\
          --sandbox-db logs/r11_part3.db \\
          --output logs/r11_part3_result.json
    """
    import argparse
    import json
    import sys as _sys
    # Force utf-8 stdout so the Delta / Rs symbols don't trip cp1252
    # on Windows (same bug R9's recovery script worked around).
    if _sys.stdout.encoding and _sys.stdout.encoding.lower() != "utf-8":
        try:
            _sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        prog="engine_replay_backtest",
        description="R11 engine-replay backtest — runs the LIVE intraday.engine "
                    "tick processor chronologically over historical 5m bars.",
    )
    parser.add_argument(
        "--symbols", required=True,
        help='Comma-separated NSE tickers (e.g. "TCS.NS,INFY.NS"), '
              'or "default" for config.DEFAULT_SYMBOLS, '
              'or "@path/to/file.txt" for newline-separated.',
    )
    parser.add_argument("--holdout-start", required=True,
                          help="Inclusive start (YYYY-MM-DD or ISO timestamp)")
    parser.add_argument("--holdout-end", required=True,
                          help="Inclusive end (YYYY-MM-DD or ISO timestamp)")
    parser.add_argument(
        "--ensemble", default="models/saved/ensemble_intraday.pkl",
        help="Path to ensemble .pkl (default: production v1)",
    )
    parser.add_argument("--sandbox-db", required=True,
                          help="Path for the temp SQLite DB (will be created)")
    parser.add_argument(
        "--output", required=True,
        help="Path for the result JSON",
    )
    parser.add_argument("--portfolio", type=float, default=500_000.0,
                          help="Starting cash for the sandbox (default 500_000)")
    parser.add_argument("--progress-every", type=int, default=500,
                          help="Print one progress line every N ticks")
    parser.add_argument(
        "--block-log", default=None,
        help="(R12) Path to write per-tick block-reason CSV. Each row: "
              "symbol, timestamp, block_reason (regime/conf/cooldown/etc), "
              "signal, confidence, regime. Omit to skip block logging.",
    )
    args = parser.parse_args()

    # Resolve --symbols → list
    if args.symbols == "default":
        from config import DEFAULT_SYMBOLS
        symbols = list(DEFAULT_SYMBOLS)
    elif args.symbols.startswith("@"):
        symbols = [s.strip() for s in
                    Path(args.symbols[1:]).read_text().splitlines()
                    if s.strip() and not s.strip().startswith("#")]
    else:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        parser.error("--symbols resolved to an empty list")

    holdout_start = pd.Timestamp(args.holdout_start)
    holdout_end = pd.Timestamp(args.holdout_end)
    if holdout_start > holdout_end:
        parser.error("--holdout-start must be on or before --holdout-end")

    result = run_replay(
        symbols=symbols,
        holdout_start=holdout_start,
        holdout_end=holdout_end,
        ensemble_path=Path(args.ensemble),
        sandbox_db_path=Path(args.sandbox_db),
        portfolio_value=float(args.portfolio),
        progress_every=int(args.progress_every),
        block_log_path=Path(args.block_log) if args.block_log else None,
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str),
                          encoding="utf-8")
    print(f"\nResult JSON -> {out_path}")

    m = result["metrics"]
    print(f"  n_trades        : {m.get('n_trades', 0)}")
    print(f"  profit_factor   : {m.get('profit_factor', float('nan')):.3f}")
    print(f"  win_rate        : {m.get('win_rate', 0):.3f}")
    print(f"  max_drawdown    : {m.get('max_drawdown', 0):.3f}")
    print(f"  wall_clock_secs : {result['wall_clock_secs']:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
