"""Telegram alert sender using the Bot API (no polling — fire-and-forget)."""
import logging
import urllib.request
import urllib.parse
import json
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


def _is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a plain message to the configured Telegram chat. Returns True on success."""
    if not _is_configured():
        logger.debug("Telegram not configured — skipping message")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }).encode()
    try:
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
        return False


def format_signal_message(payload: dict) -> str:
    """Format a signal payload as an HTML Telegram message."""
    signal = payload.get("signal", "?")
    symbol = payload.get("symbol", "?")
    price  = payload.get("price", 0)
    conf   = payload.get("confidence", 0)
    sl     = payload.get("stop_loss", 0)
    tp     = payload.get("target", 0)
    rr     = payload.get("risk_reward", 0)
    regime = payload.get("regime", "?")
    shares = payload.get("shares", 0)
    reasons = payload.get("reasons", [])

    emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(signal, "⚪")
    regime_emoji = {
        "TRENDING_UP": "📈", "TRENDING_DOWN": "📉",
        "SIDEWAYS": "➡️", "HIGH_VOL": "⚡",
    }.get(regime, "❓")

    reason_lines = "\n".join(f"  • {r}" for r in reasons[:3]) if reasons else "  • (no reasons)"

    return (
        f"{emoji} <b>{signal} — {symbol}</b>\n"
        f"\n"
        f"💰 Price:      ₹{price:,.2f}\n"
        f"📊 Confidence: {conf:.0%}\n"
        f"🛡️ Stop-Loss:  ₹{sl:,.2f}\n"
        f"🎯 Target:     ₹{tp:,.2f}\n"
        f"⚖️ R:R:        {rr:.1f}x\n"
        f"{regime_emoji} Regime:    {regime}\n"
        f"📦 Shares:     {shares}\n"
        f"\n"
        f"<b>Why:</b>\n{reason_lines}"
    )


def format_trade_closed_message(trade: dict) -> str:
    """Format a closed-trade notification."""
    sym    = trade.get("symbol", "?")
    pnl    = trade.get("net_pnl", 0)
    ret    = trade.get("return_pct", 0)
    reason = trade.get("exit_reason", "?")
    emoji  = "✅" if pnl >= 0 else "❌"
    reason_labels = {"target": "🎯 Target hit", "stop_loss": "🛡️ Stop hit", "signal": "📡 Signal exit"}
    return (
        f"{emoji} <b>Trade Closed — {sym}</b>\n"
        f"\n"
        f"Net PnL:   ₹{pnl:+,.2f}\n"
        f"Return:    {ret:+.2%}\n"
        f"Exit:      {reason_labels.get(reason, reason)}"
    )


def format_portfolio_summary(state: dict) -> str:
    """Format a daily portfolio snapshot message."""
    cash   = state.get("cash", 0)
    eq     = state.get("open_equity", 0)
    total  = state.get("total_value", 0)
    dd     = state.get("drawdown_pct", 0)
    n_open = state.get("n_open", 0)
    return (
        f"📋 <b>Daily Portfolio Summary</b>\n"
        f"\n"
        f"💵 Cash:        ₹{cash:>12,.0f}\n"
        f"📈 Open Equity: ₹{eq:>12,.0f}\n"
        f"💼 Total:       ₹{total:>12,.0f}\n"
        f"📉 Drawdown:    {dd:.1%}\n"
        f"🔓 Open Trades: {n_open}"
    )


def format_engine_pulse(state: dict) -> str:
    """Format a periodic engine health pulse (every ~30 min during market hours)."""
    now    = state.get("now", "—")
    n_proc = state.get("symbols_processed", 0)
    n_open = state.get("open_positions", 0)
    new_t  = state.get("new_today", 0)
    closed = state.get("closed_today", 0)
    cash   = state.get("cash", 0)
    total  = state.get("total", 0)
    dd     = state.get("drawdown_pct", 0)
    last_actions = state.get("last_tick_actions", 0)
    ram_mb = state.get("ram_mb")
    latency = state.get("last_tick_seconds")
    extras = []
    if ram_mb is not None:
        extras.append(f"RAM: {ram_mb:.0f} MB")
    if latency is not None:
        extras.append(f"Tick: {latency:.0f}s")
    extra_line = " | ".join(extras)
    return (
        f"🤖 <b>Engine Pulse — {now} IST</b>\n"
        f"\n"
        f"Processed:  {n_proc} symbols last tick\n"
        f"Open:       {n_open}  |  New today: {new_t}  |  Closed today: {closed}\n"
        f"Cash:       ₹{cash:>10,.0f}\n"
        f"Total:      ₹{total:>10,.0f}\n"
        f"Drawdown:   {dd:.2%}\n"
        + (f"\n{extra_line}" if extra_line else "")
    )


def send_engine_pulse(state: dict) -> bool:
    return send_message(format_engine_pulse(state))


def send_signal_alert(payload: dict) -> bool:
    return send_message(format_signal_message(payload))


def send_trade_closed_alert(trade: dict) -> bool:
    return send_message(format_trade_closed_message(trade))


def send_portfolio_summary(state: dict) -> bool:
    return send_message(format_portfolio_summary(state))


def send_test_ping() -> bool:
    return send_message("🤖 <b>AI Stock Analyzer</b> — alert system connected ✓")
