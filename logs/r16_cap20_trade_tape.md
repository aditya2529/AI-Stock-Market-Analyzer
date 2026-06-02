# R16 cap=20 — full trade tape (v3 @ floor 0.55)

Source: logs/r16_v3_sandbox_cap20.db (paper_trades table, 20 rows)

**Caveat:** entry_time and exit_time are wall-clock-stamped (the R16 P0).
Hold duration reported here is replay-EXECUTION seconds, NOT market hold
minutes. All other columns are real engine flow.

## Per-trade tape (chronological by entry)

| # | Symbol | Entry Rs | Exit Rs | Shares | Net PnL Rs | Ret % | Exit reason | Entry conf | Entry regime | Exec sec |
|---:|---|---:|---:|---:|---:|---:|---|---:|---|---:|
| 1 | DRREDDY.NS | 1254.93 | 1247.98 | 63 | -643.08 | -0.81% | stop_loss | 0.555 | TRENDING_UP | 11.4 |
| 2 | RELIANCE.NS | 1574.94 | 1584.34 | 50 | +264.29 | +0.34% | target | 0.891 | TRENDING_UP | 0.4 |
| 3 | BPCL.NS | 381.50 | 384.25 | 220 | +386.99 | +0.46% | target | 0.895 | TRENDING_UP | 0.4 |
| 4 | MARUTI.NS | 16734.73 | 16888.02 | 5 | +547.90 | +0.65% | target | 0.899 | TRENDING_UP | 0.4 |
| 5 | HINDALCO.NS | 895.46 | 904.82 | 112 | +786.15 | +0.78% | target | 0.897 | TRENDING_UP | 0.4 |
| 6 | DRREDDY.NS | 1241.41 | 1249.47 | 64 | +308.72 | +0.39% | target | 0.607 | TRENDING_UP | 4.2 |
| 7 | BAJAJ-AUTO.NS | 9482.31 | 9397.77 | 8 | -872.70 | -1.15% | stop_loss | 0.574 | TRENDING_UP | 1.3 |
| 8 | AXISBANK.NS | 1270.65 | 1278.44 | 63 | +281.76 | +0.35% | target | 0.863 | TRENDING_UP | 0.4 |
| 9 | ONGC.NS | 242.31 | 244.41 | 347 | +508.18 | +0.60% | target | 0.878 | TRENDING_UP | 0.4 |
| 10 | TATASTEEL.NS | 183.04 | 185.15 | 490 | +800.03 | +0.89% | target | 0.852 | TRENDING_UP | 0.4 |
| 11 | HINDALCO.NS | 927.70 | 934.03 | 106 | +414.40 | +0.42% | target | 0.868 | TRENDING_UP | 0.4 |
| 12 | KOTAKBANK.NS | 439.07 | 436.93 | 183 | -599.80 | -0.75% | stop_loss | 0.866 | TRENDING_UP | 2.6 |
| 13 | BAJAJ-AUTO.NS | 9511.35 | 9638.95 | 8 | +821.67 | +1.08% | target | 0.859 | TRENDING_UP | 0.3 |
| 14 | HINDALCO.NS | 932.21 | 954.36 | 99 | +1949.79 | +2.11% | target | 0.879 | TRENDING_UP | 0.3 |
| 15 | BPCL.NS | 369.13 | 366.02 | 218 | -885.38 | -1.10% | stop_loss | 0.572 | TRENDING_UP | 0.5 |
| 16 | RELIANCE.NS | 1503.95 | 1504.54 | 53 | -176.07 | -0.22% | force_close_eod | 0.595 | TRENDING_UP | 11.5 |
| 17 | INFY.NS | 1607.09 | 1630.48 | 50 | +959.11 | +1.19% | target | 0.865 | TRENDING_UP | 0.4 |
| 18 | HCLTECH.NS | 1617.50 | 1636.17 | 52 | +750.90 | +0.89% | target | 0.863 | SIDEWAYS | 0.3 |
| 19 | BRITANNIA.NS | 6136.47 | 6185.95 | 14 | +468.46 | +0.54% | target | 0.867 | TRENDING_UP | 0.4 |
| 20 | CIPLA.NS | 1465.90 | 1457.40 | 55 | -676.53 | -0.84% | stop_loss | 0.562 | TRENDING_UP | 0.5 |

## Win/loss

| | n | Sum Rs | Avg Rs | Avg ret% |
|---|---:|---:|---:|---:|
| Wins | 14 | +9,248 | +661 | +0.76% |
| Losses | 6 | -3,854 | -642 | -0.81% |
| **Total** | **20** | **+5,395** | — | — |

**Profit Factor (sum_wins / |sum_losses|):** 2.400
**Realized R:R (avg_win / |avg_loss|):** 1.03:1

## Symbol diversity (20 trades across 14 unique symbols)

| Symbol | n_trades | n_wins | n_losses | net Rs |
|---|---:|---:|---:|---:|
| HINDALCO.NS | 3 | 3 | 0 | +3,150 |
| DRREDDY.NS | 2 | 1 | 1 | -334 |
| RELIANCE.NS | 2 | 1 | 1 | +88 |
| BPCL.NS | 2 | 1 | 1 | -498 |
| BAJAJ-AUTO.NS | 2 | 1 | 1 | -51 |
| MARUTI.NS | 1 | 1 | 0 | +548 |
| AXISBANK.NS | 1 | 1 | 0 | +282 |
| ONGC.NS | 1 | 1 | 0 | +508 |
| TATASTEEL.NS | 1 | 1 | 0 | +800 |
| KOTAKBANK.NS | 1 | 0 | 1 | -600 |
| INFY.NS | 1 | 1 | 0 | +959 |
| HCLTECH.NS | 1 | 1 | 0 | +751 |
| BRITANNIA.NS | 1 | 1 | 0 | +468 |
| CIPLA.NS | 1 | 0 | 1 | -677 |

## Exit-reason breakdown

| Exit reason | n | % | avg net Rs |
|---|---:|---:|---:|
| target | 14 | 70% | +661 |
| stop_loss | 5 | 25% | -735 |
| force_close_eod | 1 | 5% | -176 |

## Confidence: wins vs losses

| | n | mean conf | min | max |
|---|---:|---:|---:|---:|
| Wins | 14 | 0.856 | 0.607 | 0.899 |
| Losses | 6 | 0.621 | 0.555 | 0.866 |

## Regime at entry

| Regime | n | n_wins | n_losses | net Rs |
|---|---:|---:|---:|---:|
| TRENDING_UP | 19 | 13 | 6 | +4,644 |
| SIDEWAYS | 1 | 1 | 0 | +751 |
