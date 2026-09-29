"""Command-line interface.

    python -m trend_bot scan                 # today's signals for the watchlist
    python -m trend_bot backtest AAPL        # historical performance of the strategy
    python -m trend_bot backtest prices.csv  # same, from a local CSV
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from trend_bot.backtest import run_backtest
from trend_bot.data import load_prices, read_watchlist
from trend_bot.strategy import STRATEGIES, Breakout, MACrossover, Strategy


def build_strategy(args: argparse.Namespace) -> Strategy:
    if args.strategy == Breakout.name:
        return Breakout(entry=args.entry, exit=args.exit)
    return MACrossover(fast=args.fast, slow=args.slow, use_ema=args.ema, rsi_max=args.rsi_max)


def describe_today(ticker: str, signals: pd.DataFrame) -> dict:
    last, prev = signals.iloc[-1], signals.iloc[-2]
    if last["position"] and not prev["position"]:
        action = "BUY"
    elif not last["position"] and prev["position"]:
        action = "SELL"
    else:
        action = "HOLD" if last["position"] else "WAIT"
    change_20d = signals["Close"].iloc[-1] / signals["Close"].iloc[-21] - 1 if len(signals) > 20 else float("nan")
    return {
        "ticker": ticker,
        "date": signals.index[-1].date(),
        "close": round(float(last["Close"]), 2),
        "20d_chg": f"{change_20d:+.1%}",
        "rsi": round(float(last["rsi"]), 1) if "rsi" in signals and pd.notna(last["rsi"]) else None,
        "trend": "UP" if last["position"] else "DOWN/FLAT",
        "action": action,
    }


def cmd_scan(args: argparse.Namespace) -> int:
    tickers = args.tickers or read_watchlist(args.watchlist)
    strategy = build_strategy(args)
    rows = []
    for t in tickers:
        try:
            signals = strategy.generate(load_prices(t, period=args.period or "2y", use_cache=not args.no_cache))
            rows.append(describe_today(t, signals))
        except Exception as e:  # keep scanning the rest of the list
            print(f"[skip] {t}: {e}", file=sys.stderr)
    if not rows:
        print("No results.")
        return 1
    table = pd.DataFrame(rows).set_index("ticker")
    order = {"BUY": 0, "SELL": 1, "HOLD": 2, "WAIT": 3}
    table = table.sort_values("action", key=lambda s: s.map(order))
    print(f"Strategy: {strategy}\n")
    print(table.to_string())
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    strategy = build_strategy(args)
    prices = load_prices(args.source, period=args.period or "5y", use_cache=not args.no_cache)
    signals = strategy.generate(prices)
    result = run_backtest(signals, capital=args.capital, cost_bps=args.cost_bps)
    s = result.stats

    print(f"{args.source}  {prices.index[0].date()} -> {prices.index[-1].date()}  ({len(prices)} bars)")
    print(f"Strategy: {strategy}\n")
    print(f"{'':22}{'Strategy':>12}{'Buy & hold':>12}")
    print(f"{'Total return':22}{s['total_return']:>12.1%}{s['buy_hold_return']:>12.1%}")
    print(f"{'Max drawdown':22}{s['max_drawdown']:>12.1%}{s['buy_hold_max_drawdown']:>12.1%}")
    print(f"{'Final equity':22}{result.equity.iloc[-1]:>12,.0f}{result.buy_hold.iloc[-1]:>12,.0f}")
    print(f"\nCAGR {s['cagr']:.1%} | Sharpe {s['sharpe']:.2f} | "
          f"time in market {s['exposure']:.0%} | trades {int(s['trades'])} | win rate {s['win_rate']:.0%}")

    if args.show_trades and not result.trades.empty:
        t = result.trades.copy()
        t["entry"] = t["entry"].dt.date
        t["exit"] = t["exit"].dt.date
        t["return"] = t["return"].map("{:+.1%}".format)
        print("\n" + t.to_string(index=False, float_format="{:.2f}".format))
    return 0


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trend_bot", description="Local trend-following stock bot")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--strategy", choices=sorted(STRATEGIES), default=MACrossover.name)
    common.add_argument("--fast", type=int, default=50, help="fast MA window (ma_cross)")
    common.add_argument("--slow", type=int, default=200, help="slow MA window (ma_cross)")
    common.add_argument("--ema", action="store_true", help="use EMAs instead of SMAs (ma_cross)")
    common.add_argument("--rsi-max", type=float, default=None,
                        help="skip new entries when RSI is above this (ma_cross), e.g. 70")
    common.add_argument("--entry", type=int, default=55, help="breakout entry lookback (breakout)")
    common.add_argument("--exit", type=int, default=20, help="breakout exit lookback (breakout)")
    common.add_argument("--period", default=None,
                        help="history to download: 1y, 2y, 5y, max, ... (default: 2y scan, 5y backtest)")
    common.add_argument("--no-cache", action="store_true", help="always re-download prices")

    sub = p.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", parents=[common], help="show today's signal for each ticker")
    scan.add_argument("tickers", nargs="*", help="tickers to scan (default: watchlist)")
    scan.add_argument("--watchlist", default="watchlist.txt")
    scan.set_defaults(func=cmd_scan)

    bt = sub.add_parser("backtest", parents=[common], help="backtest the strategy on one ticker or CSV")
    bt.add_argument("source", help="ticker symbol or path to a CSV file")
    bt.add_argument("--capital", type=float, default=10_000)
    bt.add_argument("--cost-bps", type=float, default=5.0, help="cost per trade in basis points")
    bt.add_argument("--show-trades", action="store_true")
    bt.set_defaults(func=cmd_backtest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    return args.func(args)
