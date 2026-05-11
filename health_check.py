"""Morning health check — run at 08:55 IST before NSE open.

Validates:
  1. Ensemble model file exists and loads without error
  2. Feature engineering produces valid output
  3. Signal generation returns valid BUY/HOLD/SELL with confidence in [0,1]
  4. All 4 required fields present (price > 0, stop_loss, target, regime valid)

Sends Telegram alert with result. Exits 0 on pass, 1 on any failure.
"""
import sys
import logging
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")

VALID_SIGNALS = {"BUY", "HOLD", "SELL"}
VALID_REGIMES = {"TRENDING_UP", "TRENDING_DOWN", "SIDEWAYS", "HIGH_VOL", "UNKNOWN"}
CHECK_SYMBOLS = ["RELIANCE.NS", "HDFCBANK.NS", "TCS.NS"]


def run_health_check() -> dict:
    errors = []
    warnings = []
    results = {}

    # ── 1. Model file ────────────────────────────────────────────────────────
    try:
        from models.ensemble import Ensemble
        ensemble = Ensemble.load()
        results["model"] = "OK"
    except Exception as e:
        errors.append(f"Model load failed: {e}")
        return _build_report(errors, warnings, results)

    # ── 2. Signal checks for each symbol ────────────────────────────────────
    from data.ingestion import get_ohlcv
    from features.engineer import engineer_features
    from signals.generator import generate_signal

    signals_found = []
    for sym in CHECK_SYMBOLS:
        try:
            df = get_ohlcv(sym)
            feat = engineer_features(df)
            payload = generate_signal(sym, feat, ensemble)

            signal = payload.get("signal")
            conf = payload.get("confidence", -1)
            price = payload.get("price", -1)
            sl = payload.get("stop_loss", -1)
            tp = payload.get("target", -1)
            regime = payload.get("regime", "")
            latency = payload.get("latency_sec", 99)

            sym_errors = []
            if signal not in VALID_SIGNALS:
                sym_errors.append(f"invalid signal '{signal}'")
            if not (0.0 <= conf <= 1.0):
                sym_errors.append(f"confidence={conf:.4f} out of [0,1]")
            if price <= 0:
                sym_errors.append(f"price={price} not positive")
            if sl <= 0 or tp <= 0:
                sym_errors.append(f"sl={sl:.2f} or tp={tp:.2f} not positive")
            if regime not in VALID_REGIMES:
                sym_errors.append(f"unknown regime '{regime}'")
            if latency > 5:
                warnings.append(f"{sym} latency {latency:.1f}s > 5s")

            if sym_errors:
                errors.append(f"{sym}: {'; '.join(sym_errors)}")
            else:
                signals_found.append(signal)
                results[sym] = f"{signal} conf={conf:.0%} regime={regime} latency={latency:.2f}s"
        except Exception as e:
            errors.append(f"{sym}: exception — {e}")

    # ── 3. Diversity check — not ALL signals can be identical ────────────────
    if len(signals_found) == len(CHECK_SYMBOLS) and len(set(signals_found)) == 1:
        if signals_found[0] == "HOLD":
            # All HOLD could be valid (market closed, regime gate), just warn
            warnings.append("All checked symbols returned HOLD — regime gate may be active")
        # If all BUY or all SELL that would be suspicious but not impossible, just note it

    return _build_report(errors, warnings, results)


def _build_report(errors, warnings, results):
    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "results": results,
    }


def send_telegram_report(report: dict):
    try:
        from alerts.telegram_bot import send_message
    except ImportError:
        return

    ok = report["ok"]
    errors = report["errors"]
    warnings = report["warnings"]
    results = report["results"]

    if ok:
        lines = ["✅ <b>System Health: PASS</b> — NSE open in ~20 min\n"]
        for sym, detail in results.items():
            if sym == "model":
                continue
            lines.append(f"  • {sym}: {detail}")
        if warnings:
            lines.append("\n⚠️ Warnings:")
            for w in warnings:
                lines.append(f"  • {w}")
    else:
        lines = ["🔴 <b>SYSTEM HEALTH ALERT — Action may be needed</b>\n"]
        lines.append("<b>Errors:</b>")
        for e in errors:
            lines.append(f"  ❌ {e}")
        if results:
            lines.append("\n<b>Partial results:</b>")
            for sym, detail in results.items():
                if sym != "model":
                    lines.append(f"  • {sym}: {detail}")
        if warnings:
            lines.append("\n⚠️ Warnings:")
            for w in warnings:
                lines.append(f"  • {w}")

    send_message("\n".join(lines))


if __name__ == "__main__":
    report = run_health_check()

    # Print to stdout (visible in cron logs)
    status = "PASS" if report["ok"] else "FAIL"
    print(f"Health check: {status}")
    for sym, detail in report["results"].items():
        print(f"  {sym}: {detail}")
    if report["errors"]:
        print("Errors:")
        for e in report["errors"]:
            print(f"  ✗ {e}")
    if report["warnings"]:
        print("Warnings:")
        for w in report["warnings"]:
            print(f"  ! {w}")

    # Send Telegram
    send_telegram_report(report)

    sys.exit(0 if report["ok"] else 1)
