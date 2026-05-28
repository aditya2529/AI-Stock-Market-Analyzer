# R11 — Engine-Replay Backtest Harness — ops handoff

**Status:** ready for ops review. All commits local; no push.
**Branch:** `master`
**Commits (R11 only):**
- `9158607` — RED tests for engine-replay harness (14 cases)
- `834ce07` — engine-replay impl GREEN (224/2/0)
- `856423a` — predict-precompute optimization (97× speedup) + paper_trades schema fix
- `<final-commit>` — CLI + R11 handoff doc + cosmetic report fix (this commit)

---

## What ships

A backtest harness that runs the **actual** `intraday.engine` tick processor chronologically through historical 5m bars — same gates, same cooldowns, same sizing, same accounting as the live engine. Built specifically to close the **P49 backtest-vs-live PF gap**.

The shortcut harness (R9 `models/retrain_and_backtest_v2.py`) is officially **deprecated for deploy-gate decisions** as of R11. R11 Part 1 showed shortcut PF 2.39 vs replay PF 0.02 on the same model + same holdout — shortcut's "615 trades" became 8 once engine gates ran.

R11 Part 3 showed **0 of 66 archived trades** would have been taken by the current engine code — prevented loss equals the entire lifetime −Rs 14,295.

## The 9+1 monkey-patches

`intraday/engine.py` is **not modified** by R11. The harness imports the engine functions and patches 9 named dependencies that point outward to the data feed, system clock, and alert dispatch. Engine gating + cooldown + sizing logic runs **unchanged**.

| Patched | Where | Effect |
|---|---|---|
| `_fetch_intraday(symbol)` | `intraday.engine` | Return raw OHLCV slice ending at replay clock — NOT a yfinance call |
| `engineer_features(df)` | `features.engineer` (source — engine local-imports) | Return precomputed featured slice from `ctx.featured_by_symbol[ctx.current_symbol]` |
| `_ist_now()` | `intraday.engine` | Return replay clock as tz-aware IST datetime |
| `_market_open()` | `intraday.engine` | Always True during replay |
| `_seconds_to_next_bar()` | `intraday.engine` | 0 — replay drives the clock manually |
| `date` (class) | `intraday.engine` | `_FakeDate.today()` → ctx clock's date (cooldown keys are date-keyed) |
| `datetime` (class) | `intraday.engine` | `_FakeDatetime.now(tz)` → replay clock (R7-B's `_is_buy_cutoff_active` calls `datetime.now(IST)` directly) |
| `on_signal` / `on_trade_closed` / `on_portfolio_snapshot` / `send_engine_pulse` | `alerts.dispatcher` (source — engine local-imports) | No-ops. **No Telegram during replay.** No live alert log writes. |
| `ensemble.predict_with_confidence` | the loaded Ensemble **instance** | Return precomputed prediction slice (the 97× perf win). Patched on the instance, not the class — restored in `finally`. |

Plus `_FETCH_CACHE.clear()` is called at patch-install time so a prior tick's frame doesn't leak in.

## Sandbox DB pattern

Engine writes (paper_config, paper_positions, paper_trades, cooldown keys) flow through `data.database.DB_PATH`. The harness redirects this to a **temp SQLite file** per replay run — same pattern `tests/test_p30_sl_cooldown_persistence.py` uses for the SL cooldown regression test (proven safe since P30).

The sandbox DB persists after the replay finishes — drop into it with `sqlite3 logs/r11_full_sandbox.db` if a result looks weird and you want to audit which gate fired when.

## CLI usage

```bash
# Run the R11 holdout against production v1 model
python models/engine_replay_backtest.py \
    --symbols default \
    --holdout-start 2026-03-01 \
    --holdout-end 2026-05-26 \
    --sandbox-db logs/r11_replay.db \
    --output logs/r11_result.json

# Replay a custom universe (e.g. 38 symbols of the archived 66-trade set)
python models/engine_replay_backtest.py \
    --symbols "@scripts/symbols_38.txt" \
    --holdout-start 2026-05-14 \
    --holdout-end 2026-05-26 \
    --sandbox-db logs/r11_part3.db \
    --output logs/r11_part3_result.json

# Run against a different ensemble file (e.g. v2 from R9)
python models/engine_replay_backtest.py \
    --symbols default \
    --holdout-start 2024-06-01 \
    --holdout-end 2026-02-28 \
    --ensemble models/saved/ensemble_intraday_v2.pkl \
    --sandbox-db logs/r12_v2_replay.db \
    --output logs/r12_v2_result.json
```

Flags:
- `--symbols` accepts `default` (config.DEFAULT_SYMBOLS), a comma-separated list, or `@path/to/file.txt` for newline-separated
- `--portfolio` defaults to Rs 500,000 (matches the cutover reset)
- `--progress-every` defaults to 500 ticks (one progress line per ~500 5m bars)

Output JSON shape (matches the R9 shortcut harness for direct diff):
```
{
  "metrics": {profit_factor, sharpe, win_rate, max_drawdown, cagr, n_trades},
  "per_symbol": {sym: {n_trades, pf, win_rate, total_pnl}},
  "n_ticks": int, "n_symbol_evaluations": int,
  "wall_clock_secs": float, "sandbox_db_path": str, ...
}
```

## Performance notes

Naïve replay (no precompute): per-symbol-eval cost ~1s (mostly `ensemble.predict_with_confidence`). For the R11 full holdout (4,725 ticks × 25 symbols = 118K evals) that projected to ~34 hours.

The precompute optimization in commit `856423a`:
- For each symbol, calls `ensemble.predict_with_confidence(featured_full)` ONCE upfront
- Stashes the result in `ctx.predictions_by_symbol[symbol]`
- Patches the ensemble's `predict_with_confidence` to return a `.loc[<= clock]` slice — pure Series indexing
- Causality is mathematically equivalent (LSTM inference uses inputs ≤ T for output at T) — a sanity check at run start compares per-bar inference vs precomputed slice; falls back to non-patched mode if they diverge

Observed wall-clock after optimization:
| Run | Symbols × ticks | Time |
|---|---|---|
| Smoke (Phase 3a) | 3 × 150 | 4.8s |
| Full holdout (Phase 3b) | 25 × 4,200 | 962s (~16 min) |
| Part 3 (Phase 5b) | 38 × 675 | 354s (~6 min, plus 155s precompute for 38 symbols) |

## Rollback

Pure additive feature. To revert R11:
```bash
git revert <final-commit> 856423a 834ce07 9158607
```

Removes:
- `models/engine_replay_backtest.py`
- `tests/test_r11_engine_replay.py`
- `R11_ENGINE_REPLAY_HANDOFF.md` (this file)

Engine, paper trading, alerts, signals, backtesting, dashboard, the A+B overlays — all untouched. The live engine boots Fri 09:10 IST identically with or without R11.

## Test coverage

- `tests/test_r11_engine_replay.py` — **14 cases**, all GREEN
  - Module surface (2)
  - 9 patch points (8 plus the FETCH_CACHE clear)
  - Sandbox DB isolation (2)
  - Driver shape + idempotency (2)
- Full suite at this commit: **224 passed, 2 skipped** (210 pre-R11 + 14 new). Zero regression.

## Files touched (R11 only)

```
NEW    models/engine_replay_backtest.py       (~700 lines: harness + CLI)
NEW    tests/test_r11_engine_replay.py        (~300 lines: contract tests)
NEW    R11_ENGINE_REPLAY_HANDOFF.md           (this doc)
NEW    logs/r11_engine_replay_vs_shortcut.md  (Part 1 report — gitignored)
NEW    logs/r11_replay_vs_live.md             (Part 3 report — gitignored)
NEW    logs/r11_full_replay_result.json       (Part 1 raw — gitignored)
NEW    logs/r11_part3_replay_result.json      (Part 3 raw — gitignored)
NEW    logs/r11_part3_diff.csv                (Part 3 archive diff — gitignored)
NEW    logs/r11_full_sandbox.db               (Part 1 sandbox — gitignored)
NEW    logs/r11_part3_sandbox.db              (Part 3 sandbox — gitignored)
```

**Zero touches to:** `intraday/engine.py`, `paper_trading/`, `signals/`, `alerts/`, `backtesting/`, `api/`, `dashboard/`, `scripts/`, `live_trading/`, `features/engineer.py`, `models/ensemble.py`, `data/database.py`, `data/validator.py`, `config.py`, `.env`, `market_data.db` schema, the R9 shortcut harness `models/retrain_and_backtest_v2.py`, `ensemble_intraday.pkl`, the 210 pre-R11 tests, the A+B overlays.

Plus, separately, R11 Part 3 added **5m bars for 38 symbols** to `market_data.db` (~1.4M new rows via R8 Upstox adapter). Those rows are additive — the live engine ignores them because its `SELECT ... WHERE symbol IN (DEFAULT_SYMBOLS)` doesn't reach the morning-scanner universe.

## Known quirks worth flagging for ops sign-off

1. **Δ Unicode bug on Windows stdout** — the report-builder scripts (in `D:\Projects\AI Work Shift\`) print a side-by-side table with the Δ character. Windows `cp1252` stdout encoding can't encode `Δ`, raising `UnicodeEncodeError` AFTER the file write completes. The crash is cosmetic (file already on disk) but bumps exit code to 1 and noises up the log. **Fixed in the harness CLI** (calls `sys.stdout.reconfigure(encoding="utf-8")` at startup); not yet fixed in the throwaway builder scripts (they're outside the repo). Same bug R9's recovery script worked around.

2. **Precompute optimization assumes LSTM causality.** Holds for any inference-time forward-only network (no future-bar leak in standard LSTM/XGBoost/HMM). If a future model layer uses bi-directional or attention-over-future context, the precompute path would silently introduce look-ahead bias. The runtime causality check (line ~395 of `engine_replay_backtest.py`) compares per-bar vs precomputed at one sample point — catches gross drift but isn't a guarantee. **Falls back to non-patched mode if check fails.**

3. **Sandbox DB grows ~25 KB per replay** — negligible. Not auto-cleaned; ops can delete `logs/r11_*_sandbox.db` files at convenience.

4. **R11 doesn't instrument per-trade block reason.** Part 3 confirmed all 66 archived trades are LIVE_ONLY, but the harness doesn't log WHICH gate blocked each one (regime / conf / cooldown / slot / P28 / 14:00 cutoff / model HOLD). Per-trade attribution is an R12 backlog candidate; would let ops see which overlay is doing the heavy lifting.

5. **Δ + "440 symbols" log line** in the R9 shortcut harness — NOT touched by R11 (different file, different bug). Belongs in a separate cosmetic-fix commit when convenient.

## What R12 likely needs (forward-looking notes, not work products)

- **v1-vs-v2 re-test on extended replay window** (option (d) from R11 PING decision #2). The R9 NO_SHIP verdict for v2 was decided on shortcut PFs that R11 proved are fictions. Engine-replay over 6-12 months with both models gives the real answer.
- **Per-trade block-reason instrumentation** (point 4 above).
- **Possible: replay with pre-R7 engine code** (option (b) from yesterday's brief, deferred from R11). Would let us isolate "data/latency gap" from "overlay impact."
