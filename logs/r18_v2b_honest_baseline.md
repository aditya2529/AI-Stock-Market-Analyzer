# R18 v2b honest baseline — post-fix replay

**Holdout:** 2026-01-01 -> 2026-05-28
**Ensemble:** v2b @ floor 0.6 cap=8 (LIVE config, unchanged)
**Replay fix SHA:** 36b9eff (strict-less-than slicing in 3 patch sites)

## Three numbers compared

| | R12-R17 backtest (look-ahead) | **R18 backtest (fixed)** | Live v2b (since 2026-05-29) |
|---|---:|---:|---:|
| n_trades | 197 | **292** | 16 |
| Profit Factor | 8.126 | **1.167** | 0.764 |
| Win rate | 0.888 | **0.658** | 0.438 |
| Sharpe | 12.626 | **0.731** | n/a (small sample) |
| Max DD | 0.035 | **0.338** | n/a (small sample) |

## Reading

**R18 fix CONFIRMED.** Post-fix backtest PF lands in the live-plausible range (0.4-1.8 brackets where live's 0.76 sample sits comfortably). The 10x backtest-vs-live PF gap is fully explained by the 5-minute replay look-ahead. R12-R17 PF rankings are NOT informative about live.

**Operational consequence:** v2b stays live with realistic expectations. The model is approximately break-even, not the PF-8 phantom the look-ahead promised. Same for v3 — pivot to swing as previously discussed by ops is now data-supported.

## Run metadata

| | |
|---|---|
| wall_clock | 38.3 min |
| ticks | 7350 |
| symbol-evals | 182025 |
| sandbox | `r18_v2b_honest_sandbox.db` |
| block log | `r18_v2b_honest_block_reasons.csv` |

Compare to R17 v2b (same ensemble, same window, look-ahead enabled): n=197, PF=8.126, win=0.888, Sharpe=12.626, max_dd=0.035, wall=38.5 min.