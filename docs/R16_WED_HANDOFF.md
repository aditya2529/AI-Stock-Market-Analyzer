# R16 — Wed Jun 3 Handoff (Pre-Think Only)

**Drafted:** 2026-06-02 night, after R14/R15 land NO_SHIP
**For:** fresh-head Wednesday morning
**Do NOT build any of this Monday/Tuesday.** Ops directive: tired-night cap changes are risk-control mistakes. Wednesday only.

---

## Where we left off

Engine LIVE on v2b. v3 retrained + replayed at 5 conf-floors. All produce **exactly 8 trades**.

### What's been ruled out (don't redo)

| Round | SHA | Hypothesis | Result |
|---|---|---|---|
| R12 | (prior) | v1 → v2b retrain | v2b ships, 8 trades, PF inf |
| R13 Stage 2 | (prior) | v2b conf-floor sweep | 8 trades at every floor |
| R14 | c15a881 | v3 scalper retrain (lookahead=3, sigma_mult=0.25) | 8 trades |
| R15 | 708183f | v3 conf-floor sweep {0.50, 0.52, 0.54, 0.55} | 8 trades at every floor |

**Both single-knob model-side hypotheses are dead.** Next investigation is engine-side.

---

## The lead — v3 @ 0.55 looks like the scalper we wanted

From R15:

| Floor | PF | Sharpe | Win | Trades |
|---:|---:|---:|---:|---:|
| v2b @0.60 (LIVE) | inf | -2.602 | 1.000 | 8 |
| **v3 @0.55** | **1.699** | **-2.498** | **0.750** | **8** |
| v3 @0.50 | 0.600 | -4.702 | 0.500 | 8 |

v3@0.55 is the only swept floor where PF > 1.3 AND the per-trade quality looks plausible at a higher signal-density. If the 8-cap is the only throttle, lifting it MIGHT surface the scalper we've been chasing since R12. That's the R16 thesis.

---

## Q1 — What IS the cap, and where is it set?

**Pre-looked-up so Wed doesn't have to:**

| Constant | Value | Location | Comment in code |
|---|---|---|---|
| `DAILY_TRADE_CAP` | **8** | `intraday/engine.py:130` | "5 max-open + a few closes/re-entries" |
| `DAILY_LOSS_LIMIT` | -0.03 | `intraday/engine.py:129` | halt new opens if today's net_pnl < -3% |
| `TOTAL_EXPOSURE_CAP` | 0.80 | `intraday/engine.py:128` | never deploy > 80% of nse_initial_cash |

The gate logic at `intraday/engine.py:186-190`:

```python
today_count = today_closed_count + len(nse_positions)
if today_count >= DAILY_TRADE_CAP:
    return {"_action": "daily_count_capped", "symbol": symbol}
```

`today_closed_count` query at `intraday/engine.py:174-177`:

```python
"SELECT COUNT(*) FROM paper_trades WHERE date(exit_time) = date('now','localtime')"
```

---

## Q2 — Per-day cap-hit timeline (don't build, just sketch)

The deliverable: `r16_per_day_trades.csv` with columns

| date | n_signals_passed_conf | n_signals_passed_regime | n_BUYs_opened | first_cap_block_time | end_of_day_open_positions |

Build path (Wed):

1. Add per-tick counters to the replay loop (already partial — see `tick_counts` in `intraday/engine.py:713-718`).
2. Write tick_counts to a CSV per (date, symbol) inside `run_replay`.
3. Pivot in pandas.

Reuse `models/engine_replay_backtest.py` + the v3@0.55 sandbox DB at `logs/r15_v3_sandbox_f55.db` if it survives (otherwise re-run that single floor — 28 min).

---

## Q3 — Replay v3 @ 0.55 with cap raised {10, 15, 20}

Same harness as R15 but parameterize `DAILY_TRADE_CAP` via env var or `cap_override` kwarg in `_p28_daily_gate_block`.

**Pre-think — the replay-vs-live discrepancy Wed needs to know about:**

The cap reads `today_closed_count` via `date('now','localtime')` — wall-clock today, NOT replay-clock today. During the R14/R15 replays running on 2026-06-01..02:

- `today_closed_count` = trades closed today wall-clock = 0 (none of the replay's trades have exit_time = 2026-06-01)
- So `today_count = 0 + len(nse_positions)` = "currently-open positions"
- The cap effectively becomes "max 8 concurrent open positions across the ENTIRE replay timeline" — not "max 8 trades per day"

**Implication for R16:**

- In PRODUCTION, the cap resets daily (date(now,localtime) advances).
- In REPLAY, the cap behaves like a global concurrent-position cap.
- So replay results MAY UNDERESTIMATE the live trade count. v3 in production might already trade more than 8 across the 5-month equivalent — we can't tell from replay alone.
- Before raising the cap based on replay numbers, decide whether to:
  - **(a)** fix the replay clock first (patch `date('now','localtime')` via FakeDate — already patched at point 8 of `models/engine_replay_backtest.py:43-46`, but the SQLite query bypasses Python date), then re-run R14/R15 baselines to see if "8" was a replay artifact, OR
  - **(b)** accept the replay-as-concurrent-cap reading and just measure marginal change when cap is raised in replay (less rigorous but faster).

**Recommended:** (a). The replay clock-not-respected-by-SQLite is a P-item in its own right.

---

## Q4 — Risk math: cap × per-trade loss vs 3% daily halt

Per-trade SL is ATR-scaled. Position sizing is in `paper_trading/portfolio.py`. Approximate sketch:

- Account: Rs 500K (`nse_initial_cash`).
- Daily loss halt: 3% = Rs 15K.
- Typical per-trade SL distance (R12 baseline): ~0.5-1.5% of entry × position size.
- Per-trade loss range: ~Rs 500 – Rs 2,500 (depends on sizing).

Worst-case math at cap=20:
- 20 losers × Rs 2,500 = Rs 50K = 10% account ← exceeds halt by 3.3x
- But: daily_loss_halt would fire at Rs 15K, blocking further opens. So actual realized loss is bounded.

The hard ceiling on daily loss is `DAILY_LOSS_LIMIT` (-0.03), NOT `DAILY_TRADE_CAP`. The cap is about trade FREQUENCY and exposure-concentration, not loss-limit.

**Wed decision needed:** does raising cap to 15-20 meaningfully change risk profile given the loss-halt already caps daily loss? Likely **no** — loss-halt is the real circuit breaker. Cap is mostly about preventing runaway re-entries on a hot day.

---

## Suggested Wed schedule

1. **09:00–09:30** — read this doc + R14/R15 reports cold.
2. **09:30–10:00** — Q2 build (per-day CSV); confirm cap-hit pattern.
3. **10:00–10:15** — Q3 replay clock bug decision (option a or b).
4. **10:15–12:30** — Q3 sweep (3 caps × ~28 min each = ~84 min, sequential).
5. **12:30–13:30** — Q4 risk math; decide ship/no-ship on cap+conf combo.
6. **13:30–14:00** — report + commit + push.

---

## Don't lose

- v3.pkl / v3.ubj at `models/saved/ensemble_intraday_v3.pkl` (preserve through R16)
- v3@0.55 sandbox at `logs/r15_v3_sandbox_f55.db` (if Wed wants to skip re-running floor 0.55)
- All 5 block-reason CSVs at `logs/r15_block_reasons_f{50,52,54,55}.csv` + R14's `logs/r14_v3_block_reasons.csv`

---

## P-items deferred (file as separate audit-pending commits Wed AM)

1. **v2c LSTM-phase silent SIGKILL** on Windows after 15-25 min sustained CPU (killed v2c retrain). Mitigation: retry-once. Root cause unknown — needs faulthandler+Process Hacker repro.
2. **`int(cf*100)` float-truncation in R13 Stage 2 filenames** — cosmetic only; cf=0.57 produced `..._cf56_...` files. Results correct, filenames mislabeled.
3. **`alerts.dispatcher.send_text` doesn't exist** — R14/R15 telegram alerts at end-of-replay dropped silently. Use `alerts.telegram_bot.send_message` instead (already fixed in R15 script; R14 script still has the bug for archaeology).
4. **Replay clock bypassed by SQLite `date('now','localtime')`** in `intraday/engine.py:172,176` — surfaced just now while writing this doc. This is the Q3 (a) above; can be done as part of R16 or as a separate P-item.

---

Good night.
