"""Central alert dispatcher — paper trading engine calls this on every trade event.

Applies confidence threshold and symbol whitelist filters before firing alerts.
Both Telegram and email are attempted; failure of one does not block the other.
"""
import logging
from config import ALERT_MIN_CONFIDENCE, ALERT_SYMBOLS
from alerts import telegram_bot, email_alert

logger = logging.getLogger(__name__)


def _passes_filters(payload: dict) -> bool:
    """Return True if alert should fire for this signal payload."""
    conf = payload.get("confidence", 0.0)
    if conf < ALERT_MIN_CONFIDENCE:
        logger.debug("Alert suppressed: confidence %.2f < threshold %.2f", conf, ALERT_MIN_CONFIDENCE)
        return False
    if ALERT_SYMBOLS:
        sym = payload.get("symbol", "")
        if sym not in ALERT_SYMBOLS:
            logger.debug("Alert suppressed: %s not in ALERT_SYMBOLS whitelist", sym)
            return False
    return True


def on_signal(payload: dict):
    """Fire BUY/SELL signal alert. HOLD signals are silently dropped."""
    signal = payload.get("signal", "HOLD")
    if signal == "HOLD":
        return
    if not _passes_filters(payload):
        return
    sym = payload.get("symbol", "?")
    logger.info("Dispatching %s alert for %s (conf=%.2f)", signal, sym,
                payload.get("confidence", 0))
    tg_ok = telegram_bot.send_signal_alert(payload)
    em_ok = email_alert.send_signal_alert(payload)
    if not tg_ok and not em_ok:
        logger.warning("All alert channels failed for %s %s", signal, sym)


def on_trade_closed(trade: dict):
    """Fire trade-closed notification (no confidence filter — always send)."""
    sym = trade.get("symbol", "?")
    if ALERT_SYMBOLS and sym not in ALERT_SYMBOLS:
        return
    logger.info("Dispatching trade-closed alert for %s (pnl=%.2f)", sym, trade.get("net_pnl", 0))
    telegram_bot.send_trade_closed_alert(trade)
    email_alert.send_trade_closed_alert(trade)


def on_portfolio_snapshot(state: dict):
    """Send end-of-day portfolio summary (Telegram only — not spammy enough for email)."""
    telegram_bot.send_portfolio_summary(state)


def send_test_alerts():
    """Ping both channels to verify credentials are wired correctly."""
    tg = telegram_bot.send_test_ping()
    em = email_alert.send_test_ping()
    return {"telegram": tg, "email": em}
