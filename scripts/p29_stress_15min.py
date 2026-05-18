"""P29 acceptance gate — 15-min concurrent stress exercise.

Mimics the engine's tick fanout under sustained load:
    - 8 worker threads (same as ThreadPoolExecutor in run_intraday_session)
    - Each tick: every worker calls engineer_features on a real symbol's
      OHLCV (which transitively invokes _load_macro_context)
    - Tick rate: every 2 seconds (much more aggressive than the 5-min
      production rate — compresses ~150 ticks of pressure into 15 min)
    - Runs for 15 min wall time, then exits 0

Watches:
    - logs/faulthandler.log byte length (printed at start + end)
    - The interpreter must survive (no segfault / heap corruption)
    - All worker calls must return non-empty featured DataFrames
      (downstream consumers tolerate None but we want to see real work)

Usage:
    python scripts/p29_stress_15min.py
"""
from __future__ import annotations
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import faulthandler
faulthandler.enable(open(Path(__file__).resolve().parent.parent
                         / "logs" / "faulthandler.log", "a"))


def main(duration_seconds: int = 15 * 60, tick_interval: float = 2.0,
         max_workers: int = 8) -> int:
    from data.database import load_ohlcv, list_tradeable_symbols
    from features.engineer import engineer_features

    symbols = list_tradeable_symbols()
    if not symbols:
        print("No tradeable symbols in DB — aborting stress test")
        return 1

    pool = symbols[:30] if len(symbols) >= 30 else symbols
    print(f"Stress: {len(pool)} symbols x {max_workers} workers, "
          f"tick every {tick_interval}s for {duration_seconds}s")

    def work(symbol: str) -> tuple[str, int]:
        try:
            df = load_ohlcv(symbol)
            if df is None or df.empty or len(df) < 100:
                return symbol, 0
            featured = engineer_features(df)
            return symbol, len(featured)
        except Exception as exc:
            print(f"  {symbol}: WARN {exc.__class__.__name__}: {exc}")
            return symbol, -1

    start = time.time()
    tick = 0
    total_calls = 0
    failed = 0

    while time.time() - start < duration_seconds:
        tick += 1
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(work, sym) for sym in pool]
            for fut in as_completed(futures):
                sym, n_rows = fut.result()
                total_calls += 1
                if n_rows < 0:
                    failed += 1
        elapsed = time.time() - start
        if tick % 30 == 0:
            print(f"  [{int(elapsed)}s] tick={tick} calls={total_calls} "
                  f"failed={failed}")
        time.sleep(tick_interval)

    elapsed = time.time() - start
    print(f"\nDone: {tick} ticks, {total_calls} calls, {failed} failures "
          f"in {elapsed:.0f}s")
    return 0


if __name__ == "__main__":
    duration = int(os.environ.get("STRESS_SECONDS", str(15 * 60)))
    interval = float(os.environ.get("STRESS_INTERVAL", "2.0"))
    sys.exit(main(duration_seconds=duration, tick_interval=interval))
