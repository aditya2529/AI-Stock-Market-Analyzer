# Swing Trading Strategy Options
**Drafted:** May 21, 2026 · **For:** Future reference, not for immediate execution

---

## When this document matters

**NOT today, not this week, not until intraday is resolved.**

Per the Day-5 intraday playbook commitment, no second strategy gets built until:
1. Clean Day #5 reached for intraday
2. Intraday strategy verdict reached (keep / tweak / retire)
3. If retired or low-conviction → swing becomes a real conversation
4. If kept → swing is "diversification later," not "urgent now"

This file exists so when that moment arrives, the analysis is already done and you don't make the choice emotionally.

---

## The 5 honest swing strategy options

### 1. Trend Following on Daily Bars
**One sentence:** Buy when the 50-day moving average crosses above the 200-day moving average; exit when it crosses back.
**Trades/year:** 5-15 per stock
**Holding period:** 1-6 months
**Backtest PF range (published):** 1.4-1.8
**Pros:** Zero ML, decades of research backing it, simple
**Cons:** Late entries (miss first 20%), whipsaws in sideways markets
**Real-world analogy:** Changing lanes only when both your short-term mood AND your long-term route agree.

### 2. Breakout Swing — 52-Week High Momentum ⭐ (MY PICK)
**One sentence:** Buy stocks breaking above their 20-day or 52-week high; trail a stop loss UP as the price climbs.
**Trades/year:** 20-50 on a 25-stock universe
**Holding period:** 1-8 weeks
**Backtest PF range (published):** 1.6-2.2
**Pros:** Catches big trends, simple rules, no ML, highest PF of simple options
**Cons:** ~40-50% of breakouts fail, drawdowns in bear markets
**Real-world analogy:** Betting on a horse that just won — momentum tends to continue.

### 3. Mean Reversion on Daily
**One sentence:** Buy stocks in a long-term uptrend that just dropped 5-15% with RSI < 30; sell on bounce.
**Trades/year:** 30-60
**Holding period:** 3-15 days
**Backtest PF range:** 1.3-1.7
**Pros:** High win rate (60-70%), works in choppy markets, complementary to trend
**Cons:** Catches falling knives in real trend reversals (2008-style)
**Real-world analogy:** Buying a quality umbrella after a rainy week ends.

### 4. Sector Rotation
**One sentence:** Rank the ~10 NSE sector indices by recent performance, hold top 2-3, rebalance weekly.
**Trades/year:** 10-15
**Holding period:** weeks to months
**Backtest PF range:** 1.5-2.0
**Pros:** Catches macro themes, only 10 indices to track
**Cons:** Slow to react to regime change, needs sector index data
**Real-world analogy:** Sitting at the table with whoever's currently telling the best joke.

### 5. ML Swing (current architecture, retrained on daily bars)
**One sentence:** Retrain XGBoost + LSTM + HMM ensemble on daily bars + 5-day forward returns; same SL/TP logic scaled to daily ATR.
**Trades/year:** 30-60
**Holding period:** 3-15 days
**Pros:** Maximum infrastructure reuse, daily bars less noisy than 5-min
**Cons:** Inherits the same C-extension threading bugs that bit intraday (P33, P39, etc.); 2-3 weeks to retrain
**Real-world analogy:** Same weather forecaster but predicting weekly weather instead of 5-minute weather.

---

## Honest comparison matrix

| Criterion | Trend | Breakout | Mean Rev | Sector | ML Swing |
|---|---|---|---|---|---|
| Code complexity | Tiny | Small | Small | Small | Huge |
| Threading crash risk | Zero | Zero | Zero | Zero | Same as intraday |
| Trades/year | ~15 | 30 | 45 | 12 | 45 |
| Months to 30 trades | 24 | 8 | 6 | 30 | 6 |
| Reuses existing infra | Low | Medium | Medium | Low | **High** |
| Win rate (real-world) | 40-50% | 35-45% | 60-70% | 50-55% | TBD |
| Expected PF | 1.4-1.8 | 1.6-2.2 | 1.3-1.7 | 1.5-2.0 | TBD |
| Capital efficiency | Low | High | High | Medium | Medium |
| Solo-dev maintainability | High | High | High | High | Low |

---

## Why I recommend Breakout Swing (option 2)

1. **Simple enough to build in 2 weekends.** Rules-based, no ML, no training pipeline.
2. **Highest published PF (1.6-2.2)** of any simple swing approach.
3. **Long-only friendly** — no shorting needed.
4. **Reuses your data pipeline** — yfinance daily bars work as-is.
5. **Won't crash like intraday** — no SHAP, no xgboost in a thread, no curl_cffi races.
6. **Time-friendly** — one evening check per day, not 5-min monitoring.
7. **Fastest path to a 30-trade evaluation sample** (~8 months) of the simple options.

---

## Why I do NOT recommend ML Swing (option 5)

Same C-extension stack that crashed your engine three mornings this week (May 18-20). The threading bugs (P33, P39) don't go away by switching from 5-min to daily bars — they're library-level, not strategy-level. Building a second engine on the same fragile foundation = doubling your operational pain without doubling the edge.

**ML belongs in the strategy where it's already paid for itself** (intraday). Swing's lower trade frequency and longer holding period means the ML complexity adds less value while inheriting the same fragility cost.

---

## Sequence (when you eventually act on this file)

**Prerequisites — none of these are done yet:**
1. Intraday Day-5 verdict reached.
2. P27 (schema migration to add `strategy` column to `paper_positions`, `paper_trades`; separate cash buckets per strategy).
3. Capital allocation decision: e.g. ₹3L intraday + ₹2L swing.

**Build sequence:**
1. **Weekend 1:** Build the breakout-detection logic. Run it against historical daily bars (2-3 years) for the existing 25-stock universe. Confirm the strategy is actually profitable historically. Write the backtest.
2. **Weekend 2:** Build the engine wrapper. Reuse existing `paper_trading/executor.py` (`try_open`, `try_close`) with a swing-specific `_process_symbol_swing` function. Add EOD scheduled task (runs once at 15:30 IST after intraday's force-close).
3. **Week 3-4:** Paper trade with ₹50K micro-allocation. Watch for operational issues. Don't worry about P&L yet — just confirm engine boots, trades, exits cleanly.
4. **Month 2-4:** Collect ~30 swing trades. Compare PF to intraday lifetime PF.
5. **Decision at 30 swing trades:** Same playbook as Day-5 intraday. PF ≥ 1.5 keep; 1.2-1.5 tweak; <1.2 retire or try option 3 (mean reversion).

---

## What this is NOT

- Not a today-fix
- Not a substitute for finishing intraday properly
- Not a guarantee any swing strategy will hit PF > 1.5
- Not an excuse to abandon intraday in a moment of frustration

It's a written, well-analyzed answer to the question "if/when I add a second strategy, what is it?" so that decision is made with patience, not emotion.

---

## Meta-rule

Re-read the Day-5 intraday playbook commitment before opening this file. If intraday isn't resolved, this file is academic. **Discipline > strategy choice.**
