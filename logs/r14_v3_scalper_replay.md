# R14 — v3 'scalper' engine-replay report

**Holdout window:** 2026-01-01 -> 2026-05-28 (OOS for v3; same window as R12 v1/v2b)
**Symbols:** 25 (DEFAULT_SYMBOLS)
**Engine code:** R7-A+B + P30 + P28 + P50 (production as of R14)
**v3 training params:** lookahead=3, sigma_mult=0.25, vol_scaled=True, train 2024-05-27 -> 2025-12-31

## TL;DR — SHIP VERDICT

**NO_SHIP — PF gate cleared but trade-count gate failed**

_SHIP gate (R14 brief): n_trades > 24 AND PF > 1.3._
_v3 result: n_trades=8, PF=infinity._

## v1 vs v2b vs v3 — same holdout, same engine code, same gates

| Metric        | v1 (production) | v2b (R12 LIVE) | v3 (R14 scalper) |
|---------------|----------------:|---------------:|-----------------:|
| Profit Factor | 0.731 | infinity | infinity |
| Sharpe        | -3.227 | -2.602 | -2.605 |
| Win rate      | 0.500 | 1.000 | 1.000 |
| Max drawdown  | 0.017 | -0.000 | -0.000 |
| n_trades      | 8 | 8 | 8 |

_v1/v2b numbers are pulled verbatim from logs/r12_v1_vs_v2_engine_replay.md (same window, same engine, deterministic — re-running would burn ~80 min for zero information gain)._

## v3 replay metadata

|  | value |
|---|---:|
| ticks | 7350 |
| symbol-evals | 182025 |
| wall_clock_secs | 1661.9 |
| ensemble pkl | `ensemble_intraday_v3.pkl` |
| sandbox db | `r14_v3_sandbox.db` |
| block log | `r14_v3_block_reasons.csv` |

## v3 per-symbol PF

| Symbol | n_trades | PF | win_rate |
|--------|---:|---:|---:|
| AXISBANK.NS | 1 | infinity | 1.000 |
| BPCL.NS | 1 | infinity | 1.000 |
| DRREDDY.NS | 1 | infinity | 1.000 |
| HINDALCO.NS | 1 | infinity | 1.000 |
| MARUTI.NS | 1 | infinity | 1.000 |
| ONGC.NS | 1 | infinity | 1.000 |
| RELIANCE.NS | 1 | infinity | 1.000 |
| TATASTEEL.NS | 1 | infinity | 1.000 |

## v3 block-reason attribution

**v3 total blocked symbol-evals:** 113,522

| Block reason | v2b (R12) | v3 (R14) | Δ (v3 - v2b) |
|---|---:|---:|---:|
| conf_blocked | 14,386 | 83,151 | **+68,765 (5.8x)** |
| regime_blocked | 9,601 | 28,745 | +19,144 |
| daily_count_capped | 1,760 | 1,618 | -142 |
| time_cutoff | 13 | 8 | -5 |
| target_cooldown | 0 | 0 | 0 |

_v3 conf_blocked split: BUY 42,172 / SELL 40,979 — balanced, no asymmetry._

## The 0.60 wall — diagnostic for R15

The R14 retrain DID restructure the signal distribution at the model level (5.8x more raw candidates than v2b on the same window). But the confidence distribution on those candidates sits structurally below the production 0.60 conf-floor:

| Quantile | v3 conf (blocked) |
|---|---:|
| min | 0.333 |
| p10 | 0.359 |
| p50 | 0.405 |
| p90 | 0.496 |
| p99 | 0.578 |
| max | 0.600 |

**Conf-floor sensitivity on v3 (candidates that would pass the gate):**

| Floor | v3 candidates ≥ floor |
|---|---:|
| 0.55 | **2,449** |
| 0.57 | 1,236 |
| 0.58 | 754 |
| 0.60 | 0 |

This is a **fundamentally different shape** vs v2b. R13 Stage 2 showed v2b had ~30 marginal candidates between 0.55-0.60 — so the conf-floor was not the v2b bottleneck. On v3, 2,449 candidates sit at floor 0.55. Conf-floor is the v3 bottleneck.

## Recommendation

**NO_SHIP for v3 as-is.** v2b remains live; v3.pkl is preserved at `models/saved/ensemble_intraday_v3.pkl` for diagnostic comparison; no deploy.

But: v3 + lower conf-floor is a genuine R15 candidate worth investigating. Suggested R15 design (pending ops approval):

1. Run engine-replay on v3 with conf_floor_override ∈ {0.50, 0.52, 0.54, 0.55} (4 sandboxes, in parallel if mem permits)
2. SHIP gate stays at n_trades > 24 AND PF > 1.3 (the R14 brief floor — do NOT relax it just because we changed conf-floor)
3. Side-by-side report v2b@0.60 / v3@floor each tested
4. If a floor clears: that's a TWO-axis change (model + gate), so ops calibrates rollback first (`config.SIGNAL_MIN_CONFIDENCE` is the live engine's read path — needs both `ensemble_intraday.pkl` swap AND config change to deploy)

Diagnostic artifacts preserved for R15 prep:
- `models/saved/ensemble_intraday_v3.pkl` (4.27 MB)
- `models/saved/ensemble_intraday_v3.ubj` (4.05 MB, version-portable booster)
- `logs/r14_v3_sandbox.db`
- `logs/r14_v3_block_reasons.csv` (113,522 rows)
- `logs/r14_v3_memory_trace.csv`
- `logs/r14_v3_replay.log`