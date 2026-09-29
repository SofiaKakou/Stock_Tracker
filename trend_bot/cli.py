"""Command-line interface.

    python -m trend_bot scan                    # today's signals for the watchlist
    python -m trend_bot scan --events           # ...plus upcoming earnings dates
    python -m trend_bot backtest AAPL           # historical performance on one stock
    python -m trend_bot backtest SPY --plot     # also save a chart to charts/
    python -m trend_bot portfolio --plot        # backtest the whole watchlist together
    python -m trend_bot alert                   # send trend changes to Discord
    python -m trend_bot news NVDA               # next earnings date + recent headlines
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd

from trend_bot.backtest import run_backtest
from trend_bot.data import load_prices, read_watchlist
from trend_bot.strategy import STRATEGIES, Breakout, MACrossover, Strategy

ACTION_ORDER = {"BUY": 0, "SELL": 1, "HOLD": 2, "WAIT": 3}


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


def tickers_from(args: argparse.Namespace) -> list[str]:
    if not args.tickers:
        return read_watchlist(args.watchlist)
    return [t if t.lower().endswith(".csv") else t.upper() for t in args.tickers]


def generate_all(tickers: list[str], strategy: Strategy, period: str, use_cache: bool) -> dict[str, pd.DataFrame]:
    """Run the strategy on every ticker, skipping (and reporting) any that fail."""
    out = {}
    for t in tickers:
        try:
            out[t] = strategy.generate(load_prices(t, period=period, use_cache=use_cache))
        except Exception as e:  # keep going with the rest of the list
            print(f"[skip] {t}: {e}", file=sys.stderr)
    return out


def open_file(path: Path) -> None:
    """Open a file with the system's default viewer (best effort)."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
    except OSError:
        pass


def _finish_chart(chart: Path, args: argparse.Namespace) -> None:
    print(f"\nChart saved to {chart.resolve()}")
    if not args.no_open and sys.stdout.isatty():
        open_file(chart)


# --- commands ---------------------------------------------------------------

def cmd_scan(args: argparse.Namespace) -> int:
    strategy = build_strategy(args)
    signals = generate_all(tickers_from(args), strategy, args.period or "2y", not args.no_cache)
    rows = [describe_today(t, s) for t, s in signals.items()]
    if not rows:
        print("No results.")
        return 1
    table = pd.DataFrame(rows).set_index("ticker")
    if args.events:
        from trend_bot.news import earnings_note, next_earnings

        table["earnings"] = [earnings_note(next_earnings(t)) or "–" for t in table.index]
    table = table.sort_values("action", key=lambda s: s.map(ACTION_ORDER))
    print(f"Strategy: {strategy.label}\n")
    print(table.to_string())
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    strategy = build_strategy(args)
    prices = load_prices(args.source, period=args.period or "5y", use_cache=not args.no_cache)
    signals = strategy.generate(prices)
    result = run_backtest(signals, capital=args.capital, cost_bps=args.cost_bps, cash_rate=args.cash_rate)
    s = result.stats

    print(f"{args.source}  {prices.index[0].date()} -> {prices.index[-1].date()}  ({len(prices)} bars)")
    print(f"Strategy: {strategy.label}")
    if args.cash_rate:
        print(f"Cash earns {args.cash_rate:g}% a year while out of the market")
    print(f"\n{'':22}{'Strategy':>12}{'Buy & hold':>12}")
    print(f"{'Total return':22}{s['total_return']:>12.1%}{s['buy_hold_return']:>12.1%}")
    print(f"{'Yearly growth (CAGR)':22}{s['cagr']:>12.1%}{s['buy_hold_cagr']:>12.1%}")
    print(f"{'Max drawdown':22}{s['max_drawdown']:>12.1%}{s['buy_hold_max_drawdown']:>12.1%}")
    print(f"{'Sharpe ratio':22}{s['sharpe']:>12.2f}{s['buy_hold_sharpe']:>12.2f}")
    print(f"{'Time in market':22}{s['exposure']:>12.0%}{1:>12.0%}")
    print(f"{'Final equity':22}{result.equity.iloc[-1]:>12,.0f}{result.buy_hold.iloc[-1]:>12,.0f}")
    print(f"\nTrades {int(s['trades'])} | win rate {s['win_rate']:.0%} (closed trades)")

    if args.show_trades and not result.trades.empty:
        t = result.trades.copy()
        t["entry"] = t["entry"].dt.date
        t["exit"] = t["exit"].dt.date
        t["return"] = t["return"].map("{:+.1%}".format)
        print("\n" + t.to_string(index=False, float_format="{:.2f}".format))

    if args.plot:
        from trend_bot.plot import plot_backtest  # matplotlib is only needed for charts

        name = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(args.source).stem).upper()
        chart = plot_backtest(signals, result, f"{name} · {strategy.label}",
                              Path(args.chart_dir) / f"{name}_{strategy.name}.png")
        _finish_chart(chart, args)
    return 0


def cmd_portfolio(args: argparse.Namespace) -> int:
    from trend_bot.portfolio import run_portfolio_backtest

    strategy = build_strategy(args)
    signals = generate_all(tickers_from(args), strategy, args.period or "5y", not args.no_cache)
    if not signals:
        print("No results.")
        return 1
    result = run_portfolio_backtest(signals, weighting=args.weighting, capital=args.capital,
                                    cost_bps=args.cost_bps, cash_rate=args.cash_rate)
    s = result.stats
    idx = result.equity.index

    print(f"{len(signals)} tickers: {', '.join(signals)}")
    print(f"{idx[0].date()} -> {idx[-1].date()}  ({len(idx)} bars, dates where every ticker has data)")
    print(f"Strategy: {strategy.label}  |  weighting: {args.weighting}")
    if args.cash_rate:
        print(f"Cash earns {args.cash_rate:g}% a year while out of the market")
    print(f"\n{'':22}{'Portfolio':>12}{'Buy & hold':>12}")
    print(f"{'Total return':22}{s['total_return']:>12.1%}{s['benchmark_return']:>12.1%}")
    print(f"{'Yearly growth (CAGR)':22}{s['cagr']:>12.1%}{s['benchmark_cagr']:>12.1%}")
    print(f"{'Max drawdown':22}{s['max_drawdown']:>12.1%}{s['benchmark_max_drawdown']:>12.1%}")
    print(f"{'Sharpe ratio':22}{s['sharpe']:>12.2f}{s['benchmark_sharpe']:>12.2f}")
    print(f"{'Final equity':22}{result.equity.iloc[-1]:>12,.0f}{result.benchmark.iloc[-1]:>12,.0f}")
    print(f"\nOn average {s['avg_invested']:.0%} invested, holding {s['avg_holdings']:.1f} "
          f"of {len(signals)} stocks. Buy & hold = equal amounts in every ticker.")

    t = result.per_ticker.copy()
    for col in ("strategy", "buy_hold", "max_dd"):
        t[col] = t[col].map("{:+.1%}".format)
    t["in_market"] = t["in_market"].map("{:.0%}".format)
    print("\nEach ticker on its own (same dates):\n" + t.to_string())

    if args.plot:
        from trend_bot.plot import plot_portfolio

        chart = plot_portfolio(result, f"Watchlist portfolio · {strategy.label} · {args.weighting} weighting",
                               Path(args.chart_dir) / f"PORTFOLIO_{strategy.name}_{args.weighting}.png")
        _finish_chart(chart, args)
    return 0


def cmd_alert(args: argparse.Namespace) -> int:
    from trend_bot import alerts
    from trend_bot.news import earnings_note, headlines, next_earnings

    webhook = args.webhook or alerts.load_webhook()
    if not webhook and not args.dry_run:
        print("No Discord webhook set. Add DISCORD_WEBHOOK_URL=... to a .env file (see README).", file=sys.stderr)
        return 2

    if args.test:
        alerts.send(webhook, {"username": "Trend Bot", "content": "✅ Trend Bot is connected to this channel."})
        print("Test message sent.")
        return 0

    strategy = build_strategy(args)
    signals = generate_all(tickers_from(args), strategy, args.period or "2y", not args.no_cache)
    rows = sorted((describe_today(t, s) for t, s in signals.items()), key=lambda r: ACTION_ORDER[r["action"]])
    if not rows:
        print("No results.", file=sys.stderr)
        return 1

    state = alerts.load_state(args.state)
    changes = alerts.find_changes(rows, state)
    embeds = []
    for row in changes:
        note = news = None
        if not args.no_news:
            note = earnings_note(next_earnings(row["ticker"]))
            news = headlines(row["ticker"], limit=3)
        embeds.append(alerts.change_embed(row, strategy.label, note or "", news))
    if args.summary:
        embeds.append(alerts.summary_embed(rows, strategy.label))

    if changes:
        print("Trend changes: " + ", ".join(f"{r['ticker']} {r['action'] if r['action'] in ('BUY', 'SELL') else r['trend']}"
                                            for r in changes))
    else:
        print("No trend changes since the last run.")

    if embeds:
        for payload in alerts.payloads(embeds):
            if args.dry_run:
                print(json.dumps(payload, indent=2, default=str))
            else:
                alerts.send(webhook, payload)
        if not args.dry_run:
            print(f"Sent {len(embeds)} message(s) to Discord.")

    if not args.dry_run:
        alerts.save_state(args.state, alerts.updated_state(rows, state))
    return 0


def cmd_news(args: argparse.Namespace) -> int:
    from trend_bot.news import earnings_note, headlines, next_earnings

    for t in tickers_from(args):
        print(f"== {t} ==")
        note = earnings_note(next_earnings(t))
        print(f"Next earnings: {note or 'unknown / none scheduled'}")
        items = headlines(t, limit=args.limit)
        if not items:
            print("No headlines found.")
        for h in items:
            when = f"{h.published:%b %d} " if h.published else ""
            print(f"  {when}{h.title}" + (f"  [{h.publisher}]" if h.publisher else ""))
            if h.url:
                print(f"         {h.url}")
        print()
    return 0


# --- argument parsing ---------------------------------------------------------

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
                        help="history to download: 1y, 2y, 5y, max, ... (default: 2y scan/alert, 5y backtests)")
    common.add_argument("--no-cache", action="store_true", help="always re-download prices")

    watch = argparse.ArgumentParser(add_help=False)
    watch.add_argument("tickers", nargs="*", help="tickers (default: the watchlist)")
    watch.add_argument("--watchlist", default="watchlist.txt")

    money = argparse.ArgumentParser(add_help=False)
    money.add_argument("--capital", type=float, default=10_000)
    money.add_argument("--cost-bps", type=float, default=5.0, help="cost per trade in basis points")
    money.add_argument("--cash-rate", type=float, default=0.0,
                       help="annual %% interest earned while in cash, e.g. 4 (default 0)")

    chart = argparse.ArgumentParser(add_help=False)
    chart.add_argument("--plot", action="store_true", help="save a chart")
    chart.add_argument("--chart-dir", default="charts", help="folder for charts (default: charts)")
    chart.add_argument("--no-open", action="store_true", help="don't open the chart after saving it")

    sub = p.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", parents=[common, watch], help="show today's signal for each ticker")
    scan.add_argument("--events", action="store_true", help="add a column with the next earnings date")
    scan.set_defaults(func=cmd_scan)

    bt = sub.add_parser("backtest", parents=[common, money, chart], help="backtest one ticker or CSV")
    bt.add_argument("source", help="ticker symbol or path to a CSV file")
    bt.add_argument("--show-trades", action="store_true")
    bt.set_defaults(func=cmd_backtest)

    pf = sub.add_parser("portfolio", parents=[common, watch, money, chart],
                        help="backtest the strategy across several tickers with one pot of money")
    pf.add_argument("--weighting", choices=["equal", "active"], default="equal",
                    help="equal: fixed 1/N slot per ticker; active: split money among tickers in an uptrend")
    pf.set_defaults(func=cmd_portfolio)

    al = sub.add_parser("alert", parents=[common, watch], help="send trend changes to Discord")
    al.add_argument("--webhook", help="Discord webhook URL (default: DISCORD_WEBHOOK_URL from env or .env)")
    al.add_argument("--summary", action="store_true", help="also send a table of every ticker")
    al.add_argument("--test", action="store_true", help="just send a test message")
    al.add_argument("--dry-run", action="store_true", help="print the messages instead of sending them")
    al.add_argument("--no-news", action="store_true", help="don't add earnings dates and headlines")
    al.add_argument("--state", default="alert_state.json", help="file that remembers the last trends")
    al.set_defaults(func=cmd_alert)

    nw = sub.add_parser("news", parents=[watch], help="next earnings date and recent headlines")
    nw.add_argument("--limit", type=int, default=5, help="headlines per ticker")
    nw.set_defaults(func=cmd_news)
    return p


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    return args.func(args)
