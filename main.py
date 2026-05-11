"""AI Stock Market Analyzer — CLI entry point.

Commands:
    fetch     — Download OHLCV data and store in SQLite
    features  — Compute features and show a sample
    train     — Train the full ensemble model
    backtest  — Run walk-forward backtest and print performance gates
    signal    — Generate a live signal for the latest bar

Usage examples:
    python main.py fetch --symbol RELIANCE.NS --years 3
    python main.py features --symbol RELIANCE.NS
    python main.py train --symbol RELIANCE.NS
    python main.py backtest --symbol RELIANCE.NS
    python main.py signal --symbol RELIANCE.NS
"""

import argparse
import json
import logging
import sys
sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

from config import (
    DEFAULT_SYMBOLS, DEFAULT_US_SYMBOLS, DEFAULT_CRYPTO_SYMBOLS,
    GATE_SHARPE, GATE_WIN_RATE, GATE_MAX_DRAWDOWN, GATE_PROFIT_FACTOR,
)


# ── Command handlers ────────────────────────────────────────────────────────────

def cmd_fetch(args):
    from data.ingestion import fetch_and_store
    if args.symbol:
        symbols = [s.strip() for s in args.symbol.split(",")]
    elif args.market == "us":
        symbols = DEFAULT_US_SYMBOLS
    elif args.market == "crypto":
        symbols = DEFAULT_CRYPTO_SYMBOLS
    else:
        symbols = DEFAULT_SYMBOLS
    for sym in symbols:
        try:
            df = fetch_and_store(sym.strip(), years=args.years, resolution=args.resolution)
            print(f"✓ {sym}: {len(df)} bars fetched and stored.")
        except Exception as e:
            print(f"✗ {sym}: {e}")


def cmd_features(args):
    from data.ingestion import get_ohlcv
    from features.engineer import engineer_features
    df = get_ohlcv(args.symbol, resolution=args.resolution)
    featured = engineer_features(df)
    print(f"\n{args.symbol} — Feature sample (last 5 rows):")
    print(featured.tail(5).to_string())
    print(f"\nTotal rows: {len(featured)}  |  Features: {featured.shape[1]}")


def cmd_train(args):
    from data.ingestion import get_ohlcv, fetch_and_store
    from data.database import list_symbols
    from features.engineer import engineer_features
    from models.ensemble import Ensemble
    import pandas as pd

    if args.intraday:
        from config import DEFAULT_SYMBOLS
        symbols = DEFAULT_SYMBOLS  # Core 25 NSE symbols — confirmed to have 5-min data on yfinance
        print(f"Intraday mode: fetching 5-min data for {len(symbols)} symbols …")
        frames = []
        for sym in symbols:
            try:
                df = fetch_and_store(sym, years=1, resolution="5m")
                if len(df) < 100:
                    continue
                feat = engineer_features(df)
                frames.append(feat)
                print(f"  ✓ {sym}: {len(df)} bars")
            except Exception as e:
                print(f"  ✗ {sym}: {e}")
    else:
        symbols = list_symbols() if args.all else [args.symbol]
        frames = []
        for sym in symbols:
            try:
                df = get_ohlcv(sym, resolution=args.resolution)
                feat = engineer_features(df)
                feat["_symbol"] = sym
                frames.append(feat)
            except Exception as e:
                print(f"  Skipping {sym}: {e}")

    if not frames:
        print("No data to train on.")
        sys.exit(1)

    combined = pd.concat(frames, ignore_index=False)
    if "_symbol" in combined.columns:
        combined = combined.drop(columns=["_symbol"])
    print(f"Training on {len(combined):,} bars across {len(frames)} symbol(s) …")

    ensemble = Ensemble()
    if args.intraday:
        from config import INTRADAY_LOOKAHEAD, INTRADAY_BUY_THRESHOLD, INTRADAY_SELL_THRESHOLD
        ensemble.fit(combined,
                     lookahead=INTRADAY_LOOKAHEAD,
                     buy_threshold=INTRADAY_BUY_THRESHOLD,
                     sell_threshold=INTRADAY_SELL_THRESHOLD)
        path = ensemble.save(suffix="_intraday")
    else:
        ensemble.fit(combined)
        path = ensemble.save()
    print(f"✓ Ensemble saved to {path}")


def cmd_backtest(args):
    from data.ingestion import get_ohlcv
    from features.engineer import engineer_features
    from models.ensemble import Ensemble
    from backtesting.engine import run_walk_forward_pretrained

    df = get_ohlcv(args.symbol, resolution=args.resolution)
    featured = engineer_features(df)
    print(f"Running walk-forward backtest on {args.symbol} ({len(featured)} bars) …\n")

    try:
        ensemble = Ensemble.load()
        print("  Using pre-trained ensemble (65K bars, 25 symbols).")
    except FileNotFoundError:
        print("No trained model found — run: python main.py train --all")
        sys.exit(1)

    results = run_walk_forward_pretrained(featured, ensemble)

    print("── Per-fold results ──────────────────────────────────────")
    for fold in results["folds"]:
        status = "✓" if fold["sharpe"] >= GATE_SHARPE else "✗"
        print(f"  Fold {fold['fold']} [{fold['test_start']} → {fold['test_end']}]"
              f"  Sharpe={fold['sharpe']:.2f}  WinRate={fold['win_rate']:.1%}"
              f"  MaxDD={fold['max_drawdown']:.1%}  Trades={fold['n_trades']}  {status}")

    s = results["summary"]
    print("\n── Phase 1 Performance Gates ─────────────────────────────")
    gates = [
        ("Sharpe Ratio",   s["sharpe"],        GATE_SHARPE,        ">="),
        ("Win Rate",       s["win_rate"],       GATE_WIN_RATE,      ">="),
        ("Max Drawdown",   s["max_drawdown"],   GATE_MAX_DRAWDOWN,  "<="),
        ("Profit Factor",  s["profit_factor"],  GATE_PROFIT_FACTOR, ">="),
    ]
    all_pass = True
    for name, value, threshold, op in gates:
        if op == ">=":
            passed = value >= threshold
        else:
            passed = value <= threshold
        status = "✓ PASS" if passed else "✗ FAIL"
        if not passed:
            all_pass = False
        fmt_val = f"{value:.2f}" if name != "Win Rate" else f"{value:.1%}"
        fmt_thr = f"{threshold:.2f}" if name != "Win Rate" else f"{threshold:.1%}"
        print(f"  {status}  {name:<18} {fmt_val}  (threshold {op} {fmt_thr})")

    print(f"\n  {'🟢 ALL GATES PASSED — Ready for Phase 2!' if all_pass else '🔴 Some gates failed — retrain or add more data.'}")

    if args.save:
        import pathlib
        out = pathlib.Path("backtest_report.json")
        out.write_text(json.dumps(results, indent=2))
        print(f"\n  Full report saved to {out}")


def cmd_intraday(args):
    from models.ensemble import Ensemble

    # ── US / NYSE session ────────────────────────────────────────────────
    if getattr(args, "us", False):
        from intraday.us_engine import run_us_session
        from config import DEFAULT_US_SYMBOLS

        try:
            ensemble = Ensemble.load(suffix="_intraday")
        except FileNotFoundError:
            try:
                print("No intraday model — using swing model as fallback.")
                ensemble = Ensemble.load()
            except FileNotFoundError:
                print("No trained model found. Run: python main.py train --all")
                sys.exit(1)

        symbols = (
            [s.strip() for s in args.symbol.split(",")]
            if args.symbol else DEFAULT_US_SYMBOLS
        )
        print(f"NYSE/NASDAQ session | {len(symbols)} symbols | portfolio=${args.portfolio:,.0f}")
        run_us_session(symbols=symbols, ensemble=ensemble, portfolio_value=args.portfolio)
        return

    # ── NSE / IST session (default) ──────────────────────────────────────
    from data.universe import get_morning_universe, NIFTY_500_SYMBOLS
    from intraday.engine import run_intraday_session

    try:
        ensemble = Ensemble.load(suffix="_intraday")
    except FileNotFoundError:
        try:
            print("No intraday model found — using swing model as fallback.")
            ensemble = Ensemble.load()
        except FileNotFoundError:
            print("No trained model found. Run: python main.py train --intraday")
            sys.exit(1)

    if args.symbol:
        symbols = [s.strip() for s in args.symbol.split(",")]
        print(f"Using {len(symbols)} manually specified symbols.")
    else:
        print("Scanning universe — picking top 50 stocks for today …")
        symbols = get_morning_universe(top_n=args.top_n)

    print(f"Selected: {', '.join(symbols[:10])}{'…' if len(symbols)>10 else ''}\n")
    run_intraday_session(symbols, ensemble, portfolio_value=args.portfolio)


def cmd_review(args):
    from review.performance import run_review, print_review
    report = run_review()
    print_review(report)
    if args.save:
        import pathlib
        out = pathlib.Path("forward_review.json")
        out.write_text(json.dumps(report, indent=2, default=str))
        print(f"  Full report saved to {out}")


def cmd_dashboard(args):
    import uvicorn
    print(f"Starting dashboard at http://localhost:{args.port}")
    print("  Press Ctrl+C to stop.\n")
    uvicorn.run("api.app:app", host=args.host, port=args.port,
                reload=args.reload, log_level="warning")


def cmd_alerts(args):
    from alerts.dispatcher import send_test_alerts
    from config import (
        TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
        ALERT_EMAIL_FROM, ALERT_EMAIL_TO,
        ALERT_MIN_CONFIDENCE, ALERT_SYMBOLS,
    )

    if args.test:
        print("Sending test alerts …")
        results = send_test_alerts()
        print(f"  Telegram: {'✓ sent' if results['telegram'] else '✗ failed (check TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env)'}")
        print(f"  Email:    {'✓ sent' if results['email'] else '✗ failed (check ALERT_EMAIL_* in .env)'}")
        return

    # Print current config
    print("\n── Alert Configuration ───────────────────────────────────")
    print(f"  Telegram bot token:  {'SET' if TELEGRAM_BOT_TOKEN else 'NOT SET'}")
    print(f"  Telegram chat ID:    {'SET' if TELEGRAM_CHAT_ID else 'NOT SET'}")
    print(f"  Email from:          {ALERT_EMAIL_FROM or 'NOT SET'}")
    print(f"  Email to:            {ALERT_EMAIL_TO or 'NOT SET'}")
    print(f"  Min confidence:      {ALERT_MIN_CONFIDENCE:.0%}")
    print(f"  Symbol whitelist:    {', '.join(ALERT_SYMBOLS) if ALERT_SYMBOLS else 'all symbols'}")
    print()
    print("  To configure: edit .env and set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    print("  To test:      python main.py alerts --test")
    print()


def cmd_paper(args):
    from models.ensemble import Ensemble
    from paper_trading.portfolio import init_paper_tables, reset_portfolio, get_config
    from paper_trading.engine import run_paper_session, print_paper_status
    from config import DEFAULT_SYMBOLS

    if args.status:
        print_paper_status()
        return

    if args.reset:
        reset_portfolio(args.portfolio)
        print(f"Paper portfolio reset — starting cash: ₹{args.portfolio:,.0f}")
        return

    try:
        ensemble = Ensemble.load()
    except FileNotFoundError:
        print("No trained model found — run: python main.py train --all")
        sys.exit(1)

    symbols = args.symbol.split(",") if args.symbol else DEFAULT_SYMBOLS

    init_paper_tables()
    if get_config("cash") is None:
        from paper_trading.portfolio import set_cash, set_config
        set_cash(args.portfolio)
        set_config("peak_value", args.portfolio)
        set_config("initial_cash", args.portfolio)

    print(f"Paper trading | {len(symbols)} symbols | {'EOD mode' if args.once else 'live loop'}")
    if not args.once:
        print("  Press Ctrl+C to stop.\n")

    run_paper_session(
        symbols=symbols,
        ensemble=ensemble,
        portfolio_value=args.portfolio,
        resolution=args.resolution,
        interval_sec=args.interval,
        run_once=args.once,
    )


def cmd_signal(args):
    from data.ingestion import get_ohlcv
    from features.engineer import engineer_features
    from models.ensemble import Ensemble
    from signals.generator import generate_signal, format_signal

    try:
        ensemble = Ensemble.load()
    except FileNotFoundError:
        print("No trained model found. Run: python main.py train --symbol", args.symbol)
        sys.exit(1)

    df = get_ohlcv(args.symbol, resolution=args.resolution)
    featured = engineer_features(df)
    payload = generate_signal(args.symbol, featured, ensemble,
                              portfolio_value=args.portfolio)
    print(format_signal(payload))

    if args.json:
        print(json.dumps(payload, indent=2))


# ── Argument parser ─────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="AI Stock Market Analyzer — Phase 1 CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # fetch
    p_fetch = sub.add_parser("fetch", help="Download OHLCV data")
    p_fetch.add_argument("--symbol", default=None, help="Comma-separated symbols (overrides --market)")
    p_fetch.add_argument("--market", default="nse", choices=["nse", "us", "crypto"],
                         help="Fetch default symbol list for a market (nse/us/crypto)")
    p_fetch.add_argument("--years", type=int, default=3)
    p_fetch.add_argument("--resolution", default="1d")

    # features
    p_feat = sub.add_parser("features", help="Compute and display features")
    p_feat.add_argument("--symbol", default=DEFAULT_SYMBOLS[0])
    p_feat.add_argument("--resolution", default="1d")

    # train
    p_train = sub.add_parser("train", help="Train the ensemble model")
    p_train.add_argument("--symbol", default=DEFAULT_SYMBOLS[0])
    p_train.add_argument("--resolution", default="1d")
    p_train.add_argument("--all", action="store_true", help="Train on all stored symbols")
    p_train.add_argument("--intraday", action="store_true", help="Train on 5-min bars for intraday trading")

    # backtest
    p_bt = sub.add_parser("backtest", help="Run walk-forward backtest")
    p_bt.add_argument("--symbol", default=DEFAULT_SYMBOLS[0])
    p_bt.add_argument("--resolution", default="1d")
    p_bt.add_argument("--save", action="store_true", help="Save JSON report")

    # intraday
    p_intra = sub.add_parser("intraday", help="Run intraday paper trading (NSE 9:15 AM IST | NYSE 9:30 AM ET)")
    p_intra.add_argument("--symbol", default=None, help="Comma-separated symbols (default: morning scanner / DEFAULT_US_SYMBOLS)")
    p_intra.add_argument("--top-n", type=int, default=50, help="NSE only: how many stocks to pick from morning scan")
    p_intra.add_argument("--portfolio", type=float, default=100_000.0)
    p_intra.add_argument("--us", action="store_true",
                         help="Run NYSE/NASDAQ session (9:30 AM – 3:50 PM ET) via Alpaca")

    # review
    p_rev = sub.add_parser("review", help="Forward performance review — go/no-go for live trading")
    p_rev.add_argument("--save", action="store_true", help="Save report as forward_review.json")

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Start the web dashboard")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=8000)
    p_dash.add_argument("--reload", action="store_true", help="Auto-reload on code changes")

    # alerts
    p_alerts = sub.add_parser("alerts", help="Configure and test alert channels")
    p_alerts.add_argument("--test", action="store_true", help="Send a test ping to Telegram and email")

    # paper
    p_paper = sub.add_parser("paper", help="Run paper trading engine")
    p_paper.add_argument("--symbol", default=None, help="Comma-separated symbols (default: all 25)")
    p_paper.add_argument("--resolution", default="1d")
    p_paper.add_argument("--portfolio", type=float, default=100_000.0)
    p_paper.add_argument("--interval", type=int, default=300, help="Poll interval in seconds")
    p_paper.add_argument("--once", action="store_true", help="Run one pass and exit (EOD cron mode)")
    p_paper.add_argument("--status", action="store_true", help="Print portfolio status and exit")
    p_paper.add_argument("--reset", action="store_true", help="Reset paper portfolio to starting cash")

    # signal
    p_sig = sub.add_parser("signal", help="Generate signal for latest bar")
    p_sig.add_argument("--symbol", default=DEFAULT_SYMBOLS[0])
    p_sig.add_argument("--resolution", default="1d")
    p_sig.add_argument("--portfolio", type=float, default=100_000.0)
    p_sig.add_argument("--json", action="store_true", help="Also print raw JSON")

    return parser


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "intraday": cmd_intraday,
        "fetch": cmd_fetch,
        "features": cmd_features,
        "train": cmd_train,
        "backtest": cmd_backtest,
        "review": cmd_review,
        "dashboard": cmd_dashboard,
        "alerts": cmd_alerts,
        "paper": cmd_paper,
        "signal": cmd_signal,
    }
    dispatch[args.command](args)
