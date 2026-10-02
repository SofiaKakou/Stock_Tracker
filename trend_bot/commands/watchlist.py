"""Watchlist commands: scan, backtest, portfolio, alert (Discord), news, insiders."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from contextlib import closing
from pathlib import Path

import pandas as pd

from trend_bot.backtest import run_backtest
from trend_bot.strategy import Strategy
from trend_bot.commands.outputs import _record_model_picks
from trend_bot.commands.common import (
    ACTION_ORDER,
    build_strategy,
    describe_today,
    tickers_from,
    get_prices,
    generate_all,
    _finish_chart,
    _insider_short,
)


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


def _red_flag_embed(con, watch: list[str], dry_run: bool = False) -> dict | None:
    """One message listing red flags filed in the last 30 days that weren't sent before."""
    from trend_bot import alerts, db, events

    watched = {w.upper() for w in watch}
    held = [t for (t,) in con.execute("SELECT ticker FROM ideas_holdings")]
    names = {t.upper(): "watchlist" if t.upper() in watched else "top-ideas portfolio" for t in held + watch}
    if not names:
        return None
    cik_of = {}   # cik -> ticker
    for t, c in con.execute(f"SELECT ticker, cik FROM tickers WHERE cik IS NOT NULL AND ticker IN "
                            f"({','.join('?' * len(names))})", list(names)):
        cik_of.setdefault(c, t)
    flags = events.recent_flags(con, list(cik_of))
    sent = set(json.loads(db.get_meta(con, "red_flags_sent", "[]")))
    flags = flags[~flags["accession"].isin(sent)]
    if flags.empty:
        return None
    if not dry_run:
        db.set_meta(con, "red_flags_sent", json.dumps((sorted(sent) + list(flags["accession"]))[-1000:]))
        con.commit()
    return alerts.red_flags_embed([(cik_of[r.cik], names[cik_of[r.cik]], r.filed, events.describe(r.items))
                                   for r in flags.itertuples(index=False)])


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

    if args.model and Path(args.db).exists():
        from trend_bot import db as mdb, model

        with closing(mdb.connect(args.db)) as con:
            res = model.update_portfolio(con)
            if res["rebalanced"] and not args.dry_run:
                _record_model_picks(con, res)
        e = alerts.model_embed(res)
        if e:
            embeds.append(e)
    if (args.model or args.forecast) and Path(args.db).exists():
        from trend_bot import db as idb, ideas as iideas

        with closing(idb.connect(args.db)) as con:
            # Once a month: how every kind of pick has really done since it was made.
            month = dt.date.today().strftime("%Y-%m")
            if idb.get_meta(con, "scorecard_month") != month:
                from trend_bot import track as itrack

                card = itrack.scorecard(con)
                if len(card):
                    embeds.append(alerts.scorecard_embed(card))
                    if not args.dry_run:
                        idb.set_meta(con, "scorecard_month", month)
                        con.commit()
            port = iideas.load_portfolio(con)
            # Once per monthly update: the buys and sells of the top-ideas portfolio.
            if port and idb.get_meta(con, "ideas_portfolio_sent") != port["date"]:
                embeds.append(alerts.ideas_portfolio_embed(port, iideas.reliability_note(con), iideas.live_line(con)))
                if not args.dry_run:
                    idb.set_meta(con, "ideas_portfolio_sent", port["date"])
                    con.commit()
            # New 8-K red flags or late filing notices for the watchlist and the top-ideas portfolio.
            e = _red_flag_embed(con, [r["ticker"] for r in rows], args.dry_run)
            if e:
                embeds.append(e)
    if args.forecast and Path(args.db).exists():
        from trend_bot import db as fdb, forecast, track as ftrack

        with closing(fdb.connect(args.db)) as con:
            week = f"{dt.date.today().isocalendar()[0]}-W{dt.date.today().isocalendar()[1]:02d}"
            if fdb.get_meta(con, "forecast_week") != week:
                from trend_bot import model as fmodel

                models, _ = forecast.load_models(con)
                preds, date, _ = forecast.predict_today(con, models)
                if len(preds):
                    if not args.dry_run:
                        ftrack.record_forecast(con, preds, date)
                        fdb.set_meta(con, "forecast_week", week)
                        con.commit()
                    from trend_bot import ideas as fideas, ml as fml

                    e = alerts.ideas_embed(fideas.load(con), fmodel.weather_status(con), fideas.reliability_note(con),
                                           fideas.live_line(con))
                    if e:
                        embeds.append(e)

                    verdict, ranking = fml.load(con)
                    note = forecast.reliability_note(forecast.load_scorecard(con))
                    extra = alerts.ml_status_line(verdict)
                    embeds.append(alerts.forecast_embed(preds, date, fmodel.weather_status(con),
                                                        f"{note} {extra}".strip()))
                    e = alerts.ml_embed(verdict, ranking)
                    if e:
                        embeds.append(e)
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
    from trend_bot.track import record_picks

    with closing(db.connect(args.db)) as con:
        flips, clusters = screen(con, strategy, days=1, min_price=args.min_price, min_dollar_vol=args.min_volume,
                                 cluster_days=args.cluster_days, min_buyers=args.cluster_min)
        new = set() if args.dry_run else record_picks(con, flips, clusters)
    return [alerts.market_embed(flips, clusters, strategy.label, limit=args.market_limit, new=new)]
