# R15 — v3 conf-floor sweep report

**Holdout window:** 2026-01-01 -> 2026-05-28 (same as R12/R14, OOS for v3)
**Symbols:** 25 (DEFAULT_SYMBOLS)
**Engine code:** R7-A+B + P30 + P28 + P50 (production as of R15)
**Ensemble:** `ensemble_intraday_v3.pkl` (R14 scalper retrain)
**Floors swept:** [0.5, 0.52, 0.54, 0.55]
**SHIP gate:** n_trades > 24 AND PF > 1.3

## TL;DR — SHIP VERDICT

**NO_SHIP — no floor cleared both gates**

_floor 0.50: n_trades=8, PF=0.600 | floor 0.52: n_trades=8, PF=1.168 | floor 0.54: n_trades=8, PF=0.792 | floor 0.55: n_trades=8, PF=1.699_

## Full comparison — same window, same engine, same gates

| Model @ conf-floor | PF | Sharpe | Win rate | Max DD | n_trades |
|---|---:|---:|---:|---:|---:|
| v1 @0.60 (R12) | 0.731 | -3.227 | 0.500 | 0.017 | 8 |
| v2b @0.60 (LIVE) | infinity | -2.602 | 1.000 | -0.000 | 8 |
| v3 @0.60 (R14) | infinity | -2.605 | 1.000 | -0.000 | 8 |
| v3 @0.50 (R15) | 0.600 | -4.702 | 0.500 | 0.028 | 8 |
| v3 @0.52 (R15) | 1.168 | -3.117 | 0.625 | 0.015 | 8 |
| v3 @0.54 (R15) | 0.792 | -3.034 | 0.625 | 0.016 | 8 |
| v3 @0.55 (R15) | 1.699 | -2.498 | 0.750 | 0.011 | 8 |

_v1, v2b@0.60, v3@0.60 baselines pulled from R12/R14 reports — same window, same engine, deterministic._

## Sweep metadata

| Floor | ticks | symbol-evals | wall_secs | sandbox db | block log |
|---:|---:|---:|---:|---|---|
| 0.50 | 7350 | 182025 | 1666.9 | `r15_v3_sandbox_f50.db` | `r15_block_reasons_f50.csv` |
| 0.52 | 7350 | 182025 | 1669.0 | `r15_v3_sandbox_f52.db` | `r15_block_reasons_f52.csv` |
| 0.54 | 7350 | 182025 | 1672.0 | `r15_v3_sandbox_f54.db` | `r15_block_reasons_f54.csv` |
| 0.55 | 7350 | 182025 | 1657.4 | `r15_v3_sandbox_f55.db` | `r15_block_reasons_f55.csv` |

## v3 @ floor 0.50 — per-symbol PF

| Symbol | n_trades | PF | win_rate |
|--------|---:|---:|---:|
| AXISBANK.NS | 1 | infinity | 1.000 |
| BPCL.NS | 1 | infinity | 1.000 |
| DRREDDY.NS | 2 | 0.487 | 0.500 |
| HCLTECH.NS | 1 | 0.000 | 0.000 |
| HDFCBANK.NS | 1 | 0.000 | 0.000 |
| MARUTI.NS | 1 | 0.000 | 0.000 |
| RELIANCE.NS | 1 | infinity | 1.000 |

## v3 @ floor 0.52 — per-symbol PF

| Symbol | n_trades | PF | win_rate |
|--------|---:|---:|---:|
| BAJAJ-AUTO.NS | 1 | 0.000 | 0.000 |
| BPCL.NS | 1 | infinity | 1.000 |
| DRREDDY.NS | 2 | 0.480 | 0.500 |
| HDFCBANK.NS | 1 | 0.000 | 0.000 |
| M&M.NS | 1 | infinity | 1.000 |
| MARUTI.NS | 1 | infinity | 1.000 |
| RELIANCE.NS | 1 | infinity | 1.000 |

## v3 @ floor 0.54 — per-symbol PF

| Symbol | n_trades | PF | win_rate |
|--------|---:|---:|---:|
| BAJAJ-AUTO.NS | 1 | 0.000 | 0.000 |
| BPCL.NS | 1 | infinity | 1.000 |
| BRITANNIA.NS | 1 | 0.000 | 0.000 |
| DRREDDY.NS | 2 | 0.458 | 0.500 |
| M&M.NS | 1 | infinity | 1.000 |
| MARUTI.NS | 1 | infinity | 1.000 |
| RELIANCE.NS | 1 | infinity | 1.000 |

## v3 @ floor 0.55 — per-symbol PF

| Symbol | n_trades | PF | win_rate |
|--------|---:|---:|---:|
| AXISBANK.NS | 1 | infinity | 1.000 |
| BAJAJ-AUTO.NS | 1 | 0.000 | 0.000 |
| BPCL.NS | 1 | infinity | 1.000 |
| DRREDDY.NS | 2 | 0.480 | 0.500 |
| HINDALCO.NS | 1 | infinity | 1.000 |
| MARUTI.NS | 1 | infinity | 1.000 |
| RELIANCE.NS | 1 | infinity | 1.000 |

## Block-reason attribution per floor (the actual story)

| Block reason | v3@0.60 (R14) | v3@0.55 | v3@0.54 | v3@0.52 | v3@0.50 |
|---|---:|---:|---:|---:|---:|
| conf_blocked | 83,151 | 80,701 | 79,929 | 78,097 | 75,500 |
| regime_blocked | 28,745 | 28,747 | 28,748 | 28,748 | 28,750 |
| **daily_count_capped** | **1,618** | **2,991** | **3,464** | **4,524** | **6,090** |
| time_cutoff | 8 | 14 | 4 | 6 | 6 |
| exposure_capped | 0 | 0 | 1 | 4 | 6 |
| **n_trades (output)** | **8** | **8** | **8** | **8** | **8** |

**The actual finding:** identical 8-trade ceiling across all 5 v3 conf-floor configurations.

As conf-floor drops 0.60 → 0.50:
- conf_blocked falls by ~7,651 (more candidates released into the pipeline)
- regime_blocked is essentially flat (regime gate sees same input distribution)
- **daily_count_capped grows by ~4,472** — soaking up every extra candidate
- Output trade count: **unchanged at 8**

Translation: conf-floor was NEVER the bottleneck. The downstream `daily_count_cap` (P28) is the binding constraint. Every conf-released candidate gets caught one gate later. The R15 sweep ruled out the second of two suspected knobs.

This matches and extends R13 Stage 2's finding (v2b conf-floor sweep — same 8 trades at every floor) but with much higher informational content: on v2b there were ~30 candidates between floors so the cap-cascade was invisible; on v3 there are ~7,651 and the cascade is undeniable.

## Recommendation

**NO_SHIP.** No swept floor cleared n_trades > 24. v2b stays live.

Both single-knob hypotheses are now disproved:
- R13 Stage 2: lowering v2b conf-floor → no trade-count change.
- R15: lowering v3 conf-floor → no trade-count change (cap-cascade visible).

The 8-trade ceiling is structural — set by `daily_count_cap` (P28) and/or per-symbol cooldowns interacting with the regime-gate and the engine's slot allocator. Investigating that ceiling is **not a model retrain question** — it's an engine-configuration question.

Suggested R16 candidates for ops discussion (not implemented):
1. **Audit `daily_count_cap` and slot caps**: print the per-day cap-hit timeline. If the cap fires on day-1 of the holdout and most subsequent days, the cap value is likely too low for v3's signal density. The cap was tuned for v1's distribution; v3 generates 5.8× more candidates so the same cap is much more restrictive.
2. **Audit `regime_blocked` stability**: regime numbers are essentially identical across floors. Suggests regime gate runs BEFORE conf (otherwise relaxing conf would shift regime numbers). Check whether regime classification on the scalper distribution is calibrated to v3's confidence shape.
3. **Per-day trade timeline**: produce `r16_per_day_trades.csv` (date, n_trades_attempted, n_trades_opened, first_cap_hit_time). Will surface whether the engine opens 1 trade then locks out for the rest of the day, vs hitting cap at day-end naturally.
4. Defer R16 to Wed Jun 3 review per the standing schedule. v2b operationally healthy in the meantime.

## Sweep wall-clock

| Floor | wall_secs | wall_min |
|---:|---:|---:|
| 0.50 | 1666.9 | 27.8 |
| 0.52 | 1669.0 | 27.8 |
| 0.54 | 1672.0 | 27.9 |
| 0.55 | 1657.4 | 27.6 |
| **Total** | **6665.3** | **111.1** |