"""Command-line interface.

    python -m trend_bot scan                    # today's signals for the watchlist
    python -m trend_bot scan --events           # ...plus upcoming earnings dates
    python -m trend_bot backtest AAPL           # historical performance on one stock
    python -m trend_bot backtest SPY --plot     # also save a chart to charts/
    python -m trend_bot portfolio --plot        # backtest the whole watchlist together
    python -m trend_bot alert                   # send trend changes to Discord
    python -m trend_bot news NVDA               # next earnings date + recent headlines
    python -m trend_bot insiders NVDA           # insider buys/sells from SEC Form 4 filings
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import closing
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


def _period_start(period: str) -> str | None:
    """'5y' / '6mo' / '30d' -> an ISO start date; 'max' -> None."""
    m = re.fullmatch(r"(\d+)(y|mo|d)", period or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    days = n * {"y": 365.25, "mo": 30.5, "d": 1}[unit]
    return (pd.Timestamp.today() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")


def get_prices(source: str, args: argparse.Namespace, default_period: str) -> pd.DataFrame:
    """Prices from the local database (--from-db) or Yahoo/CSV."""
    period = args.period or default_period
    if getattr(args, "from_db", False) and not source.lower().endswith(".csv"):
        from trend_bot import db

        with closing(db.connect(args.db)) as con:
            return db.load_prices(con, source, start=_period_start(period))
    return load_prices(source, period=period, use_cache=not args.no_cache)


def generate_all(tickers: list[str], strategy: Strategy, args: argparse.Namespace,
                 default_period: str) -> dict[str, pd.DataFrame]:
    """Run the strategy on every ticker, skipping (and reporting) any that fail."""
    out = {}
    for t in tickers:
        try:
            out[t] = strategy.generate(get_prices(t, args, default_period))
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
    signals = generate_all(tickers_from(args), strategy, args, "2y")
    rows = [describe_today(t, s) for t, s in signals.items()]
    if not rows:
        print("No results.")
        return 1
    table = pd.DataFrame(rows).set_index("ticker")
    if args.events:
        from trend_bot.news import earnings_note, next_earnings

        table["earnings"] = [earnings_note(next_earnings(t)) or "–" for t in table.index]
    if args.insiders:
        from trend_bot import insiders

        table["insiders_90d"] = [_insider_short(insiders, tk) for tk in table.index]
    table = table.sort_values("action", key=lambda s: s.map(ACTION_ORDER))
    print(f"Strategy: {strategy.label}\n")
    print(table.to_string())
    return 0


def _insider_short(insiders, ticker: str) -> str:
    if ticker.lower().endswith(".csv"):
        return "–"
    try:
        return insiders.summarize(insiders.fetch_trades(ticker, days=90)).short
    except Exception as e:
        print(f"[insiders] {ticker}: {e}", file=sys.stderr)
        return "?"


def cmd_backtest(args: argparse.Namespace) -> int:
    strategy = build_strategy(args)
    prices = get_prices(args.source, args, "5y")
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
    signals = generate_all(tickers_from(args), strategy, args, "5y")
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
    signals = generate_all(tickers_from(args), strategy, args, "2y")
    rows = sorted((describe_today(t, s) for t, s in signals.items()), key=lambda r: ACTION_ORDER[r["action"]])
    if not rows:
        print("No results.", file=sys.stderr)
        return 1

    state = alerts.load_state(args.state)
    changes = alerts.find_changes(rows, state)

    # Insider trades (last 90 days) for every ticker, unless turned off.
    insider_trades = {}
    if not args.no_insiders:
        from trend_bot import insiders

        for r in rows:
            if r["ticker"].lower().endswith(".csv"):
                continue
            try:
                insider_trades[r["ticker"]] = insiders.fetch_trades(r["ticker"], days=90)
            except Exception as e:
                print(f"[insiders] skipped: {e}", file=sys.stderr)
                if "SEC_USER_AGENT" in str(e):
                    break  # same problem for every ticker

    embeds = []
    for row in changes:
        note = news = summary = None
        if not args.no_news:
            note = earnings_note(next_earnings(row["ticker"]))
            news = headlines(row["ticker"], limit=3)
        if row["ticker"] in insider_trades:
            summary = insiders.summarize(insider_trades[row["ticker"]])
        embeds.append(alerts.change_embed(row, strategy.label, note or "", news, summary))

    new_state = alerts.updated_state(rows, state)
    clusters = []
    trend_of = {r["ticker"]: r["trend"] for r in rows}
    for ticker, trades in insider_trades.items():
        cluster = insiders.cluster_buy(trades, window_days=args.cluster_days, min_buyers=args.cluster_min)
        if alerts.new_cluster(ticker, cluster, state):
            embeds.append(alerts.insider_embed(ticker, cluster, trend_of.get(ticker)))
            alerts.mark_cluster(ticker, cluster, new_state)
            clusters.append(ticker)

    if args.market:
        embeds.extend(_market_embeds(args, strategy))
    if args.summary:
        embeds.append(alerts.summary_embed(rows, strategy.label))

    if changes:
        print("Trend changes: " + ", ".join(f"{r['ticker']} {r['action'] if r['action'] in ('BUY', 'SELL') else r['trend']}"
                                            for r in changes))
    else:
        print("No trend changes since the last run.")
    if clusters:
        print("Insider buying clusters: " + ", ".join(clusters))

    if embeds:
        for payload in alerts.payloads(embeds):
            if args.dry_run:
                print(json.dumps(payload, indent=2, default=str))
            else:
                alerts.send(webhook, payload)
        if not args.dry_run:
            print(f"Sent {len(embeds)} message(s) to Discord.")

    if not args.dry_run:
        alerts.save_state(args.state, new_state)
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


def cmd_insiders(args: argparse.Namespace) -> int:
    from trend_bot import insiders

    for ticker in tickers_from(args):
        trades = insiders.fetch_trades(ticker, days=args.days, market_only=not args.all)
        print(f"== {ticker}: insider trades filed in the last {args.days} days ==")
        if not trades:
            print("None found (or not a US-listed operating company, e.g. an ETF).\n")
            continue
        s = insiders.summarize(trades)
        print(f"Open-market buys: {s.buys} (${s.buy_value:,.0f})   sells: {s.sells} (${s.sell_value:,.0f})")
        cluster = insiders.cluster_buy(trades, window_days=args.cluster_days, min_buyers=args.cluster_min)
        if cluster:
            names = ", ".join(sorted({t.insider for t in cluster}))
            print(f"*** Cluster buy: {len({t.insider for t in cluster})} insiders bought within "
                  f"{args.cluster_days} days ({names})")
        table = pd.DataFrame([{
            "date": t.date, "insider": t.insider[:24], "role": t.role[:28], "type": t.kind,
            "shares": f"{t.shares:,.0f}", "price": f"{t.price:,.2f}" if t.price else "–",
            "value": f"${t.value:,.0f}" if t.price else "–", "plan": "10b5-1" if t.planned else "",
        } for t in trades[: args.limit]])
        print("\n" + table.to_string(index=False))
        if len(trades) > args.limit:
            print(f"... {len(trades) - args.limit} more (use --limit)")
        print()
    return 0


def _market_embeds(args: argparse.Namespace, strategy: Strategy) -> list[dict]:
    from trend_bot import alerts, db
    from trend_bot.screen import screen

    if not Path(args.db).exists():
        print(f"[market] {args.db} not found - run 'python -m trend_bot db update' first", file=sys.stderr)
        return []
    with closing(db.connect(args.db)) as con:
        flips, clusters = screen(con, strategy, days=1, min_price=args.min_price, min_dollar_vol=args.min_volume,
                                 cluster_days=args.cluster_days, min_buyers=args.cluster_min)
    return [alerts.market_embed(flips, clusters, strategy.label, limit=args.market_limit)]


def cmd_db(args: argparse.Namespace) -> int:
    from trend_bot import db, market_data, sec_bulk

    with closing(db.connect(args.db)) as con:
        if args.action == "status":
            s = db.status(con)
            size = Path(args.db).stat().st_size / 1e6 if Path(args.db).exists() else 0
            print(f"Database: {Path(args.db).resolve()}  ({size:,.0f} MB)")
            print(f"Tickers:        {s['tickers']:,} ({s['tickers_with_prices']:,} with prices, "
                  f"{s['failed_tickers']:,} failing)")
            print(f"Price rows:     {s['price_rows']:,}  ({s['price_first']} -> {s['price_last']})")
            print(f"Insider trades: {s['insider_trades']:,}  ({s['insider_buys']:,} open-market buys, "
                  f"filed {s['insider_first']} -> {s['insider_last']})")
            return 0

        everything = not (args.universe_only or args.prices_only or args.insiders_only)
        if everything or args.universe_only or args.prices_only:
            n = market_data.update_universe(con, include_otc=args.include_otc, all_securities=args.all_securities)
            print(f"[universe] {n:,} tickers")
        if everything or args.prices_only:
            counts = market_data.update_prices(con, period=args.period, batch_size=args.batch, pause=args.pause,
                                               retry_failed=args.retry_failed, limit=args.limit)
            print(f"[prices] new {counts['new']:,}, updated {counts['updated']:,}, "
                  f"reloaded {counts['reloaded']:,}, no data {counts['failed']:,}")
        if everything or args.insiders_only:
            added = sec_bulk.load_quarters(con, since_year=args.insider_since)
            print(f"[insiders] {added} quarterly file(s) loaded")
            filings = sec_bulk.load_recent_days(con, max_days=args.insider_days)
            print(f"[insiders] {filings:,} recent filing(s) loaded")
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    from trend_bot import db
    from trend_bot.screen import screen

    if not Path(args.db).exists():
        print(f"{args.db} not found. Build it first:  python -m trend_bot db update", file=sys.stderr)
        return 1
    strategy = build_strategy(args)
    with closing(db.connect(args.db)) as con:
        flips, clusters = screen(con, strategy, days=args.days, min_price=args.min_price,
                                 min_dollar_vol=args.min_volume, cluster_days=args.cluster_days,
                                 min_buyers=args.cluster_min)
    print(f"Strategy: {strategy.label}  |  price >= ${args.min_price:g}, "
          f"avg daily volume >= ${args.min_volume:,.0f}")

    def show(df: pd.DataFrame) -> str:
        df = df.copy()
        df["chg_20d"] = df["chg_20d"].map("{:+.1%}".format)
        df["buy_value"] = df["buy_value"].map(lambda v: f"${v:,.0f}" if v else "")
        df["buyers"] = df["buyers"].map(lambda v: v or "")
        df["cluster"] = df["cluster"].map(lambda v: "YES" if v else "")
        return df.head(args.limit).to_string()

    if args.signal != "all" and not flips.empty:
        flips = flips[flips["signal"] == args.signal.upper()]
    print(f"\n== Trend flips in the last {args.days} trading day(s): {len(flips)} ==")
    print(show(flips) if not flips.empty else "None.")
    print(f"\n== Insider buying clusters (2+ insiders within {args.cluster_days} days): {len(clusters)} ==")
    print(show(clusters.drop(columns=["name"])) if not clusters.empty else "None.")
    return 0


def cmd_study(args: argparse.Namespace) -> int:
    from trend_bot import db, study
    from trend_bot.screen import liquid_tickers

    strategy = build_strategy(args)
    with closing(db.connect(args.db)) as con:
        pct = lambda df: df.to_string(formatters={
            c: "{:+.1%}".format for c in ("avg_return", "median_return", "avg_vs_bench")} | {
            c: "{:.0%}".format for c in ("win_rate", "beat_bench_rate")})
        print(f"Benchmark: {args.benchmark} (bought and sold on the same days as each stock)\n")
        if args.signal in ("insiders", "both"):
            events = study.cluster_events(con, window_days=args.cluster_days, min_buyers=args.cluster_min,
                                          since=args.since)
            print(f"Insider cluster buys since {args.since}: {len(events):,} events")
            ev = study.add_returns(con, events, strategy=strategy, benchmark=args.benchmark)
            if not ev.empty:
                print("\nAll clusters (buy the day after the filing is public):\n" + pct(study.summarize(ev, min_price=args.min_price)))
                for trend in ("UP", "DOWN"):
                    part = ev[ev["trend"] == trend]
                    print(f"\n...when the price trend was {trend} ({strategy.label}):\n"
                          + pct(study.summarize(part, min_price=args.min_price)))
        if args.signal in ("trend", "both"):
            tickers = liquid_tickers(con, args.min_price, args.min_volume)
            if args.max_tickers:
                tickers = tickers[: args.max_tickers]
            events = study.trend_flip_events(con, strategy, tickers, since=args.since)
            print(f"\nTrend BUY flips ({strategy.label}) in {len(tickers):,} stocks since {args.since}: "
                  f"{len(events):,} events")
            ev = study.add_returns(con, events, benchmark=args.benchmark)
            if not ev.empty:
                print(pct(study.summarize(ev, min_price=args.min_price)))
    print("\nNote: only companies still listed today are included (survivorship bias), "
          "so real-world results would be somewhat worse.")
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
    common.add_argument("--db", default="market.db", help="market database file (default: market.db)")
    common.add_argument("--from-db", action="store_true", help="read prices from the market database")

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
    scan.add_argument("--insiders", action="store_true", help="add a column with insider buys/sells (90 days)")
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
    al.add_argument("--no-insiders", action="store_true", help="don't check SEC insider trades")
    al.add_argument("--market", action="store_true", help="add the whole-market screen (needs the database)")
    al.add_argument("--market-limit", type=int, default=10, help="stocks listed in the market screen")
    al.add_argument("--min-price", type=float, default=5, help="market screen: minimum share price")
    al.add_argument("--min-volume", type=float, default=1e6, help="market screen: minimum average daily $ volume")
    al.add_argument("--cluster-days", type=int, default=30, help="window for an insider buying cluster")
    al.add_argument("--cluster-min", type=int, default=2, help="different insiders needed for a cluster")
    al.set_defaults(func=cmd_alert)

    nw = sub.add_parser("news", parents=[watch], help="next earnings date and recent headlines")
    nw.add_argument("--limit", type=int, default=5, help="headlines per ticker")
    nw.set_defaults(func=cmd_news)

    dbp = sub.add_parser("db", help="build/update the local market database, or show its status")
    dbp.add_argument("action", choices=["update", "status"])
    dbp.add_argument("--db", default="market.db", help="database file (default: market.db)")
    dbp.add_argument("--period", default="max", help="history for newly added tickers (default: max)")
    dbp.add_argument("--batch", type=int, default=100, help="tickers per Yahoo request")
    dbp.add_argument("--pause", type=float, default=1.0, help="seconds between Yahoo requests")
    dbp.add_argument("--limit", type=int, help="only process this many tickers (for a quick test)")
    dbp.add_argument("--include-otc", action="store_true", help="also track OTC (over-the-counter) stocks")
    dbp.add_argument("--all-securities", action="store_true",
                     help="also track warrants, units, rights and preferred shares")
    dbp.add_argument("--retry-failed", action="store_true", help="retry tickers that returned no data 3 times")
    dbp.add_argument("--insider-since", type=int, default=2006, help="first year of SEC insider data")
    dbp.add_argument("--insider-days", type=int, default=30,
                     help="max days of recent filings to fetch one by one (default 30)")
    only = dbp.add_mutually_exclusive_group()
    only.add_argument("--universe-only", action="store_true")
    only.add_argument("--prices-only", action="store_true")
    only.add_argument("--insiders-only", action="store_true")
    dbp.set_defaults(func=cmd_db)

    market = argparse.ArgumentParser(add_help=False)
    market.add_argument("--min-price", type=float, default=5, help="skip stocks below this price (default 5)")
    market.add_argument("--min-volume", type=float, default=1e6,
                        help="skip stocks trading less than this many $ a day on average (default 1M)")
    market.add_argument("--cluster-days", type=int, default=30, help="window for an insider buying cluster")
    market.add_argument("--cluster-min", type=int, default=2, help="different insiders needed for a cluster")

    sc = sub.add_parser("screen", parents=[common, market], help="scan the whole market in the database")
    sc.add_argument("--days", type=int, default=1, help="flips within this many trading days (default 1)")
    sc.add_argument("--signal", choices=["buy", "sell", "all"], default="all")
    sc.add_argument("--limit", type=int, default=40, help="rows to show per section")
    sc.set_defaults(func=cmd_screen)

    st = sub.add_parser("study", parents=[common, market], help="test a signal on the whole market's history")
    st.add_argument("signal", choices=["insiders", "trend", "both"])
    st.add_argument("--since", default="2006-01-01", help="first event date")
    st.add_argument("--max-tickers", type=int, help="trend study: limit the number of stocks (faster)")
    st.add_argument("--benchmark", default="SPY",
                    help="compare against this ticker (default SPY; IWM = small companies, QQQ = Nasdaq-100)")
    st.set_defaults(func=cmd_study)

    ins = sub.add_parser("insiders", parents=[watch], help="insider buys and sells from SEC Form 4 filings")
    ins.add_argument("--days", type=int, default=90, help="look back this many days (default 90)")
    ins.add_argument("--all", action="store_true", help="include awards, option exercises, gifts, ...")
    ins.add_argument("--limit", type=int, default=20, help="rows to show per ticker")
    ins.add_argument("--cluster-days", type=int, default=30, help="window for an insider buying cluster")
    ins.add_argument("--cluster-min", type=int, default=2, help="different insiders needed for a cluster")
    ins.set_defaults(func=cmd_insiders)
    return p


def main(argv: list[str] | None = None) -> int:
    # On Windows, output redirected to a file (like alerts.log) may use a legacy
    # encoding; never crash over a character it can't represent.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = make_parser().parse_args(argv)
    return args.func(args)
