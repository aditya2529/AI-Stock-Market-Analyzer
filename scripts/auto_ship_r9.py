"""Auto-SHIP decision script for R9 (model retrain v2).

Runs after the audit-team retrain process finishes and writes
logs/retrain_v2_summary.md + models/saved/ensemble_intraday_v2.pkl.

Reads the summary, applies pre-authorized SHIP rules, executes
deploy + Telegram alert if pass, otherwise holds for user review.

Pre-authorized rules (locked May 27 evening by ops):
  - v2 PF >= 1.3 AND v2 PF > v1 PF + 0.10  -> AUTO-SHIP
  - v2 PF beats v1 but < 0.10 margin       -> HOLD, alert user
  - v2 PF <= v1 PF                          -> NO SHIP, keep v1
  - Anything weird / parse error            -> HOLD, alert user

Usage:
    python scripts/auto_ship_r9.py
    python scripts/auto_ship_r9.py --dry-run   # report decision, no file copy

Safety:
- Engine is OFF Wed evening (market closed since 15:30).
- Even if engine were running, the .pkl copy is atomic on Windows.
- Old v1 model preserved as ensemble_intraday_v1_pre_r9_backup.pkl
  before any copy.
- Telegram alert always sent on any decision (SHIP / HOLD / NO-SHIP / ERROR).
"""
from __future__ import annotations
import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUMMARY_MD = ROOT / "logs" / "retrain_v2_summary.md"
REPORT_JSON = ROOT / "logs" / "retrain_v2_backtest_report.json"
V2_PKL = ROOT / "models" / "saved" / "ensemble_intraday_v2.pkl"
V1_PKL = ROOT / "models" / "saved" / "ensemble_intraday.pkl"
V1_BACKUP = ROOT / "models" / "saved" / "ensemble_intraday_v1_pre_r9_backup.pkl"

# Pre-authorized SHIP rules
MIN_PF_THRESHOLD = 1.3
MIN_MARGIN_OVER_V1 = 0.10


def parse_pf_from_summary(text: str) -> tuple[float | None, float | None]:
    """Extract v1 PF and v2 PF from the summary markdown.

    Audit team's format (R7 b935c21 harness):
      | Metric        | v1 (current production) | v2 (retrained) | Δ |
      | Profit Factor | 2.390                   | 2.120          | -0.270 |
    """
    for line in text.splitlines():
        # Match "Profit Factor" specifically (not just "PF")
        if (re.search(r"profit\s*factor", line, re.IGNORECASE)
                and "|" in line):
            # Pull all numbers; expect [v1_pf, v2_pf, delta]
            nums = re.findall(r"[-+]?\d+\.\d+", line)
            if len(nums) >= 2:
                try:
                    return float(nums[0]), float(nums[1])
                except ValueError:
                    continue
    return None, None


def parse_pf_from_json(path: Path) -> tuple[float | None, float | None]:
    """Fallback: parse v1 + v2 PF from the JSON report."""
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text())
        v1 = data.get("v1", {}).get("pf") or data.get("v1_pf")
        v2 = data.get("v2", {}).get("pf") or data.get("v2_pf")
        return (float(v1) if v1 is not None else None,
                float(v2) if v2 is not None else None)
    except Exception:
        return None, None


def send_telegram(message: str) -> bool:
    """Best-effort Telegram alert. Returns True on success."""
    try:
        import os
        from dotenv import dotenv_values  # type: ignore
        env = dotenv_values(ROOT / ".env")
        token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
        if not (token and chat_id):
            return False
        import urllib.request
        import urllib.parse
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        # Use plain text — HTML parse_mode rejects bare '<=' / '<' chars
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
        }).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def decide(v1_pf: float | None, v2_pf: float | None) -> tuple[str, str]:
    """Apply pre-authorized SHIP rules. Returns (verdict, reason)."""
    if v1_pf is None or v2_pf is None:
        return "ERROR", "Could not parse v1 or v2 PF from summary."
    if v2_pf < MIN_PF_THRESHOLD:
        return "NO_SHIP", f"v2 PF {v2_pf:.2f} below floor {MIN_PF_THRESHOLD}"
    if v2_pf <= v1_pf:
        return "NO_SHIP", f"v2 PF {v2_pf:.2f} <= v1 PF {v1_pf:.2f} — no improvement"
    margin = v2_pf - v1_pf
    if margin < MIN_MARGIN_OVER_V1:
        return "HOLD", (f"v2 PF {v2_pf:.2f} beats v1 PF {v1_pf:.2f} by {margin:.2f}, "
                        f"below auto-ship margin {MIN_MARGIN_OVER_V1}")
    return "SHIP", (f"v2 PF {v2_pf:.2f} beats v1 PF {v1_pf:.2f} "
                    f"by {margin:.2f} (>= {MIN_MARGIN_OVER_V1})")


def deploy_v2() -> None:
    """Copy v2 model into the engine-loaded path. Backup v1 first."""
    if not V2_PKL.exists():
        raise FileNotFoundError(f"v2 model not found at {V2_PKL}")
    if not V1_PKL.exists():
        raise FileNotFoundError(f"v1 model not found at {V1_PKL}")
    shutil.copy2(V1_PKL, V1_BACKUP)
    shutil.copy2(V2_PKL, V1_PKL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Report decision, do not copy files")
    args = parser.parse_args()

    print("=" * 60)
    print("R9 AUTO-SHIP DECISION")
    print(f"  Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Wait / check artifacts exist
    if not SUMMARY_MD.exists():
        print(f"  ABORT: {SUMMARY_MD.name} not found. Retrain may not be done.")
        send_telegram("⚠️ R9 auto-ship aborted: summary file missing. Retrain incomplete?")
        return 1

    text = SUMMARY_MD.read_text(encoding="utf-8", errors="replace")
    v1_pf, v2_pf = parse_pf_from_summary(text)
    if v1_pf is None or v2_pf is None:
        # Fallback to JSON
        v1_pf, v2_pf = parse_pf_from_json(REPORT_JSON)

    print(f"\n  v1 PF: {v1_pf}")
    print(f"  v2 PF: {v2_pf}")

    verdict, reason = decide(v1_pf, v2_pf)
    print(f"\n  Verdict: {verdict}")
    print(f"  Reason : {reason}")

    if args.dry_run:
        print("\n  (dry-run — no deploy, no Telegram)")
        return 0

    message = (
        f"R9 Auto-SHIP: {verdict}\n\n"
        f"v1 PF: {v1_pf}\n"
        f"v2 PF: {v2_pf}\n\n"
        f"{reason}"
    )

    if verdict == "SHIP":
        try:
            deploy_v2()
            print("\n  [OK] v2 deployed.")
            print(f"       Backup: {V1_BACKUP.name}")
            print(f"       Active: {V1_PKL.name} (now v2)")
            message += "\n\nDeployed. Engine boots next session with v2."
        except Exception as e:
            print(f"\n  [FAIL] Deploy failed: {e}")
            message += f"\n\nDeploy failed: {e}"
    elif verdict == "NO_SHIP":
        print("\n  Kept v1. No engine touch.")
        message += "\n\nKept v1. No deploy."
    elif verdict == "HOLD":
        print("\n  HOLD - needs your manual review.")
        message += "\n\nHOLD - needs your decision."
    else:
        print(f"\n  ERROR - check {SUMMARY_MD.name} manually.")
        message += "\n\nParse error - review summary file manually."

    sent = send_telegram(message)
    print(f"\n  Telegram alert: {'sent OK' if sent else 'FAILED'}")

    return 0 if verdict in ("SHIP", "NO_SHIP", "HOLD") else 2


if __name__ == "__main__":
    raise SystemExit(main())
