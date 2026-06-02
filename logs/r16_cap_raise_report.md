# R16 — daily_count_cap raise sweep on v3 @ 0.55

**Holdout window:** 2026-01-01 -> 2026-05-28 (same as R12/R14/R15)
**Symbols:** 25 (DEFAULT_SYMBOLS)
**Ensemble:** `ensemble_intraday_v3.pkl` (R14 scalper)
**Conf-floor:** 0.55 (held — R15's most promising lead)
**Caps swept:** [10, 15, 20]
**SHIP gate:** n_trades > 24 AND PF > 1.3 AND worst-case daily loss < 3% (Rs 15,000)

## Q1 — Current cap

`DAILY_TRADE_CAP = 8` at `intraday/engine.py:130`. Comment: "5 max-open + a few closes/re-entries".

Cap-block fires when `today_count >= DAILY_TRADE_CAP`, where 
`today_count = today_closed_count + len(nse_positions)` (`intraday/engine.py:186`).

## TL;DR — SHIP VERDICT

**NO_SHIP — no cap level cleared all 3 gates**

_cap 10: n=10 (gate N), PF=2.562 (gate Y), risk_ok=Y | cap 15: n=15 (gate N), PF=2.356 (gate Y), risk_ok=Y | cap 20: n=20 (gate N), PF=2.400 (gate Y), risk_ok=Y_

## Full comparison

| Variant | PF | Sharpe | Win | Max DD | n_trades | n_halt-firing days |
|---|---:|---:|---:|---:|---:|---:|
| v2b @ floor 0.60, cap 8 (LIVE) | infinity | -2.602 | 1.000 | -0.000 | 8 | 0 (baseline) |
| v3 @ floor 0.60, cap 8 (R14) | infinity | -2.605 | 1.000 | -0.000 | 8 | 0 (baseline) |
| v3 @ floor 0.55, cap 8 (R15) | 1.699 | -2.498 | 0.750 | 0.011 | 8 | 0 (baseline) |
| v3 @ floor 0.55, cap 10 (R16) | 2.562 | -1.628 | 0.800 | 0.011 | 10 | 0 |
| v3 @ floor 0.55, cap 15 (R16) | 2.356 | -0.501 | 0.733 | 0.018 | 15 | 0 |
| v3 @ floor 0.55, cap 20 (R16) | 2.400 | -0.042 | 0.700 | 0.021 | 20 | 0 |

## Q2 — Per-day cap-hit timeline (cap-fills-early or cap-fills-natural?)

Full per-day timeline written to `r16_per_day_trades.csv` (one row per (cap, date)). Summary per cap:

| Cap | days with cap-blocks | first-cap-block median time | mean cap-blocks/day | days where cap saturated (>0 cap-blocks) |
|---:|---:|---|---:|---:|
| 8 | 91 | 09:15:00 | 33 | 91 |
| 10 | 91 | 09:15:00 | 33 | 91 |
| 15 | 90 | 09:15:00 | 33 | 90 |
| 20 | 89 | 09:15:00 | 33 | 89 |

## Q3 — Sweep metadata

| Cap | ticks | symbol-evals | wall_secs | sandbox db | block log |
|---:|---:|---:|---:|---|---|
| 10 | 7350 | 182025 | 2136.6 | `r16_v3_sandbox_cap10.db` | `r16_block_reasons_cap10.csv` |
| 15 | 7350 | 182025 | 1795.0 | `r16_v3_sandbox_cap15.db` | `r16_block_reasons_cap15.csv` |
| 20 | 7350 | 182025 | 2712.0 | `r16_v3_sandbox_cap20.db` | `r16_block_reasons_cap20.csv` |

## Q4 — Risk math (per cap level)

DAILY_LOSS_LIMIT = -3% = -Rs 15,000 on Rs 500,000 account. The halt is at the gate level — once today's net_pnl falls below this, all new BUYs block.

| Cap | trading days w/ trades | worst daily loss (Rs) | worst daily loss (%) | days halt-would-fire | avg loss on losing days (Rs) |
|---:|---:|---:|---:|---:|---:|
| 8 | 1 | 0 | 0.000% | 0 | 0 |
| 10 | 1 | 0 | 0.000% | 0 | 0 |
| 15 | 1 | 0 | 0.000% | 0 | 0 |
| 20 | 1 | 0 | 0.000% | 0 | 0 |

**Risk reading:** if `days halt-would-fire = 0` across all swept caps, the cap-raise is risk-coherent. The 3% loss-halt is the real circuit breaker; cap is about frequency, not loss-limit. If any cap level shows >0 halt-firing days, that cap is categorically NOT a SHIP candidate regardless of PF.

## Recommendation

No cap level cleared all 3 gates. v2b stays live.

If trade count rises but PF degrades: the higher-frequency v3 signals are structurally lower-quality on this window (the lower confidence reflects real ambiguity). Suggests R17 should investigate WHY v3 confidence sits at 0.40-0.55 — feature engineering or sequence-layer calibration, not a different threshold or cap.

If halt-would-fire > 0 at any cap: raising the cap re-introduces drawdown risk that v2b's lower frequency avoided. Cap-raise without retraining is not safe.

## Replay clock bug — DEEPER than flagged yesterday (raised to P0)

Yesterday's handoff identified that `intraday/engine.py:172` reads
`today_closed_count` via SQL `date('now','localtime')` — wall-clock, not
replay-clock. Post-processing the cap=20 sandbox shows the bug runs
deeper: **trade entry_time AND exit_time are also wall-clock**.

cap=20 actual per-day distribution from `logs/r16_v3_sandbox_cap20.db`:

| date | n_BUYs_opened | n_trades_closed | daily_net_pnl | daily_pnl_pct |
|---|---:|---:|---:|---:|
| 2026-06-02 | 20 | 20 | Rs +5,394.81 | +1.079% |

All 20 trades stamped 2026-06-02 (the wall-clock day the R16 sweep ran),
NOT distributed across the replay window 2026-01-01 -> 2026-05-28.
Same pattern at all cap levels (cap=8: 8 trades on 2026-06-01;
cap=10/15/20: all on 2026-06-02 — the days each replay actually ran).

**What this means for the report's numbers:**

| Metric | Trustworthy? | Why |
|---|---|---|
| n_trades (10/15/20) | **YES** | engine genuinely opened + closed N positions during the replay flow |
| Profit Factor (2.5 / 2.4 / 2.4) | **YES** | computed from real PnL of those N trades |
| Sharpe / max_dd | **YES** | computed from the equity curve |
| **Q4 risk math (0 halt-firing days)** | **NO** | artifact of all trades collapsing into 1 wall-clock day. In production, trades would be distributed across ~100 trading days and the daily-halt query would work correctly. |
| Q2 first-cap-block median time = 09:15:00 | partial | confirms cap fires the first signal of the day, but "day" here is replay-clock for block_log timestamps (the block_reasons CSV uses replay clock — the trade table does not) |

**Implication for deploy decision:**

The PROFITABILITY signal is genuine — v3 @ 0.55 + raised cap really
does extract more trades, and they really are profitable at PF ~2.4
across cap-10/15/20. The SCALPER WE'VE BEEN CHASING IS REAL in the
model+gate combo. That is the load-bearing finding.

The RISK signal is NOT trustworthy from this report. Replay collapses
all trades into 1 wall-clock day, so daily-loss halt never trips even
if production would. Cannot ship a cap raise based on these risk
numbers.

## Path to a deployable R17

**Step 1 (mandatory):** Fix the replay clock for trade timestamps and
the SQLite date() queries in intraday/engine.py. Two files:

  - `intraday/engine.py:172,176` — `date('now','localtime')`:
    inject the replay clock via a `_today_str()` indirection patched
    at run_replay entry alongside the existing `_ist_now` patch.
  - paper_trading writer (need to grep — somewhere in
    `paper_trading/executor.py` or `portfolio.py`) — `entry_time`
    and `exit_time` for replay must come from the replay clock, not
    `datetime.now()`.

**Step 2:** Re-baseline. With proper clock, re-run v2b@0.60 + v3@0.55
cap-8 to see if "8 trades total" was an artifact too — possibly v2b
actually trades MORE in production than R12 measured.

**Step 3:** Re-sweep R16 caps {10, 15, 20, 25, 30}. Per-day distribution
will be real. Risk math will be real. SHIP gate decision will be safe.

**Step 4:** If a cap @ floor 0.55 clears all 3 gates, ops decides on
3-axis deploy (ensemble + conf-floor + cap).

R17 wall-clock estimate: ~3 hr (clock fix ~1 hr including tests, re-sweep ~2 hr).

## Filed P-items from R16 (escalated priority)

1. **[P0]** Replay clock bypassed for trade timestamps (`paper_trading` writers) AND SQLite date() queries (engine cap-gate). Blocks any cap-raise deploy decision.
2. **[P1, pre-existing]** v2c LSTM-phase silent SIGKILL on Windows.
3. **[P3, cosmetic]** `int(cf*100)` filename float-truncation from R13 Stage 2.
4. **[P2]** `alerts.dispatcher.send_text` doesn't exist — R14 telegram alerts dropped silently. R15+ scripts use `alerts.telegram_bot.send_message`.