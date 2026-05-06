# AI Stock Market Analyzer — Product Requirements Document
**Version:** 1.0  
**Date:** 2026-05-05  
**Status:** Approved for Development  
**Author:** Aditya  
**Build Mode:** Solo, ~1 hr/day  

---

## Table of Contents
1. Executive Summary
2. Problem Statement
3. Goals & Success Metrics
4. User Personas
5. Product Architecture
6. Feature Specifications
7. ML Model Design
8. Data Strategy
9. Validation Pipeline
10. Risk Management Engine
11. Testing Framework
12. API & Integration Layer
13. Go-to-Market Strategy
14. Compliance & Legal
15. Phased Roadmap
16. Open Questions & Risks

---

## 1. Executive Summary

### Vision
Build the world's most trustworthy AI-powered stock market decision system — combining institutional-grade machine learning, transparent risk management, and explainable signals — accessible to every trader from a Zerodha retail account to a prop trading desk.

### Elevator Pitch
> "Bloomberg Terminal intelligence for every trader. One platform that tells you not just WHAT to trade, but WHY, WHEN, and HOW MUCH — with built-in risk guardrails so you never blow up your account."

### Why Now
- Retail participation in NSE/BSE has grown 3x since 2020 (250M+ demat accounts)
- Global retail trading volume hit all-time highs post-pandemic
- Existing tools (TradingView, Zerodha Streak) provide indicators — not decisions
- LLM + ML breakthroughs make explainable AI signals viable at low cost
- SEBI's new algo-trading framework (2024) creates a regulated path to monetization

### Competitive Moat
1. **Explainable signals** — every trade recommendation shows its reasoning
2. **Regime-aware** — model knows when to trade and when to stay out
3. **Multi-market** — NSE/BSE + NYSE/NASDAQ + Crypto under one roof
4. **Risk-first design** — risk engine is not a feature, it IS the product

---

## 2. Problem Statement

### For Retail Traders
- Overwhelmed by indicators with no synthesis into actionable decisions
- No way to know if a signal is appropriate for current market regime
- Lose money not from bad signals but from bad position sizing and overtrading
- Tools are either too simple (basic screeners) or too complex (professional terminals)

### For Institutional / B2B
- Building proprietary signal infrastructure is expensive ($500K+ annually)
- Vendor signals come with no explainability — compliance teams reject black boxes
- No single vendor covers Indian + Global markets with unified API

### The Gap
There is no product that combines:
- Multi-market coverage
- Ensemble ML with regime detection
- Explainable outputs
- Built-in risk management
- Accessible pricing

---

## 3. Goals & Success Metrics

### Phase 1 Goals (MVP — Months 1–3)
| Goal | Metric | Target |
|------|--------|--------|
| Signal accuracy | Backtested win rate | > 55% |
| Model performance | Sharpe Ratio (backtested) | > 1.5 |
| Risk control | Max Drawdown (backtested) | < 15% |
| Latency | Signal generation time | < 1 second |
| Data coverage | Markets supported | NSE + BSE |

### Phase 2 Goals (Paper Trading — Months 4–5)
| Goal | Metric | Target |
|------|--------|--------|
| Live signal accuracy | Paper trade win rate | > 52% |
| System stability | Uptime | > 99.5% |
| Latency (live) | End-to-end signal latency | < 2 seconds |
| Paper trading duration | Minimum validation period | 30 days |

### Phase 3 Goals (Live + Monetization — Months 6–8)
| Goal | Metric | Target |
|------|--------|--------|
| Paying users | Subscriptions | 100 users by month 8 |
| Revenue | MRR | ₹50,000 by month 8 |
| Signal accuracy (live) | Live win rate | > 50% (net of slippage) |
| B2B pipeline | API clients | 2 pilot agreements |

---

## 4. User Personas

### Persona 1: Indian Intraday Retail Trader — "Rahul"
- **Age:** 28, software engineer in Bengaluru
- **Platform:** Zerodha, 2 years trading experience
- **Pain:** Makes impulsive trades, ignores stop-losses, loses ₹5,000–10,000/month
- **Goal:** Wants a system that tells him exactly what to buy, when to exit, and how much to risk
- **Tech comfort:** High — comfortable with apps, not with code
- **Willingness to pay:** ₹500–1,500/month if consistent signals

### Persona 2: Global Swing Trader — "Alex"
- **Age:** 35, freelancer in Canada
- **Platform:** Interactive Brokers, trades US stocks + crypto
- **Pain:** Misses entries, holds losers too long, no systematic risk rules
- **Goal:** Systematic edge, back-tested, with clear rules he can follow
- **Tech comfort:** Medium-high
- **Willingness to pay:** $20–50/month

### Persona 3: B2B Algo Desk — "Quantworks Capital"
- **Company:** Boutique prop trading firm, 5 traders
- **Pain:** Building internal signal infrastructure costs too much; needs reliable external signals with API access
- **Goal:** REST API with low latency signals, historical data, webhooks for execution
- **Tech comfort:** Very high — engineers on staff
- **Willingness to pay:** ₹25,000–75,000/month for API access

---

## 5. Product Architecture

### High-Level System Diagram
```
[Data Ingestion Layer]
    ├── Market Data Adapter (NSE/BSE via broker API)
    ├── Market Data Adapter (NYSE/NASDAQ via Alpaca/Polygon)
    ├── Market Data Adapter (Crypto via Binance/Coinbase)
    ├── News/Sentiment Feed (optional, Phase 2+)
    └── Options Chain Data (Phase 2+)
          ↓
[Data Processing Layer]
    ├── Data Validator (missing data, outliers, z-score filter)
    ├── Feature Engineering (OHLCV → indicators → ML features)
    └── Data Drift Monitor
          ↓
[ML Model Layer — Hybrid Ensemble]
    ├── Signal Layer: XGBoost/LightGBM (structured signals)
    ├── Sequence Layer: LSTM/Transformer (time-series trend)
    ├── Regime Layer: Hidden Markov Model (market condition)
    └── Meta Model: Logistic Regression (final calibration)
          ↓
[Risk Management Engine]
    ├── Position Sizer (Kelly Criterion / fixed fraction)
    ├── Stop-Loss Calculator
    ├── Daily Loss Limit Monitor
    └── Regime Gate (block signals in unfavorable regimes)
          ↓
[Explainability Layer]
    ├── SHAP feature importance
    └── Signal reason generation (natural language)
          ↓
[Output Layer]
    ├── REST API (B2B clients)
    ├── Web Dashboard (retail users)
    ├── Alerts (email, SMS, Telegram bot)
    └── Broker Webhook (direct execution, Phase 3)
```

### Tech Stack (Recommended)
| Layer | Technology |
|-------|-----------|
| Backend | Python (FastAPI) |
| ML Framework | scikit-learn, XGBoost, PyTorch |
| Data Storage | PostgreSQL + TimescaleDB (time-series) |
| Cache | Redis (real-time signal cache) |
| Task Queue | Celery + Redis |
| Frontend | React (dashboard) |
| Deployment | Docker + Railway/Render (Phase 1), AWS/GCP (Phase 3) |
| Monitoring | Prometheus + Grafana |

---

## 6. Feature Specifications

### Phase 1 Features (MVP)

#### F1.1 — Market Data Ingestion
- **Description:** Pull OHLCV data for NSE/BSE symbols at 1-minute and daily resolution
- **Acceptance Criteria:**
  - Connects to at least one Indian broker API (Zerodha Kite, AngelOne SmartAPI, or Upstox)
  - Handles market hours, holidays, and missing bars gracefully
  - Stores 2+ years of historical data per symbol
  - Forward-fills missing bars, logs anomalies
- **Priority:** P0

#### F1.2 — Feature Engineering Pipeline
- **Description:** Transform raw OHLCV into ML-ready features
- **Features to compute:**
  - RSI (14), MACD (12/26/9), Bollinger Bands (20/2), ATR (14)
  - VWAP, OBV, ADX
  - Rolling returns (5, 10, 20 bars), volatility (10, 20 bars)
  - Time features: hour of day, day of week, minutes to market close
- **Acceptance Criteria:**
  - All features computed in < 100ms per symbol
  - No lookahead bias in any feature
  - Unit tests for each indicator calculation
- **Priority:** P0

#### F1.3 — Hybrid Ensemble Model
- **Description:** Train and serve the 4-layer ensemble model
- **Acceptance Criteria:**
  - Walk-forward validation implemented (minimum 5 folds)
  - Train/test split is time-respecting (no shuffling)
  - Backtested Sharpe > 1.5, win rate > 55%, max drawdown < 15%
  - Model serialized and loadable in < 2 seconds
- **Priority:** P0

#### F1.4 — Backtesting Engine
- **Description:** Simulate historical performance with realistic assumptions
- **Acceptance Criteria:**
  - Applies 0.03% brokerage + 0.1% slippage assumption per trade
  - Generates: equity curve, Sharpe ratio, max drawdown, CAGR, win rate, profit factor
  - Walk-forward report (not just single backtest)
  - PDF/HTML report export
- **Priority:** P0

#### F1.5 — Signal Output (Internal)
- **Description:** Generate BUY/SELL/HOLD signal with confidence score and explanation
- **Output format:**
  ```json
  {
    "symbol": "RELIANCE.NSE",
    "signal": "BUY",
    "confidence": 0.74,
    "price": 2845.50,
    "stop_loss": 2810.00,
    "target": 2920.00,
    "risk_reward": 2.1,
    "regime": "TRENDING_UP",
    "reasons": ["RSI oversold recovery", "MACD bullish crossover", "Above VWAP"],
    "timestamp": "2026-05-05T09:31:00+05:30"
  }
  ```
- **Acceptance Criteria:**
  - Signal generated within 1 second of bar close
  - SHAP values computed and mapped to human-readable reasons
  - Regime label always present
- **Priority:** P0

### Phase 2 Features (Paper Trading + Multi-Market)

#### F2.1 — Paper Trading Engine
- **Description:** Simulate live trading without real money
- **Acceptance Criteria:**
  - Subscribes to live market feed
  - Executes signals at next available bar open (realistic fill)
  - Tracks P&L, position, drawdown in real time
  - Dashboard shows paper portfolio performance
  - Minimum 30-day paper trading gate before Phase 3
- **Priority:** P0

#### F2.2 — Global Market Adapter
- **Description:** Extend data ingestion to NYSE/NASDAQ and crypto
- **Data sources:** Alpaca Markets API (US stocks), Binance/Coinbase API (crypto)
- **Acceptance Criteria:**
  - Same feature pipeline works for all markets
  - Separate model trained/validated per market
  - Currency normalization (USD ↔ INR)
- **Priority:** P1

#### F2.3 — Alert System
- **Description:** Notify users when signals trigger
- **Channels:** Email, Telegram bot, in-app notification
- **Acceptance Criteria:**
  - Alert delivered within 30 seconds of signal generation
  - User can configure: markets, min confidence threshold, signal type (BUY/SELL/both)
  - Alert includes full signal payload
- **Priority:** P1

#### F2.4 — Web Dashboard (Retail)
- **Description:** React-based dashboard for signal viewing
- **Pages:**
  - Signal feed (live + historical)
  - Portfolio (paper or live performance)
  - Market regime indicator
  - Explainability panel (per signal)
  - Settings
- **Priority:** P1

### Phase 3 Features (Live + Monetization)

#### F3.1 — REST API (B2B)
- See Section 12 for full API spec.
- **Priority:** P0 for Phase 3

#### F3.2 — Subscription & Billing
- **Integration:** Razorpay (India), Stripe (global)
- **Tiers:** Free / Pro / Elite (see Section 13)
- **Priority:** P0 for Phase 3

#### F3.3 — Broker Webhook (Execution)
- **Description:** Send approved signals directly to broker for auto-execution
- **Brokers:** Zerodha (Kite Connect), AngelOne (Phase 3 only, opt-in)
- **Safety gates:** User must explicitly enable; daily loss limit enforced by system
- **Priority:** P2 for Phase 3

---

## 7. ML Model Design

### 7.1 Architecture — Hybrid Ensemble

| Layer | Model | Purpose | Library |
|-------|-------|---------|---------|
| Signal Layer | XGBoost / LightGBM | Structured feature signals | xgboost, lightgbm |
| Sequence Layer | LSTM or Temporal Fusion Transformer | Time-series pattern learning | PyTorch |
| Regime Layer | Hidden Markov Model | Market condition detection | hmmlearn |
| Meta Model | Logistic Regression | Final signal calibration | scikit-learn |

**Ensemble output:** Weighted average of layer outputs, weights learned by meta model.

### 7.2 Training Protocol

1. **Data split:** Chronological. Never shuffle. Last 20% = test set (never touched during training).
2. **Walk-forward validation:** 5 folds minimum. Fold size = 6 months. Step = 3 months.
3. **Regularization:** L1/L2 on all models. Early stopping on LSTM (patience=10 epochs).
4. **Feature pruning:** Remove features with SHAP importance < 0.01.
5. **Retraining frequency:** Monthly retraining on rolling 3-year window.

### 7.3 Overfitting Controls
| Risk | Detection Method | Action |
|------|-----------------|--------|
| Overfitting | Train vs test metric divergence > 10% | Increase regularization, reduce features |
| Underfitting | Both train and test accuracy < 50% | Add features, try deeper model |
| Regime mismatch | Live performance drops > 5% vs paper | Trigger regime gate, pause signals |
| Data drift | Feature distribution shift (KS test, p < 0.05) | Alert, retrain |

### 7.4 Model Performance Gates (must pass before go-live)
- Sharpe Ratio > 1.5 (walk-forward average)
- Win Rate > 55% (gross)
- Max Drawdown < 15%
- Profit Factor > 1.3
- Consistent across at least 3 different market regimes

---

## 8. Data Strategy

### 8.1 Data Sources

| Source | Data Type | Resolution | Market |
|--------|-----------|-----------|--------|
| Zerodha Kite / AngelOne | OHLCV | 1-min, daily | NSE/BSE |
| Alpaca Markets | OHLCV | 1-min, daily | NYSE/NASDAQ |
| Binance API | OHLCV | 1-min | Crypto |
| NSE India (free) | Options chain | EOD | NSE |
| NewsAPI / Aylien (Phase 2) | News sentiment | Realtime | All |

### 8.2 Data Quality Framework

| Issue | Detection | Resolution |
|-------|-----------|-----------|
| Missing bars | Gap detection | Forward-fill (max 3 bars), flag longer gaps |
| Price outliers | Z-score > 4 on returns | Flag, use prior bar value |
| Zero volume bars | Volume = 0 check | Exclude from training, not from price series |
| Stale data | Timestamp lag > 5 min | Alert + fallback to cached value |
| Data drift | KS test on feature distributions monthly | Retrain trigger |

### 8.3 Data Storage Schema (TimescaleDB)
```sql
CREATE TABLE ohlcv (
  time        TIMESTAMPTZ NOT NULL,
  symbol      TEXT NOT NULL,
  market      TEXT NOT NULL,  -- 'NSE', 'NYSE', 'CRYPTO'
  open        NUMERIC(18,4),
  high        NUMERIC(18,4),
  low         NUMERIC(18,4),
  close       NUMERIC(18,4),
  volume      BIGINT
);
SELECT create_hypertable('ohlcv', 'time');
CREATE INDEX ON ohlcv (symbol, time DESC);
```

---

## 9. Validation Pipeline

### 9.1 Three-Stage Gate System

```
Stage 1: Backtesting
├── Minimum 3 years of historical data
├── Walk-forward validation (5 folds)
├── Must pass all 5 performance gates (Section 7.4)
└── PASS → proceed to Stage 2

Stage 2: Paper Trading
├── Minimum 30 days (target 60 days)
├── Live market feed, simulated execution
├── Win rate must stay > 52% live
├── No regime gate violations
└── PASS → proceed to Stage 3

Stage 3: Controlled Live Testing
├── Maximum 1% capital per trade
├── Maximum 3% daily portfolio loss limit
├── 30-day observation period with real money
├── Manual review of every loss > ₹5,000
└── PASS → full deployment
```

### 9.2 Regime Detection States
| Regime | Description | Model Action |
|--------|-------------|-------------|
| TRENDING_UP | Strong uptrend, ADX > 25, price > 50MA | Full signal strength |
| TRENDING_DOWN | Strong downtrend | Short signals only (if enabled) |
| SIDEWAYS | Low volatility, ADX < 20 | Reduced position size, tighter SL |
| HIGH_VOL | VIX spike or ATR > 2x baseline | Block signals (regime gate) |
| UNKNOWN | Insufficient data | Block signals |

---

## 10. Risk Management Engine

### 10.1 Per-Trade Rules
- **Max position size:** 2% of portfolio per trade (configurable, default 1%)
- **Stop-loss:** Mandatory. Set at 1x ATR(14) from entry. Cannot be removed.
- **Target:** 2x ATR(14) minimum risk-reward ratio enforced
- **Concentration:** Max 20% in any single sector, max 40% in any single market

### 10.2 Daily Rules
- **Daily loss limit:** 3% of portfolio. System locks new signals for the day if breached.
- **Max open positions:** 5 concurrent (Phase 1), 10 (Phase 3)
- **Margin call protection:** Alert at 80% margin utilization, block new positions at 90%

### 10.3 Position Sizing Formula
```
Position Size = (Portfolio Value × Risk %) / (Entry Price - Stop Loss Price)
Example:
  Portfolio = ₹100,000
  Risk % = 1% → Risk Amount = ₹1,000
  Entry = ₹500, SL = ₹490 → Risk per share = ₹10
  Shares = ₹1,000 / ₹10 = 100 shares
```

### 10.4 User Override Policy
- Users CAN widen targets but CANNOT remove stop-losses
- Users CAN reduce position size but CANNOT increase beyond system maximum
- All overrides are logged and flagged in the dashboard

---

## 11. Testing Framework

### 11.1 Test Layers

| Layer | What is Tested | Tool |
|-------|---------------|------|
| Unit tests | Each indicator, each signal rule, risk calculator | pytest |
| Integration tests | Full pipeline: data → model → signal output | pytest + test DB |
| Latency tests | End-to-end signal generation time | pytest + time assertions |
| Stress tests | High-volatility simulation (March 2020, Oct 2008 data) | Custom harness |
| A/B tests | Model A vs Model B on paper trading | Shadow mode deployment |

### 11.2 Performance Thresholds
| Metric | Threshold | Test Frequency |
|--------|-----------|---------------|
| Signal latency | < 1 second | Every deploy |
| API response time | < 200ms (p99) | Every deploy |
| Sharpe Ratio | > 1.5 | Monthly |
| Max Drawdown | < 15% | Monthly |
| Win Rate | > 55% backtested, > 52% live | Monthly |
| Uptime | > 99.5% | Continuous |

### 11.3 CI/CD Gate
- All unit tests must pass before any code merges
- Backtest report auto-generated on model retrain
- Performance regression alert if any metric drops > 5% vs last run

---

## 12. API & Integration Layer

### 12.1 REST API Endpoints

#### Authentication
```
POST /auth/token          — Get API token
POST /auth/refresh        — Refresh token
```

#### Signals
```
GET  /signals             — Latest signals (all markets)
GET  /signals/{symbol}    — Latest signal for symbol
GET  /signals/history     — Historical signals with filters
```

#### Portfolio (Paper & Live)
```
GET  /portfolio           — Current positions + P&L
GET  /portfolio/history   — Trade history
```

#### Market Data
```
GET  /market/{symbol}/ohlcv?from=&to=&resolution=   — OHLCV data
GET  /market/{symbol}/regime                         — Current regime
```

#### Webhooks (B2B)
```
POST /webhooks/register   — Register URL to receive signal events
DELETE /webhooks/{id}     — Remove webhook
```

### 12.2 Signal Webhook Payload
```json
{
  "event": "signal.generated",
  "timestamp": "2026-05-05T09:31:05Z",
  "data": {
    "symbol": "RELIANCE.NSE",
    "signal": "BUY",
    "confidence": 0.74,
    "price": 2845.50,
    "stop_loss": 2810.00,
    "target": 2920.00,
    "risk_reward": 2.1,
    "regime": "TRENDING_UP",
    "reasons": ["RSI oversold recovery", "MACD bullish crossover", "Above VWAP"],
    "position_size_pct": 1.0
  }
}
```

### 12.3 Rate Limits
| Tier | Requests/minute | Webhook events/day |
|------|----------------|-------------------|
| Free | 10 | 0 |
| Pro | 100 | 500 |
| Elite | 1,000 | Unlimited |

---

## 13. Go-to-Market Strategy

### 13.1 Pricing Tiers

| Tier | Price | Features |
|------|-------|---------|
| **Free** | ₹0 / $0 | NSE signals, 1-day delay, 5 symbols, no alerts |
| **Pro** | ₹999/mo / $15/mo | All markets, real-time signals, 50 symbols, email alerts, Telegram bot |
| **Elite** | ₹2,999/mo / $40/mo | Unlimited symbols, API access, webhook, backtesting UI, priority support |
| **B2B API** | Custom | Full API, dedicated support, SLA, custom model training |

### 13.2 Launch Strategy
1. **Month 1–3 (Private Beta):** 10 hand-picked beta users from trading communities (Twitter, Reddit r/IndiaInvestments, Trading Q&A). Free access for 60-day feedback.
2. **Month 4–5 (Paper Trading Phase):** Open waitlist. Show paper trading track record publicly. Build trust.
3. **Month 6 (Public Launch):** Product Hunt launch. Free tier drives top-of-funnel. Pro tier converts serious traders.
4. **Month 7–8 (B2B Outreach):** Target boutique prop firms and fintech companies via LinkedIn.

### 13.3 Key Differentiators vs Competition
| Feature | This Product | TradingView | Zerodha Streak | Bloomberg |
|---------|-------------|------------|---------------|-----------|
| AI signals | Yes | No | No | Partial |
| Explainable AI | Yes | No | No | No |
| Regime detection | Yes | No | No | Yes |
| Multi-market | Yes | Yes | No | Yes |
| Built-in risk engine | Yes | No | Partial | Yes |
| Price | ₹0–2,999 | ₹0–15,000 | ₹0–2,000 | ₹5L+/year |

---

## 14. Compliance & Legal

### 14.1 SEBI (India)
- The product provides **educational signals and analysis**, NOT registered investment advice
- All pages must display: *"Past performance is not indicative of future results. Trading involves substantial risk of loss. This platform is not SEBI-registered investment advice."*
- Must NOT claim: "guaranteed returns", "risk-free profits", "100% accuracy"
- If revenue exceeds ₹25 lakh from advisory services, SEBI RIA registration required (plan for this in Year 2)

### 14.2 USA (SEC / FINRA)
- Signals for US markets must include: *"This is not financial advice. Not registered with the SEC."*
- Ensure no personalized portfolio management — signals are generic market analysis

### 14.3 Data Privacy
- No user trading account data stored on platform servers (broker API tokens stored encrypted, AES-256)
- GDPR-compliant for European users (data deletion on request)
- No selling of user data to third parties

---

## 15. Phased Roadmap

> Designed for 1 hr/day solo development. Each phase has shippable milestones.

### Phase 1 — MVP (Months 1–3, ~90 hours)

| Week | Milestone | Deliverable |
|------|-----------|------------|
| 1–2 | Data pipeline | NSE OHLCV ingestion + TimescaleDB setup |
| 3–4 | Feature engineering | All 15 features computed, unit tested |
| 5–7 | Model training | XGBoost Signal Layer + backtesting engine |
| 8–9 | Ensemble + HMM | Regime detection + meta model |
| 10–11 | Signal output | JSON signal generation + SHAP explainability |
| 12 | Phase 1 review | Walk-forward report, performance gate check |

**Phase 1 Exit Criteria:** All 5 performance gates passed (Section 7.4).

### Phase 2 — Paper Trading + Multi-Market (Months 4–5, ~60 hours)

| Week | Milestone | Deliverable |
|------|-----------|------------|
| 13–14 | Paper trading engine | Simulated execution, live P&L dashboard |
| 15–16 | Alert system | Telegram bot + email alerts |
| 17–18 | Global markets | NYSE/NASDAQ + Crypto adapters |
| 19–20 | Web dashboard | React frontend (signal feed + portfolio) |

**Phase 2 Exit Criteria:** 30+ days paper trading with win rate > 52%.

### Phase 3 — Live + Monetization (Months 6–8, ~90 hours)

| Week | Milestone | Deliverable |
|------|-----------|------------|
| 21–22 | REST API | FastAPI endpoints + auth |
| 23–24 | Billing | Razorpay + Stripe integration |
| 25–26 | B2B webhooks | Webhook system + client docs |
| 27–28 | Public launch | Product Hunt, waitlist conversion |
| 29–32 | B2B outreach | Pilot agreements, custom SLAs |

**Phase 3 Exit Criteria:** 100 paying users, ₹50,000 MRR, 2 B2B pilots.

---

## 16. Open Questions & Risks

### Known Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Regime shift failure (model trained on trending, fails sideways) | High | High | HMM regime gate blocks signals in unfavorable regimes |
| Slippage reality (paper ≠ live execution) | High | Medium | Apply 0.1% slippage assumption in all backtests; extended paper trading |
| User behavior (ignores SL, blames system) | High | Medium | User education layer; cannot remove SL in system; disclaimer on every signal |
| API dependency (broker API changes) | Medium | High | Abstract broker adapter layer; support 2+ brokers per market |
| SEBI regulatory change | Low | High | Monitor SEBI circulars; legal review before launch |
| Model decay (performance degrades over months) | Medium | High | Monthly retraining + drift detection alerts |

### Open Questions
1. Which Indian broker API to start with? (Zerodha Kite vs AngelOne SmartAPI vs Upstox)
2. Host on Railway/Render (simple, cheap) or AWS from day one? (recommendation: Railway for Phase 1, migrate Phase 3)
3. Should paper trading results be public (social proof) or private?
4. LSTM vs Temporal Fusion Transformer — TFT is better but slower to train; evaluate in Phase 1.

---

*This document is the authoritative source of truth for the AI Stock Market Analyzer. All implementation decisions should trace back to requirements in this PRD.*
