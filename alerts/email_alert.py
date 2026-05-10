"""Email alert sender via SMTP (Gmail by default, any STARTTLS host works)."""
from __future__ import annotations
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from config import (
    ALERT_EMAIL_FROM, ALERT_EMAIL_PASSWORD,
    ALERT_EMAIL_TO, ALERT_EMAIL_SMTP_HOST, ALERT_EMAIL_SMTP_PORT,
)

logger = logging.getLogger(__name__)


def _is_configured() -> bool:
    return bool(ALERT_EMAIL_FROM and ALERT_EMAIL_PASSWORD and ALERT_EMAIL_TO)


def send_email(subject: str, html_body: str) -> bool:
    """Send an HTML email. Returns True on success."""
    if not _is_configured():
        logger.debug("Email not configured — skipping")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = ALERT_EMAIL_FROM
    msg["To"] = ALERT_EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP(ALERT_EMAIL_SMTP_HOST, ALERT_EMAIL_SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(ALERT_EMAIL_FROM, ALERT_EMAIL_PASSWORD)
            server.sendmail(ALERT_EMAIL_FROM, ALERT_EMAIL_TO, msg.as_string())
        return True
    except Exception as e:
        logger.warning("Email send failed: %s", e)
        return False


def _signal_html(payload: dict) -> tuple[str, str]:
    signal  = payload.get("signal", "?")
    symbol  = payload.get("symbol", "?")
    price   = payload.get("price", 0)
    conf    = payload.get("confidence", 0)
    sl      = payload.get("stop_loss", 0)
    tp      = payload.get("target", 0)
    rr      = payload.get("risk_reward", 0)
    regime  = payload.get("regime", "?")
    shares  = payload.get("shares", 0)
    reasons = payload.get("reasons", [])

    color = {"BUY": "#27ae60", "SELL": "#e74c3c", "HOLD": "#95a5a6"}.get(signal, "#95a5a6")
    subject = f"[{signal}] {symbol} @ ₹{price:,.2f} — AI Stock Analyzer"

    reason_rows = "".join(f"<li>{r}</li>" for r in reasons[:3])
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:520px;margin:auto">
      <div style="background:{color};color:#fff;padding:16px 24px;border-radius:8px 8px 0 0">
        <h2 style="margin:0">{signal} — {symbol}</h2>
      </div>
      <div style="background:#f9f9f9;padding:20px 24px;border:1px solid #ddd;border-top:none">
        <table style="width:100%;border-collapse:collapse">
          <tr><td style="padding:6px 0;color:#555">Price</td>
              <td style="text-align:right;font-weight:bold">₹{price:,.2f}</td></tr>
          <tr><td style="padding:6px 0;color:#555">Confidence</td>
              <td style="text-align:right">{conf:.0%}</td></tr>
          <tr><td style="padding:6px 0;color:#555">Stop-Loss</td>
              <td style="text-align:right;color:#e74c3c">₹{sl:,.2f}</td></tr>
          <tr><td style="padding:6px 0;color:#555">Target</td>
              <td style="text-align:right;color:#27ae60">₹{tp:,.2f}</td></tr>
          <tr><td style="padding:6px 0;color:#555">Risk:Reward</td>
              <td style="text-align:right">{rr:.1f}x</td></tr>
          <tr><td style="padding:6px 0;color:#555">Regime</td>
              <td style="text-align:right">{regime}</td></tr>
          <tr><td style="padding:6px 0;color:#555">Shares</td>
              <td style="text-align:right">{shares}</td></tr>
        </table>
        <hr style="margin:16px 0;border:none;border-top:1px solid #eee">
        <p style="margin:0 0 8px;color:#555;font-weight:bold">Why this signal:</p>
        <ul style="margin:0;padding-left:20px;color:#333">{reason_rows}</ul>
      </div>
      <div style="padding:12px 24px;background:#eee;border-radius:0 0 8px 8px;
                  font-size:12px;color:#888;text-align:center">
        AI Stock Analyzer — paper trading only, not financial advice
      </div>
    </body></html>
    """
    return subject, html


def _trade_closed_html(trade: dict) -> tuple[str, str]:
    sym    = trade.get("symbol", "?")
    pnl    = trade.get("net_pnl", 0)
    ret    = trade.get("return_pct", 0)
    reason = trade.get("exit_reason", "?")
    color  = "#27ae60" if pnl >= 0 else "#e74c3c"
    label  = {"target": "Target hit", "stop_loss": "Stop hit", "signal": "Signal exit"}.get(reason, reason)
    subject = f"[CLOSED] {sym} ₹{pnl:+,.2f} ({ret:+.2%}) — AI Stock Analyzer"
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:520px;margin:auto">
      <div style="background:{color};color:#fff;padding:16px 24px;border-radius:8px 8px 0 0">
        <h2 style="margin:0">Trade Closed — {sym}</h2>
      </div>
      <div style="background:#f9f9f9;padding:20px 24px;border:1px solid #ddd;border-top:none">
        <table style="width:100%;border-collapse:collapse">
          <tr><td style="padding:6px 0;color:#555">Net PnL</td>
              <td style="text-align:right;font-weight:bold;color:{color}">₹{pnl:+,.2f}</td></tr>
          <tr><td style="padding:6px 0;color:#555">Return</td>
              <td style="text-align:right;color:{color}">{ret:+.2%}</td></tr>
          <tr><td style="padding:6px 0;color:#555">Exit Reason</td>
              <td style="text-align:right">{label}</td></tr>
        </table>
      </div>
    </body></html>
    """
    return subject, html


def send_signal_alert(payload: dict) -> bool:
    subject, html = _signal_html(payload)
    return send_email(subject, html)


def send_trade_closed_alert(trade: dict) -> bool:
    subject, html = _trade_closed_html(trade)
    return send_email(subject, html)


def send_test_ping() -> bool:
    return send_email(
        "AI Stock Analyzer — alert system connected",
        "<html><body><p>✅ Email alerts are working correctly.</p></body></html>",
    )
