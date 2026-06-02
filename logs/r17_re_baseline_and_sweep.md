# R17 — re-baseline + re-sweep (replay clock fix in place)

> ⚠️ **PARTIAL REPORT** — 1 of 4 runs completed. Pending: v3_floor55_cap8_REBASELINE, v3_floor65_cap20, v3_floor70_cap30. This file rewrites after every successful run, so it reflects the latest state even if the process is killed.

**Holdout window:** 2026-01-01 -> 2026-05-28
**Symbols:** 25 (DEFAULT_SYMBOLS)
**Clock fix:** SHA 30a20a6 (intraday/engine.py SQL bind + portfolio.datetime patch + FakeDatetime.utcnow)
**SHIP gate:** n_trades > 24 AND PF > 1.3 AND 0 halt-firing days

## TL;DR — v2b confirmed in backtest, v3 closed out, LIVE DIVERGENCE OPEN

R17 frames the model question, not the deploy question:

1. **v2b in backtest** clears the SHIP gate by ~8x once the replay clock is fixed (n_trades=197, PF=8.1, win=0.888, Sharpe=12.6). The R12-R16 chase was working off artifact data.
2. **v3 closed out.** The 3 v3 closeout runs at this scope (floor 0.55 cap=8, floor 0.65 cap=20, floor 0.70 cap=30) test whether v3 can beat v2b's backtest. None of R14-R16's v3 data suggests it will — see the ranked tables.
3. **CRITICAL — backtest does NOT match live.** Live v2b since 2026-05-29 is mixed/losing. See "Live v2b vs R17 backtest" section. We need 2-3 weeks of live data before trusting the 8.1 PF holds in reality. **Do not change anything yet.** v2b stays live as-is.

_Backtest SHIP_: **v2b_floor60_cap8_REBASELINE** — n_trades=197, PF=8.126, worst daily loss=Rs -2,490 (-0.498%)._

## Live v2b vs R17 backtest (P49 echo)

**Window:** live since 2026-05-29 → 2026-06-02 (2 trading days with trades)

| | Live v2b (since 2026-05-29) | R17 v2b backtest (5 mo OOS) | R12 v2b backtest (broken clock) |
|---|---:|---:|---:|
| n_trades | **16** | 197 | 8 (artifact) |
| Win rate | **0.438** | 0.888 | 1.000 (artifact) |
| Profit Factor | **0.764** | 8.126 | infinity (artifact) |
| Net PnL (Rs) | **-1,645** | (backtest equity curve, see metadata) | n/a |
| Trades/active day | **8.0** | 3.0 (≈) | n/a |

**Per-day live v2b:**

| Date | n_trades | wins | net Rs |
|---|---:|---:|---:|
| 2026-05-29 | 8 | 4 | -150 |
| 2026-06-02 | 8 | 3 | -1,495 |

**Divergence reading:** live PF 0.764 is below break-even; R17 backtest claims PF 8.126. ~10x gap. Live is hitting the 8-trade cap every active day (8.0/day) while backtest averages ~2/day. The cap is binding live but not in backtest — same P49 backtest-vs-live shape as before R17, just clearer now that we can compare. Sample is only 2 trading days — could be noise, but the magnitude warrants caution.

**Implication:** the R17 clock fix closed the obvious wall-clock-collapse bug but the backtest still does not predict live. R18 candidates to investigate (not blocking v2b's continued operation):
- Live data feed (Upstox / yfinance) vs backtest data quality / latency
- Slippage / brokerage assumptions in the backtest vs realised costs
- Whether `precompute_features` in the replay produces causally-different signals from live `engineer_features` mid-tick
- The 8-trade DAILY_TRADE_CAP itself — backtest tail might over-sample low-traffic days, hiding that the cap binds on a typical day

**Operationally:** v2b stays live. Need 2-3 more weeks of live data before trusting backtest projections to set any deploy gate. R18 is diagnostic, not deploy-blocking.

## v2b LIVE baseline — R12 fiction vs R17 reality

| Source | n_trades | PF | distinct close-dates | worst loss |
|---|---:|---:|---:|---:|
| R12 v2b (broken clock) | 8 | infinity | 1 (all 2026-05-28 ish) | n/a (artifact) |
| **R17 v2b (fixed clock)** | **197** | **8.126** | **65** | **Rs -2,490** |

**Backtest finding:** v2b trades ~197 times over the 5-month window in correctly-clocked replay. The 8-trade ceiling that R12-R16 chased was a replay artifact. The "v2b too selective" premise was wrong.

**But — does this match live?** See "Live v2b vs R17 backtest" section below. The corrected backtest still does not match what v2b is actually doing in production. Treat the 8.1 PF and 88% WR as backtest claims, not deploy-ready signals.

## Full comparison — R16 vs R17 numbers

| Variant | R16 PF | R17 PF | R16 n | R17 n | R16 days | R17 days | R17 worst loss | R17 SHIP |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| v2b_floor60_cap8_REBASELINE | infinity | 8.126 | 8 | 197 | 1 (artifact) | 65 | Rs -2,490 | ✓ |
| v3_floor55_cap8_REBASELINE | 1.699 | — | 8 | — | 1 (artifact) | — | — | (pending) |
| v3_floor65_cap20 | — | — | — | — | — | — | — | (pending) |
| v3_floor70_cap30 | — | — | — | — | — | — | — | (pending) |

## Ranked by PF (descending)

| Rank | Variant | PF | n_trades | Win rate | Halt-firing days | SHIP |
|---:|---|---:|---:|---:|---:|:---:|
| 1 | v2b_floor60_cap8_REBASELINE | 8.126 | 197 | 0.888 | 0 | ✓ |

## Ranked by n_trades (descending)

| Rank | Variant | n_trades | PF | Win rate | Halt-firing days | SHIP |
|---:|---|---:|---:|---:|---:|:---:|
| 1 | v2b_floor60_cap8_REBASELINE | 197 | 8.126 | 0.888 | 0 | ✓ |

## Per-day risk (real distribution, post-fix)

| Variant | days w/ trades | worst daily loss (Rs) | worst (%) | halt-firing days | avg loss/losing day |
|---|---:|---:|---:|---:|---:|
| v2b_floor60_cap8_REBASELINE | 65 | -2,490 | -0.498% | 0 | -1,170 |
| v3_floor55_cap8_REBASELINE | (pending) | — | — | — | — |
| v3_floor65_cap20 | (pending) | — | — | — | — |
| v3_floor70_cap30 | (pending) | — | — | — | — |

_DAILY_LOSS_LIMIT = -3% = -Rs 15,000. Halt-firing days > 0 = categorical NO_SHIP._

## Run metadata

| Variant | wall_secs | sandbox db | block log |
|---|---:|---|---|
| v2b_floor60_cap8_REBASELINE | 1934.0 | `r17_sandbox_v2b_floor60_cap8_REBASELINE.db` | `r17_block_reasons_v2b_floor60_cap8_REBASELINE.csv` |
| v3_floor55_cap8_REBASELINE | (pending) | — | — |
| v3_floor65_cap20 | (pending) | — | — |
| v3_floor70_cap30 | (pending) | — | — |

## Recommendation

**Backtest SHIP_BY_BACKTEST = v2b_floor60_cap8_REBASELINE** — cleared the 3 gates with real per-day risk distribution.

**v2b LIVE config is the backtest winner.** Same config as production — no swap needed. **BUT** the live-vs-backtest section above shows live behavior does not match this backtest. Do not treat this as a deploy decision. v2b stays live; we need 2-3 weeks of live data to verify whether the 8.1 PF / 88% WR translates to reality. If the gap persists, R18 must investigate the data/slippage/feature-causality candidates listed in the live-vs-backtest section.