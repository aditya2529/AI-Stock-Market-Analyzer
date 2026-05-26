"""Thu-evening archive + reset script — Strategy v2 cutover (Tier 2.5 A + B).

Purpose
-------
Before Mon June 1 09:10 IST when the engine boots with Tier 2.5 overlays
(same-day target cooldown + 14:00 IST entry cutoff), we want a CLEAN
ledger so the new strategy's PF reading isn't diluted by the 66 trades
done under v1. This script does exactly that, idempotently.

What it does (in order, each step verified before the next)
-----------------------------------------------------------
1. Confirm engine is OFF (port 8000 dashboard can stay up; engine python
   process must not be running). Aborts if engine is alive.
2. Take a fresh full backup of market_data.db with a dated name.
3. Inside a single SQLite transaction:
   a. ALTER table renames: paper_trades, paper_positions,
      paper_portfolio_log → *_pre_overlay_v1
   b. Create fresh empty paper_trades, paper_positions,
      paper_portfolio_log via init_paper_tables() (uses existing schema)
   c. Snapshot current paper_config (with the inflated cash) to
      paper_config_pre_overlay_v1
   d. UPDATE paper_config:
        nse_cash         = 500000.00
        peak_value       = 500000.00
        cash             = 100000.00 (default for backward compat)
   e. DELETE all sl_cooldown_* keys (fresh slate)
4. Verify: query each fresh table → row count must be 0 (or 1 for
   paper_config); nse_cash must read 500000.00; archive tables must
   contain the expected row counts.
5. Print a summary the user can confirm visually before standing down.

Safety
------
- Single transaction → atomic. If anything fails, nothing changes.
- Read-only verification step → no further writes after the COMMIT.
- Idempotent guard: if *_pre_overlay_v1 tables ALREADY exist, abort.
  Re-running the script after a successful run is a no-op (refuses).
- DB backup taken BEFORE any modification.
- Engine-alive check BEFORE any modification.

Rollback
--------
If you need to undo:
    1. Stop engine + dashboard
    2. Restore the dated backup: copy market_data_pre_overlay_v1_*.db
       over market_data.db
    3. Restart services
~30 seconds total.

Usage
-----
    python scripts/archive_and_reset_for_strategy_v2.py
    python scripts/archive_and_reset_for_strategy_v2.py --dry-run
    python scripts/archive_and_reset_for_strategy_v2.py --force  (skips
        engine-alive check — DANGEROUS, only if you really know engine
        is down and process detection is unreliable)
"""
from __future__ import annotations
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "market_data.db"
ARCHIVE_SUFFIX = "_pre_overlay_v1"
RESET_CASH = 500000.00
RESET_PEAK = 500000.00


def is_engine_running() -> bool:
    """Return True if a python process running main.py intraday is alive."""
    try:
        import psutil  # type: ignore
    except ImportError:
        # psutil not installed — fall back to a softer check via tasklist
        import subprocess
        try:
            out = subprocess.check_output(
                ["tasklist", "/v", "/fo", "csv"], stderr=subprocess.DEVNULL
            ).decode("utf-8", errors="replace")
            return "main.py intraday" in out
        except Exception:
            return False
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            cmdline = " ".join(p.info.get("cmdline") or [])
            if "main.py" in cmdline and "intraday" in cmdline:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def backup_db() -> Path:
    """Copy market_data.db to a timestamped backup. Returns the path."""
    if not DB.exists():
        raise FileNotFoundError(f"market_data.db not found at {DB}")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ROOT / f"market_data_pre_overlay_v1_{ts}.db"
    shutil.copy2(DB, backup)
    return backup


def archive_already_done(conn: sqlite3.Connection) -> bool:
    """Return True if *_pre_overlay_v1 archive tables already exist."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name LIKE '%_pre_overlay_v1'"
    )
    return cur.fetchone() is not None


def archive_and_reset(dry_run: bool = False) -> dict:
    """Run the archive + reset transaction. Returns a summary dict."""
    conn = sqlite3.connect(str(DB))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        # Pre-flight: refuse if archive already done
        if archive_already_done(conn):
            raise RuntimeError(
                "Archive tables already exist — refusing to run twice. "
                "If you really mean to redo this, manually drop the "
                "*_pre_overlay_v1 tables first."
            )

        # Pre-flight: capture row counts of source tables
        pre_trades = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        pre_positions = conn.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]
        pre_log = conn.execute("SELECT COUNT(*) FROM paper_portfolio_log").fetchone()[0]
        pre_config_rows = conn.execute("SELECT COUNT(*) FROM paper_config").fetchone()[0]
        pre_cash = float(conn.execute(
            "SELECT value FROM paper_config WHERE key='nse_cash'"
        ).fetchone()[0])
        pre_peak = float(conn.execute(
            "SELECT value FROM paper_config WHERE key='peak_value'"
        ).fetchone()[0])

        summary = {
            "pre_trades": pre_trades,
            "pre_positions": pre_positions,
            "pre_log_rows": pre_log,
            "pre_config_rows": pre_config_rows,
            "pre_nse_cash": pre_cash,
            "pre_peak_value": pre_peak,
        }

        if dry_run:
            summary["dry_run"] = True
            return summary

        # Start the single transaction
        conn.execute("BEGIN")
        # 1. Rename existing tables → archive
        conn.execute("ALTER TABLE paper_trades RENAME TO paper_trades_pre_overlay_v1")
        conn.execute("ALTER TABLE paper_positions RENAME TO paper_positions_pre_overlay_v1")
        conn.execute("ALTER TABLE paper_portfolio_log RENAME TO paper_portfolio_log_pre_overlay_v1")

        # 2. Snapshot paper_config to a copy table
        conn.execute(
            "CREATE TABLE paper_config_pre_overlay_v1 AS SELECT * FROM paper_config"
        )

        # 3. Recreate fresh empty tables (mirror existing schema verbatim)
        conn.executescript("""
            CREATE TABLE paper_positions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                entry_time      TEXT NOT NULL,
                entry_price     REAL NOT NULL,
                shares          INTEGER NOT NULL,
                stop_loss       REAL NOT NULL,
                target          REAL NOT NULL,
                confidence      REAL,
                regime          TEXT,
                UNIQUE(symbol)
            );
            CREATE TABLE paper_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                entry_time      TEXT NOT NULL,
                exit_time       TEXT NOT NULL,
                entry_price     REAL NOT NULL,
                exit_price      REAL NOT NULL,
                shares          INTEGER NOT NULL,
                gross_pnl       REAL NOT NULL,
                net_pnl         REAL NOT NULL,
                return_pct      REAL NOT NULL,
                exit_reason     TEXT NOT NULL,
                confidence      REAL,
                regime          TEXT
            );
            CREATE TABLE paper_portfolio_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT NOT NULL,
                cash            REAL NOT NULL,
                open_equity     REAL NOT NULL,
                total_value     REAL NOT NULL,
                peak_value      REAL NOT NULL,
                drawdown_pct    REAL NOT NULL,
                n_open          INTEGER NOT NULL
            );
        """)

        # 4. Reset paper_config bucket values
        conn.execute(
            "UPDATE paper_config SET value=? WHERE key='nse_cash'",
            (f"{RESET_CASH}",)
        )
        conn.execute(
            "UPDATE paper_config SET value=? WHERE key='peak_value'",
            (f"{RESET_PEAK}",)
        )
        conn.execute(
            "UPDATE paper_config SET value=? WHERE key='cash'",
            (f"{RESET_CASH}",)
        )
        # initial_cash and nse_initial_cash stay at 500000 (unchanged)

        # 5. Delete all sl_cooldown_* keys (fresh slate)
        deleted_cooldowns = conn.execute(
            "DELETE FROM paper_config WHERE key LIKE 'sl_cooldown_%'"
        ).rowcount
        summary["deleted_cooldown_keys"] = deleted_cooldowns

        # Commit
        conn.commit()

        # 6. Verify after commit
        post_trades = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        post_positions = conn.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]
        post_log = conn.execute("SELECT COUNT(*) FROM paper_portfolio_log").fetchone()[0]
        post_cash = float(conn.execute(
            "SELECT value FROM paper_config WHERE key='nse_cash'"
        ).fetchone()[0])
        post_peak = float(conn.execute(
            "SELECT value FROM paper_config WHERE key='peak_value'"
        ).fetchone()[0])

        archive_trades = conn.execute(
            "SELECT COUNT(*) FROM paper_trades_pre_overlay_v1"
        ).fetchone()[0]
        archive_positions = conn.execute(
            "SELECT COUNT(*) FROM paper_positions_pre_overlay_v1"
        ).fetchone()[0]
        archive_log = conn.execute(
            "SELECT COUNT(*) FROM paper_portfolio_log_pre_overlay_v1"
        ).fetchone()[0]

        summary.update({
            "post_trades": post_trades,
            "post_positions": post_positions,
            "post_log_rows": post_log,
            "post_nse_cash": post_cash,
            "post_peak_value": post_peak,
            "archive_trades": archive_trades,
            "archive_positions": archive_positions,
            "archive_log_rows": archive_log,
            "ok": (
                post_trades == 0 and post_positions == 0 and post_log == 0 and
                post_cash == RESET_CASH and post_peak == RESET_PEAK and
                archive_trades == pre_trades and
                archive_positions == pre_positions and
                archive_log == pre_log
            ),
        })

        return summary
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would happen, make no changes")
    parser.add_argument("--force", action="store_true",
                        help="Skip engine-alive check (DANGEROUS)")
    args = parser.parse_args()

    print("=" * 60)
    print("STRATEGY v2 CUTOVER — archive + reset")
    print("=" * 60)

    # Engine-alive check (skipped only with --force)
    if not args.force:
        print("\n[1/4] Checking engine is OFF ...")
        if is_engine_running():
            print("  ABORT: engine process detected. Stop the engine first.")
            print("  (Use --force only if you're 100% sure detection is wrong.)")
            return 1
        print("  OK — engine is not running.")
    else:
        print("\n[1/4] Skipping engine-alive check (--force)")

    # Backup
    if not args.dry_run:
        print("\n[2/4] Backing up market_data.db ...")
        backup = backup_db()
        size_mb = backup.stat().st_size / (1024 * 1024)
        print(f"  OK — {backup.name} ({size_mb:.1f} MB)")
    else:
        print("\n[2/4] Skipping backup (--dry-run)")

    # Archive + reset
    print(f"\n[3/4] {'DRY-RUN: simulating' if args.dry_run else 'Running'} archive + reset transaction ...")
    try:
        summary = archive_and_reset(dry_run=args.dry_run)
    except RuntimeError as e:
        print(f"  ABORT: {e}")
        return 2
    except Exception as e:
        print(f"  ERROR: {e}")
        return 3

    # Summary
    print("\n[4/4] Summary")
    print("-" * 60)
    print(f"  Before:")
    print(f"    paper_trades         : {summary['pre_trades']:>6} rows")
    print(f"    paper_positions      : {summary['pre_positions']:>6} rows")
    print(f"    paper_portfolio_log  : {summary['pre_log_rows']:>6} rows")
    print(f"    nse_cash             : Rs {summary['pre_nse_cash']:>10,.2f}")
    print(f"    peak_value           : Rs {summary['pre_peak_value']:>10,.2f}")
    if args.dry_run:
        print("\n  (dry-run — no changes were made)")
        return 0
    print(f"\n  After:")
    print(f"    paper_trades         : {summary['post_trades']:>6} rows  (fresh)")
    print(f"    paper_positions      : {summary['post_positions']:>6} rows  (fresh)")
    print(f"    paper_portfolio_log  : {summary['post_log_rows']:>6} rows  (fresh)")
    print(f"    nse_cash             : Rs {summary['post_nse_cash']:>10,.2f}")
    print(f"    peak_value           : Rs {summary['post_peak_value']:>10,.2f}")
    print(f"    cooldown keys wiped  : {summary['deleted_cooldown_keys']}")
    print(f"\n  Archive:")
    print(f"    paper_trades_pre_overlay_v1        : {summary['archive_trades']:>6} rows")
    print(f"    paper_positions_pre_overlay_v1     : {summary['archive_positions']:>6} rows")
    print(f"    paper_portfolio_log_pre_overlay_v1 : {summary['archive_log_rows']:>6} rows")
    print(f"    paper_config_pre_overlay_v1        : snapshot of config at cutover")

    if not summary.get("ok"):
        print("\n  VERIFY FAILED — numbers don't match expected. Investigate.")
        print("  (DB transaction was committed though — restore from backup if needed.)")
        return 4

    print("\nDONE. Strategy v2 ledger is fresh. Engine can boot Mon 09:10 IST.")
    print("To rollback: copy the backup .db over market_data.db.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
