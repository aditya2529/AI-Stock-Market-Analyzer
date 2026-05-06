import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

# On Railway/cloud, set DATA_DIR env var to a persistent volume mount (e.g. /data)
# Locally it defaults to the project folder
_DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DB_PATH    = _DATA_DIR / "market_data.db"
MODELS_DIR = _DATA_DIR / "models" / "saved"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

# Default symbols to track (Yahoo Finance format for NSE)
# 25 stocks across 8 sectors — prevents the model from overfitting to any single sector's pattern
DEFAULT_SYMBOLS = [
    # IT (4)
    "TCS.NS", "INFY.NS", "WIPRO.NS", "HCLTECH.NS",
    # Banking & Finance (5)
    "HDFCBANK.NS", "ICICIBANK.NS", "KOTAKBANK.NS", "AXISBANK.NS", "SBIN.NS",
    # Energy & Oil (3)
    "RELIANCE.NS", "ONGC.NS", "BPCL.NS",
    # Auto (3)
    "MARUTI.NS", "M&M.NS", "BAJAJ-AUTO.NS",
    # Pharma (3)
    "SUNPHARMA.NS", "DRREDDY.NS", "CIPLA.NS",
    # FMCG (3)
    "HINDUNILVR.NS", "NESTLEIND.NS", "BRITANNIA.NS",
    # Metals & Mining (2)
    "TATASTEEL.NS", "HINDALCO.NS",
    # Telecom & Infra (2)
    "BHARTIARTL.NS", "LT.NS",
]

# Data settings
DATA_ADAPTER = os.getenv("DATA_ADAPTER", "yfinance")
DEFAULT_YEARS = 3          # years of historical data to fetch
DAILY_RESOLUTION = "1d"
INTRADAY_RESOLUTION = "1h"  # yfinance supports 1h for recent data

# Feature engineering
FEATURE_COLUMNS = [
    "rsi", "macd", "macd_signal", "macd_hist",
    "bb_upper", "bb_lower", "bb_mid",
    "atr", "vwap", "obv", "adx",
    "return_5", "return_10", "return_20",
    "volatility_10", "volatility_20",
    "hour_of_day", "day_of_week", "minutes_to_close",
    # Macro context — market-wide regime signals
    "nifty_return", "nifty_vs_ma20", "india_vix", "vix_zscore",
]

# Model training
LABEL_LOOKAHEAD = 5        # bars ahead to check for BUY/SELL label
BUY_THRESHOLD = 0.005      # +0.5% move → BUY label (lowered to increase signal frequency)
SELL_THRESHOLD = -0.005    # -0.5% move → SELL label
RANDOM_STATE = 42

# Walk-forward backtesting
WF_FOLDS = 5
WF_FOLD_MONTHS = 6
WF_STEP_MONTHS = 3
BROKERAGE_PCT = 0.0003     # 0.03%
SLIPPAGE_PCT = 0.001       # 0.10%

# ── Intraday settings ──────────────────────────────────────────────────
INTRADAY_RESOLUTION   = "5m"
INTRADAY_LOOKAHEAD    = 6          # 6 bars × 5 min = 30 min forward return
INTRADAY_BUY_THRESHOLD  = 0.003   # +0.3% in 30 min → BUY label
INTRADAY_SELL_THRESHOLD = -0.003  # -0.3% in 30 min → SELL label
INTRADAY_UNIVERSE_SIZE  = 50      # top N stocks picked each morning
INTRADAY_SIGNAL_INTERVAL = 5      # minutes between signal checks
INTRADAY_FORCE_CLOSE_TIME = (15, 15)  # 3:15 PM IST — close all before market end
INTRADAY_MAX_POSITIONS = 5        # max open positions at one time (risk control)

# Risk management
MAX_RISK_PCT = 0.01        # 1% of portfolio per trade
ATR_SL_MULTIPLIER = 1.0    # stop-loss = entry ± 1x ATR(14)
ATR_TARGET_MULTIPLIER = 2.0  # target = entry ± 2x ATR(14)
DAILY_LOSS_LIMIT_PCT = 0.03  # 3% daily loss limit

# Performance gates (Phase 1 exit criteria)
GATE_SHARPE = 1.5
GATE_WIN_RATE = 0.55
GATE_MAX_DRAWDOWN = 0.15
GATE_PROFIT_FACTOR = 1.3
GATE_SIGNAL_LATENCY_SEC = 1.0

# Alerts (filled from .env)
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")

ALERT_EMAIL_FROM      = os.getenv("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_PASSWORD  = os.getenv("ALERT_EMAIL_PASSWORD", "")
ALERT_EMAIL_TO        = os.getenv("ALERT_EMAIL_TO", "")
ALERT_EMAIL_SMTP_HOST = os.getenv("ALERT_EMAIL_SMTP_HOST", "smtp.gmail.com")
ALERT_EMAIL_SMTP_PORT = int(os.getenv("ALERT_EMAIL_SMTP_PORT", "587"))

ALERT_MIN_CONFIDENCE = float(os.getenv("ALERT_MIN_CONFIDENCE", "0.55"))
_sym_env = os.getenv("ALERT_SYMBOLS", "")
ALERT_SYMBOLS = [s.strip() for s in _sym_env.split(",") if s.strip()] if _sym_env else []

# Alpaca Markets — US stocks (NYSE / NASDAQ)
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET", "")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# Default US equity symbols (large-cap, liquid)
DEFAULT_US_SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "META", "TSLA", "JPM", "V", "JNJ",
]

# Default crypto symbols (Binance USDT pairs — no API key needed)
DEFAULT_CRYPTO_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
]

# Upstox (filled from .env)
UPSTOX_API_KEY = os.getenv("UPSTOX_API_KEY", "")
UPSTOX_API_SECRET = os.getenv("UPSTOX_API_SECRET", "")
UPSTOX_REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:8000/callback")
