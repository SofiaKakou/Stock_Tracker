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
import datetime as dt
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
            port = iideas.load_portfolio(con)
            # Once per monthly update: the buys and sells of the top-ideas portfolio.
            if port and idb.get_meta(con, "ideas_portfolio_sent") != port["date"]:
                embeds.append(alerts.ideas_portfolio_embed(port, iideas.reliability_note(con)))
                if not args.dry_run:
                    idb.set_meta(con, "ideas_portfolio_sent", port["date"])
                    con.commit()
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

                    e = alerts.ideas_embed(fideas.load(con), fmodel.weather_status(con), fideas.reliability_note(con))
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
            quarters, days = s["insider_quarters"], s["insider_days"]
            if quarters:
                print(f"  quarterly SEC files: {len(quarters)} loaded, {quarters[0]} -> {quarters[-1]}")
            if days:
                print(f"  recent days read one by one: {len(days)} ({days[0]} -> {days[-1]})")
            from trend_bot.company_info import fund_tickers

            checked = con.execute("SELECT COUNT(DISTINCT cik) FROM tickers WHERE info_checked_at IS NOT NULL").fetchone()[0]
            print(f"Industry codes: {checked:,} companies looked up; {len(fund_tickers(con)):,} tickers are funds/ETFs "
                  f"(left out of the screen)")
            si = con.execute("SELECT COUNT(DISTINCT settle), MIN(settle), MAX(settle) FROM short_interest").fetchone()
            if si[0]:
                print(f"Short interest: {si[0]:,} FINRA reports, {si[1]} to {si[2]}; short volume up to "
                      f"{db.get_meta(con, 'short_volume_last') or 'not loaded'}")
            picks = con.execute("SELECT COUNT(*), MIN(date) FROM picks").fetchone()
            if picks[0]:
                print(f"Tracked picks:  {picks[0]:,} since {picks[1]}")
            if s["fates"]:
                f = s["fates"]
                print(f"Delisted companies checked: {sum(f.values()):,} ({f.get('bankrupt', 0):,} bankrupt, "
                      f"{f.get('acquired', 0):,} bought out, {f.get('delisted', 0):,} delisted, "
                      f"{f.get('unknown', 0):,} unclear)")
            return 0

        if args.fundamentals_only or args.fates_only:
            args.insiders_only = True  # same SEC step, but only that part matters
        everything = not (args.universe_only or args.prices_only or args.insiders_only or args.short_only)
        if everything or args.universe_only or args.prices_only:
            n = market_data.update_universe(con, include_otc=args.include_otc, all_securities=args.all_securities)
            print(f"[universe] {n:,} tickers")
            market_data.update_etf_flags(con)
        if everything or args.prices_only:
            counts = market_data.update_prices(con, period=args.period, batch_size=args.batch, pause=args.pause,
                                               retry_failed=args.retry_failed, limit=args.limit)
            print(f"[prices] new {counts['new']:,}, updated {counts['updated']:,}, "
                  f"reloaded {counts['reloaded']:,}, no data {counts['failed']:,}")
            market_data.backfill_splits(con, pause=args.pause)
        if (everything and not args.no_short) or args.short_only:
            from trend_bot import short_interest

            try:
                short_interest.update(con)
            except RuntimeError as e:
                print(f"[short] stopped: {e} (progress is saved; the next run continues)", file=sys.stderr)
            if args.short_only:
                return 0
        if everything or args.insiders_only:
            paused = db.get_meta(con, "sec_paused_until")
            if paused and dt.datetime.now().isoformat() < paused and not args.ignore_sec_pause:
                print(f"[sec] paused until {paused[:16].replace('T', ' ')} because the SEC asked us to slow down; "
                      "skipping insider data today (prices, alerts and the report still update)")
                return 0
            try:
                if args.fundamentals_only:
                    from trend_bot import fundamentals

                    fundamentals.update_fundamentals(con, force=True)
                    return 0
                if args.fates_only:
                    from trend_bot import fates, sec_submissions

                    # Catch up in one go: the SEC's bulk file has every company's filings.
                    sec_submissions.update(con, force=True)
                    found = fates.update_fates(con, limit=args.sec_lookups)  # any the bulk file missed
                    print("[fates] " + (", ".join(f"{k} {v:,}" for k, v in sorted(found.items())) or "nothing new"))
                    left = len(fates.companies_to_check(con))
                    print(f"[fates] {left:,} companies still to check" if left else "[fates] all caught up")
                    return 0
                added = sec_bulk.load_quarters(con, since_year=args.insider_since)
                print(f"[insiders] {added} quarterly file(s) loaded")
                filings = sec_bulk.load_recent_days(con, max_days=args.insider_days,
                                                    all_companies=args.insiders_all_companies)
                print(f"[insiders] {filings:,} recent filing(s) loaded")
                from trend_bot import fates, sec_submissions

                # Industry codes and fates for every company from one weekly bulk file; the one-by-one
                # lookups below then only cover what it missed (spread over nights, well under the SEC's limits).
                sec_submissions.update(con)
                found = fates.update_fates(con, limit=args.sec_lookups)
                if found:
                    print("[fates] " + ", ".join(f"{k} {v:,}" for k, v in sorted(found.items())))
                from trend_bot import company_info

                company_info.update_company_info(con, limit=args.sec_lookups)
                if not args.no_fundamentals:
                    from trend_bot import fundamentals

                    fundamentals.update_fundamentals(con, force=args.fundamentals_only)
            except RuntimeError as e:
                print(f"[insiders] stopped: {e}", file=sys.stderr)
                if "limiting" in str(e):
                    until = (dt.datetime.now() + dt.timedelta(hours=24)).isoformat(timespec="minutes")
                    db.set_meta(con, "sec_paused_until", until)
                    con.commit()
                    print(f"[sec] pausing all SEC downloads until {until.replace('T', ' ')} "
                          "(use --ignore-sec-pause to try sooner)", file=sys.stderr)
                return 1
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
                                 min_buyers=args.cluster_min, include_funds=args.include_funds)
        if args.record:
            from trend_bot.track import record_picks

            new = record_picks(con, flips, clusters)
            print(f"Recorded {len(new)} new pick(s) for tracking.")
    print(f"Strategy: {strategy.label}  |  price >= ${args.min_price:g}, "
          f"avg daily volume >= ${args.min_volume:,.0f}")

    def show(df: pd.DataFrame) -> str:
        df = df.copy()
        df["chg_20d"] = df["chg_20d"].map("{:+.1%}".format)
        df["buy_value"] = df["buy_value"].map(lambda v: f"${v:,.0f}" if v else "")
        df["buyers"] = df["buyers"].map(lambda v: v or "")
        df["cluster"] = df["cluster"].map(lambda v: "YES" if v else "")
        if "strong" in df:
            df["strong"] = df["strong"].map(lambda v: "🔔" if v else "")
        return df.head(args.limit).to_string()

    if args.signal != "all" and not flips.empty:
        flips = flips[flips["signal"] == args.signal.upper()]
    print(f"\n== Trend flips in the last {args.days} trading day(s): {len(flips)} ==")
    print(show(flips) if not flips.empty else "None.")
    print(f"\n== Insider buying clusters (2+ insiders within {args.cluster_days} days): {len(clusters)} ==")
    print(show(clusters.drop(columns=["name"])) if not clusters.empty else "None.")
    return 0


def _print_fates(con, study, events: pd.DataFrame, ev: pd.DataFrame, args: argparse.Namespace, pct) -> None:
    """Results again with delisted companies put back (survivorship-bias correction)."""
    gone = study.add_fates(con, events, ev, benchmark=args.benchmark)
    if gone.empty:
        return
    n = gone["status"].value_counts()
    print(f"\n== Putting back companies that no longer exist: {len(gone):,} events had no price data ==")
    print(f"   bankrupt {n.get('bankrupt', 0):,}, bought out {n.get('acquired', 0):,}, "
          f"delisted for other reasons {n.get('delisted', 0):,}, unclear {n.get('unknown', 0):,}, "
          f"not checked yet {n.get('unchecked', 0):,}")
    if n.get("unchecked", 0):
        print("   (run 'python -m trend_bot db update --insiders-only' to check the rest - it also runs nightly)")
    print("   Bankruptcies count as -100%, buyouts as matching the benchmark; the rest stay out.")
    both = pd.concat([ev, gone])
    print("\nAll clusters, bankruptcies included:\n" + pct(study.summarize(both, min_price=args.min_price)))
    worst = pd.concat([ev[ev["trend"] == "UP"], gone[gone["status"] == "bankrupt"]])
    print("\nTrend UP, WORST CASE (as if every bankruptcy above had been in an uptrend):\n"
          + pct(study.summarize(worst, min_price=args.min_price)))


def _study_factors(con, args: argparse.Namespace) -> None:
    from trend_bot import factors, fundamentals

    if not con.execute("SELECT 1 FROM facts LIMIT 1").fetchone():
        print("No company financials yet. Run:  python -m trend_bot db update --fundamentals-only")
        return
    since = args.since if args.since != "2006-01-01" else "2009-06-30"  # XBRL filings start in 2009
    print("Ranking stocks by each signal at every month-end, using only financials filed by then...")
    df = factors.table(con, since=since, universe=args.universe)
    if df.empty:
        print("Not enough data.")
        return
    tables = factors.study(df, split_year=args.split_year)
    fmt = {"months": "{:.0f}".format, "stocks": "{:.0f}".format, "mean_ic": "{:+.3f}".format,
           "ic_t": "{:+.1f}".format, "ic_positive": "{:.0%}".format, "top_minus_bottom": "{:+.2%}".format,
           "ic_listed_only": "{:+.3f}".format, "ic_with_gone": "{:+.3f}".format,
           "tmb_listed_only": "{:+.2%}".format, "tmb_with_gone": "{:+.2%}".format,
           "ic_whole_market": "{:+.3f}".format, "ic_within_industry": "{:+.3f}".format,
           "t_within_industry": "{:+.1f}".format, "tmb_within_industry": "{:+.2%}".format}
    print(_gone_note(df))
    print(f"\n{df['month'].min():%Y-%m} to {df['month'].max():%Y-%m}, up to {args.universe:,} most traded stocks a month")
    for label, table in tables.items():
        print(f"\n== {label} ==\n" + table.to_string(formatters=fmt))
    print("\nmean_ic: rank correlation with next month's return (0.02-0.05 is useful, signs already flipped so"
          " positive = worked as expected). ic_t: above about 2 means unlikely to be luck. top_minus_bottom:"
          " best 10% minus worst 10%, per month.")
    print("\nWhat each signal is:")
    for n, d in fundamentals.DESCRIPTIONS.items():
        print(f"  {n:<20} {d}")
    for name, parts in factors.COMBOS.items():
        print(f"  {name:<20} average rank of {', '.join(parts)}")


def _gone_note(df: pd.DataFrame) -> str:
    """One line on the companies that disappeared and were put back into the study."""
    gone = df["gone"].dropna() if "gone" in df else pd.Series(dtype=str)
    if gone.empty:
        return ("Note: only companies listed today have prices, so ones that went bankrupt or were bought out "
                "are missing (their fates are looked up a little each night; results improve as that fills in).")
    c = gone.value_counts()
    return (f"Companies that disappeared, put back for their last month: {c.get('bankrupt', 0):,} bankrupt (-100%), "
            f"{c.get('delisted', 0):,} delisted (-30%), {c.get('acquired', 0):,} bought out (average stock). "
            "Price-based signals can't be measured for them.")


def _study_ml(con, args: argparse.Namespace) -> None:
    from trend_bot import ml, track

    if not con.execute("SELECT 1 FROM facts LIMIT 1").fetchone():
        print("No company financials yet. Run:  python -m trend_bot db update --fundamentals-only")
        return
    since = args.since if args.since != "2006-01-01" else "2009-06-30"  # XBRL filings start in 2009
    print("Building the monthly table of every signal (takes a while)...")
    df = ml.dataset(con, since=since, universe=args.universe, include_latest=True)
    if df.empty:
        print("Not enough data.")
        return
    print(_gone_note(df))
    first = max(args.first_year, df["month"].dt.year.min() + 2)
    preds = ml.walk_forward(df, first_year=first, trees=args.trees)
    if preds.empty:
        print("Not enough history before the first test year.")
        return
    card = ml.scorecard(preds, split_year=args.split_year)
    fmt = {"months": "{:.0f}".format, "stocks": "{:.0f}".format, "mean_ic": "{:+.3f}".format,
           "ic_t": "{:+.1f}".format, "ic_positive": "{:.0%}".format, "top_minus_bottom": "{:+.2%}".format,
           "top10_per_year": "{:+.1%}".format, "average_stock_per_year": "{:+.1%}".format,
           "turnover": "{:.0%}".format}
    print(f"\nWalk-forward test {preds['month'].min():%Y-%m} to {preds['month'].max():%Y-%m}: each year predicted "
          f"by a model trained only on earlier years ({len(preds):,} predictions).")
    for label, table in card.items():
        cols = [c for c in fmt if c in table]
        print(f"\n== {label} ==\n" + table[cols].to_string(formatters=fmt))
    print("\nmodel: the machine-learning model. simple_mix: equal-weight average rank of every signal plus momentum "
          "(no fitting). industry_mix: the same, with company signals ranked within each industry. "
          "..._plus: also low volatility and nearness to the 52-week high. "
          "quality_value / momentum: single-idea baselines.\n"
          "mean_ic: rank correlation with next month's return (0.02-0.05 is useful). ic_t above ~2: unlikely luck. "
          "top10_per_year: holding the top 10% each month, after 0.1% trading costs each way, vs average_stock "
          "(every stock in the universe, equal weight). Momentum can't be measured for companies that disappeared, "
          "so its numbers still leave them out.")
    passed, text = ml.verdict(card, args.split_year)
    print(f"\nVerdict: {text}")
    ranking, imp = ml.today(df, trees=args.trees)
    print("\nWhat the model leans on most (share of its total gain):")
    print("  " + ", ".join(f"{k} {v:.0%}" for k, v in imp.head(12).items()))
    # The last month in the table can be a partial one; the ranking uses its latest trading day.
    latest = con.execute("SELECT MAX(date) FROM prices WHERE ticker IN (SELECT ticker FROM tickers "
                         "WHERE last_date IS NOT NULL)").fetchone()[0][:10]
    print(f"\nRanking as of {latest} ({'model passed' if passed else 'model NOT passed - research only'}):")
    print("  Top 15:    " + ", ".join(ranking.index[:15]))
    print("  Bottom 15: " + ", ".join(ranking.index[-15:]))
    ml.save(con, ranking, latest, card, passed, text)
    from trend_bot import ideas

    ideas.save_scorecard(con, card, preds)
    print(f"Top ideas will use: {ideas.method(con)}{' + calm stocks and 52-week high' if ideas.extra(con) else ''} "
          "(each change only if it ranked better in both halves).")
    new = track.record_ml(con, ranking, latest)
    print(f"\nRecorded {len(new)} new picks (top and bottom 10%) for forward tracking: see 'python -m trend_bot track'.")


def _study_ideas(con, args: argparse.Namespace) -> None:
    from trend_bot import factors, ideas, model
    from trend_bot.db import load_prices

    if not con.execute("SELECT 1 FROM facts LIMIT 1").fetchone():
        print("No company financials yet. Run:  python -m trend_bot db update --fundamentals-only")
        return
    since = args.since if args.since != "2006-01-01" else "2009-06-30"  # XBRL filings start in 2009
    print("Building the monthly table of every signal (takes a while)...")
    df = factors.table(con, since=since, universe=args.universe)
    spy = load_prices(con, args.benchmark)["Close"]
    if df.empty or spy.empty:
        print("Not enough data.")
        return
    print(_gone_note(df))
    monthly = ideas.backtest(df, spy, hold=args.hold, buffer=args.buffer, cash_rate=args.cash_rate,
                             extra=ideas.extra(con))
    if monthly.empty:
        print("Not enough data.")
        return
    cols = [c for c in ideas.NAMES if c in monthly]
    fmt = {"cagr": "{:+.1%}".format, "volatility": "{:.0%}".format, "max_drawdown": "{:.0%}".format,
           "return_per_risk": "{:.2f}".format, "total": "{:+.0%}".format}
    print(f"\nTop {args.hold} ideas of the {args.universe:,} most traded stocks, equal weight, checked monthly, "
          f"kept while in the top {args.buffer}; 0.1% per trade; cash earns {args.cash_rate:g}%/yr when the weather "
          "filter says so (S&P 500 below its 200-day average).")
    split = pd.Timestamp(f"{args.split_year}-01-01")
    for label, part in (("All years", monthly), (f"before {args.split_year}", monthly[monthly.index < split]),
                        (f"{args.split_year} on", monthly[monthly.index >= split])):
        if part.empty:
            continue
        s = model.summarize(part, cols).rename(index=ideas.NAMES)
        print(f"\n== {label}: {part.index[0]:%Y-%m} to {part.index[-1]:%Y-%m} ==\n" + s.to_string(formatters=fmt))
    yearly = (1 + monthly[["mix", "mix_always", "mix_capped_always", "SPY"]]).groupby(monthly.index.year).prod(
        min_count=1) - 1
    print("\nYear by year:\n" + yearly.rename(columns=ideas.NAMES).rename_axis("year").to_string(
        float_format=lambda v: f"{v:+.0%}", na_rep="–"))
    beat = (yearly["mix"] > yearly["SPY"]).mean()
    print(f"\nIn the market {monthly['invested'].mean():.0%} of months; about "
          f"{monthly['mix_turnover'].mean():.0%} of the portfolio changed each month; the top ideas (+ weather) beat "
          f"the S&P 500 in {beat:.0%} of years.")
    print("return_per_risk = yearly growth divided by volatility (higher is better). Companies that disappeared "
          "only count in their last month, so the real past was a little worse than this for every stock list.")
    ideas.save_backtest(con, monthly, args.hold)
    print(f"The portfolio to follow will {'use' if ideas.use_cap(con) else 'not use'} the cap of "
          f"{ideas.MAX_PER_INDUSTRY} per industry (used only if it gave more return per risk in both halves).")


def _study_forecast(con, args: argparse.Namespace) -> None:
    from trend_bot import forecast

    print("Building 20 years of training data (takes a few minutes)...")
    rows = forecast.training_rows(con, universe=args.universe, benchmark=args.benchmark)
    if rows.empty:
        print("Not enough data.")
        return
    preds = forecast.walk_forward(rows, first_year=args.first_year)
    if preds.empty:
        print("Not enough history before the first test year.")
        return
    card = forecast.scorecard(preds)
    sc = forecast.save_scorecard(con, preds, card)
    pct = lambda v: f"{v:.1%}"
    print(f"\n30-day outlook, tested year by year from {preds['month'].min():%Y} to {preds['month'].max():%Y}: "
          f"{len(preds):,} predictions, each made with only the years before it.\n")
    print("== How often it was right ==")
    print(card["summary"].to_string(formatters={c: pct for c in ("happened", "accuracy", "naive_accuracy")} |
                                    {"brier": "{:.4f}".format, "naive_brier": "{:.4f}".format}))
    print("naive = always guessing the more common outcome. brier = prediction error, lower is better;\n"
          "the model only adds something if it beats the naive numbers.")
    for key, label in (("calibration_beat", "beats the S&P 500"), ("calibration_up", "goes up")):
        print(f"\n== When it said X% chance it {label}, how often did it happen? ==")
        print(card[key].to_string(formatters={"predicted": pct, "happened": pct}))
    print("\n== Ranking stocks by chance to beat the S&P 500, every month ==")
    ideas = card["ideas"].copy()
    ideas["beat_spy_months"] = ideas["beat_spy_months"].map(lambda v: "–" if pd.isna(v) else f"{v:.0%}")
    print(ideas.to_string(formatters={"avg_month": "{:+.2%}".format}))
    print(f"The top 10% did better than the bottom 10% in {card['top_beat_bottom']:.0%} of months.")
    print("\nIn one sentence (shown with every outlook from now on):\n" + forecast.reliability_note(sc))
    print("\nNote: only companies still listed today are included (survivorship bias).")


def _study_model(con, args: argparse.Namespace) -> None:
    from trend_bot import model

    print("Scoring every stock at every month-end since the data starts (takes a few minutes)...")
    monthly = model.backtest(con, hold=args.hold, universe=args.universe, buffer=args.buffer,
                             cash_rate=args.cash_rate, benchmark=args.benchmark)
    if monthly.empty:
        print("Not enough data.")
        return
    b = args.benchmark
    names = {"model": "Model + weather filter", "model_no_weather": "Model, always invested",
             b: f"{b} (buy & hold)", f"{b}_weather": f"{b} + weather filter", "all_eligible": "All eligible stocks"}
    cols = list(names)
    fmt = {"cagr": "{:+.1%}".format, "volatility": "{:.0%}".format, "max_drawdown": "{:.0%}".format,
           "return_per_risk": "{:.2f}".format, "total": "{:+.0%}".format}
    print(f"\nTrend Score model: top {args.hold} of the {args.universe:,} most traded stocks, checked monthly, "
          f"sell below rank {args.buffer}; 0.1% per trade; cash earns {args.cash_rate:g}%/yr")
    periods = [("All years", monthly.index.min(), monthly.index.max()),
               ("2006-2015 (check 1)", pd.Timestamp("2006-01-01"), pd.Timestamp("2015-12-31")),
               ("2016-now (check 2)", pd.Timestamp("2016-01-01"), monthly.index.max())]
    for label, start, end in periods:
        part = monthly[(monthly.index >= max(start, pd.Timestamp(args.since))) & (monthly.index <= end)]
        if part.empty:
            continue
        s = model.summarize(part, cols).rename(index=names)
        print(f"\n== {label}: {part.index[0]:%Y-%m} to {part.index[-1]:%Y-%m} ==")
        print(s.to_string(formatters=fmt))
    shown = monthly[monthly.index >= pd.Timestamp(args.since)]
    print(f"\nIn the market {shown['invested'].mean():.0%} of months; "
          f"on average {shown['turnover'].mean():.0%} of the portfolio changed each month.")
    # min_count=1 keeps years without benchmark data blank instead of showing +0%.
    yearly = (1 + shown[["model", "model_no_weather", b, f"{b}_weather"]]).groupby(
        shown.index.year).prod(min_count=1) - 1
    print("\nYear by year:\n" + yearly.rename(columns=names).rename_axis("year").to_string(
        float_format=lambda v: f"{v:+.0%}", na_rep="–"))
    print("\nreturn_per_risk = yearly growth divided by volatility (higher is better).")


def _study_momentum(con, args: argparse.Namespace) -> None:
    from trend_bot import momentum

    print("Loading month-end prices for every stock (takes a minute)...")
    closes, dollar_vol = momentum.monthly_panel(con)
    monthly = momentum.backtest(closes, dollar_vol, lookback=args.lookback, skip=1, top=args.top,
                                min_price=args.min_price, min_dollar_vol=args.min_volume)
    monthly = monthly[monthly.index >= pd.Timestamp(args.since)]
    if args.until:
        monthly = monthly[monthly.index <= pd.Timestamp(args.until)]
    if monthly.empty:
        print("Not enough data.")
        return
    monthly = momentum.add_benchmark(con, monthly, args.benchmark)
    top_label, bottom_label = f"Top {args.top:.0%} (winners)", f"Bottom {args.top:.0%} (losers)"
    monthly = monthly.rename(columns={"top": top_label, "bottom": bottom_label, "all": "All stocks (equal)"})
    cols = [top_label, "All stocks (equal)", bottom_label, args.benchmark]
    print(f"\nMomentum: rank by the {args.lookback}-month return (skipping the latest month), "
          f"rebalance monthly, 0.1% cost per trade")
    print(f"{monthly.index[0]:%Y-%m} to {monthly.index[-1]:%Y-%m}, on average "
          f"{monthly['stocks'].mean():,.0f} eligible stocks a month\n")
    table = momentum.stats(monthly, cols, base="All stocks (equal)")
    table["beat_all_months"] = table["beat_all_months"].map(lambda v: "–" if pd.isna(v) else f"{v:.0%}")
    print(table.to_string(formatters={"cagr": "{:+.1%}".format, "volatility": "{:.0%}".format,
                                      "max_drawdown": "{:.0%}".format, "total": "{:+.0%}".format}))
    print("\ncagr = yearly growth, beat_all_months = share of months it beat the all-stocks average")
    print("\nYear by year:\n" + momentum.yearly(monthly, cols).to_string(
        float_format=lambda v: f"{v:+.0%}"))


def cmd_study(args: argparse.Namespace) -> int:
    from trend_bot import db, study
    from trend_bot.screen import liquid_tickers

    strategy = build_strategy(args)
    with closing(db.connect(args.db)) as con:
        pct = lambda df: df.to_string(formatters={
            c: "{:+.1%}".format for c in ("avg_return", "median_return", "avg_vs_bench", "median_vs_bench")} | {
            c: "{:.0%}".format for c in ("win_rate", "beat_bench_rate")})
        print(f"Benchmark: {args.benchmark} (bought and sold on the same days as each stock)\n")
        if args.signal in ("insiders", "both"):
            events = study.cluster_events(con, window_days=args.cluster_days, min_buyers=args.cluster_min,
                                          since=args.since, min_value=args.min_value,
                                          officers_only=args.officers_only)
            if args.until:
                events = events[events["date"] <= pd.Timestamp(args.until)]
            rules = [f"{args.cluster_min}+ insiders within {args.cluster_days} days"]
            if args.min_value:
                rules.append(f"at least ${args.min_value:,.0f} bought")
            if args.officers_only:
                rules.append("executives only")
            period = f"{args.since} to {args.until}" if args.until else f"since {args.since}"
            print(f"Insider cluster buys {period} ({', '.join(rules)}): {len(events):,} events")
            ev = study.add_returns(con, events, strategy=strategy, benchmark=args.benchmark)
            if not ev.empty:
                print("\nAll clusters (buy the day after the filing is public):\n" + pct(study.summarize(ev, min_price=args.min_price)))
                for trend in ("UP", "DOWN"):
                    part = ev[ev["trend"] == trend]
                    print(f"\n...when the price trend was {trend} ({strategy.label}):\n"
                          + pct(study.summarize(part, min_price=args.min_price)))
                _print_fates(con, study, events, ev, args, pct)
        if args.signal == "momentum":
            _study_momentum(con, args)
            return 0
        if args.signal == "model":
            _study_model(con, args)
            return 0
        if args.signal == "forecast":
            _study_forecast(con, args)
            return 0
        if args.signal == "factors":
            _study_factors(con, args)
            return 0
        if args.signal == "ml":
            _study_ml(con, args)
            return 0
        if args.signal == "ideas":
            _study_ideas(con, args)
            return 0
        if args.signal in ("trend", "both"):
            tickers = liquid_tickers(con, args.min_price, args.min_volume)
            if args.max_tickers:
                tickers = tickers[: args.max_tickers]
            events = study.trend_flip_events(con, strategy, tickers, since=args.since)
            if args.until:
                events = events[events["date"] <= pd.Timestamp(args.until)]
            print(f"\nTrend BUY flips ({strategy.label}) in {len(tickers):,} stocks since {args.since}: "
                  f"{len(events):,} events")
            ev = study.add_returns(con, events, benchmark=args.benchmark)
            if not ev.empty:
                print(pct(study.summarize(ev, min_price=args.min_price)))
    print("\nNote: companies without price data only count where their SEC filings show a bankruptcy or buyout;\n"
          "the rest are left out, so results may still be a little optimistic.")
    return 0


def cmd_track(args: argparse.Namespace) -> int:
    from trend_bot import db, track

    with closing(db.connect(args.db)) as con:
        perf = track.performance(con, benchmark=args.benchmark, signal=args.signal)
    if perf.empty:
        print("No picks recorded yet. The nightly 'alert --market' run records them (or: screen --record).")
        return 0
    print(f"Track record of {len(perf):,} picks since {perf['date'].min()}  (vs {args.benchmark})\n")
    s = track.summary(perf)
    print(s.to_string(formatters={
        "avg_days": "{:.0f}".format, "avg_return": "{:+.1%}".format, "median_return": "{:+.1%}".format,
        "win_rate": "{:.0%}".format, "avg_vs_bench": "{:+.1%}".format, "beat_bench_rate": "{:.0%}".format}))
    print("\n(For 'New downtrend' and 'Model sell' a negative return means the sell signal was right.)")
    recent = perf.sort_values("date", ascending=False).head(args.limit).copy()
    recent["signal"] = recent["signal"].map(lambda s: track.SIGNALS.get(s, s))
    for col in ("return", "vs_bench"):
        recent[col] = recent[col].map(lambda v: f"{v:+.1%}" if pd.notna(v) else "–")
    print(f"\nMost recent {len(recent)} picks:\n" + recent[["date", "ticker", "signal", "entry", "now", "return",
                                                            "vs_bench", "detail"]].to_string(index=False))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from trend_bot import db
    from trend_bot.report import build_report

    if not Path(args.db).exists():
        print(f"{args.db} not found. Build it first:  python -m trend_bot db update", file=sys.stderr)
        return 1
    strategy = build_strategy(args)
    with closing(db.connect(args.db)) as con:
        latest = con.execute("SELECT MAX(last_date) FROM tickers").fetchone()[0]
        new = {(t, s) for t, s in con.execute("SELECT ticker, signal FROM picks WHERE date = ?", (latest,))}
        page = build_report(con, strategy, benchmark=args.benchmark, new=new, min_price=args.min_price,
                            min_dollar_vol=args.min_volume, cluster_days=args.cluster_days,
                            min_buyers=args.cluster_min)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dated = out / f"report-{latest}.html"
    for path in (dated, out / "latest.html"):
        path.write_text(page, encoding="utf-8")
    print(f"Report saved to {dated.resolve()}")
    if args.open and sys.stdout.isatty():
        open_file(dated)
    return 0


def cmd_model(args: argparse.Namespace) -> int:
    from trend_bot import db, model

    with closing(db.connect(args.db)) as con:
        res = model.update_portfolio(con, hold=args.hold, universe=args.universe, buffer=args.buffer,
                                     force=args.rebalance)
        if res["rebalanced"] and not args.no_record:
            _record_model_picks(con, res)
    _print_model(res, args.top)
    return 0


def _record_model_picks(con, res: dict) -> None:
    for signal, tickers in (("model_buy", res["buys"]), ("model_sell", res["sells"])):
        for t in tickers:
            price = res["scores"]["close"].get(t)
            if price is None:
                price = con.execute("SELECT last_close FROM tickers WHERE ticker = ?", (t,)).fetchone()[0]
            con.execute("INSERT OR IGNORE INTO picks VALUES (?, ?, ?, ?, ?)",
                        (res["date"], t, signal, float(price), "Trend Score model"))
    con.commit()


def _print_model(res: dict, top: int) -> None:
    weather = ("☀️  INVEST: the S&P 500 is above its 200-day average" if res["weather"] == "invest"
               else "🌧️  CAUTION: the S&P 500 is below its 200-day average - the model says hold cash")
    print(f"Trend Score model · {res['date']}\nMarket weather: {weather}")
    if res["weather_changed"]:
        print("   (the weather changed since the last run)")
    if res["rebalanced"]:
        print(f"\nMonthly check done today. Buy: {', '.join(res['buys']) or 'nothing'}  |  "
              f"Sell: {', '.join(res['sells']) or 'nothing'}")
    else:
        print("\nNo changes: the portfolio is checked on the first run of each month (or use --rebalance).")
    h = res["holdings"]
    if len(h):
        show = h[["since", "entry_price", "close", "return", "score_now"]].copy()
        show["return"] = show["return"].map(lambda v: f"{v:+.1%}" if pd.notna(v) else "–")
        show["score_now"] = show["score_now"].map(lambda v: f"{v:.0f}" if pd.notna(v) else "out of universe")
        print(f"\nModel portfolio ({len(h)} stocks, equal amounts):\n" + show.to_string(float_format="{:,.2f}".format))
    s = res["scores"].head(top).copy()
    if len(s):
        s = s[["score", "trend", "momentum", "high52", "steadiness", "insiders", "buyers", "close"]]
        print(f"\nTop {len(s)} scores today (parts are 0-1, higher is better):\n" + s.to_string(
            float_format="{:.2f}".format))


def cmd_forecast(args: argparse.Namespace) -> int:
    from trend_bot import db, forecast, model, track

    with closing(db.connect(args.db)) as con:
        models, trained_on = forecast.load_models(con, retrain=args.retrain)
        preds, date, _ = forecast.predict_today(con, models)
        if preds.empty:
            print("No stocks to score yet.")
            return 1
        if not args.no_record:
            track.record_forecast(con, preds, date)
        ws = model.weather_status(con)
        note = forecast.reliability_note(forecast.load_scorecard(con))
    _print_forecast(preds, date, ws, trained_on, note, args.top)
    return 0


def cmd_ideas(args: argparse.Namespace) -> int:
    from trend_bot import db, ideas, model, track

    if not Path(args.db).exists():
        print(f"{args.db} not found. Build it first:  python -m trend_bot db update", file=sys.stderr)
        return 1
    with closing(db.connect(args.db)) as con:
        if not con.execute("SELECT 1 FROM facts LIMIT 1").fetchone():
            print("No company financials yet. Run:  python -m trend_bot db update --fundamentals-only")
            return 1
        print("Ranking the most traded stocks on every signal...")
        ranked, date = ideas.latest(con, universe=args.universe)
        if ranked.empty:
            print("Not enough data.")
            return 1
        ws = model.weather_status(con)
        port = None
        if not args.no_record:
            ideas.save(con, ranked, date, top=args.top, bottom=args.bottom)
            new = track.record_ml(con, ranked, date, prefix="mix")
            port = ideas.update_portfolio(con, ranked, date, ws, hold=args.hold, buffer=args.buffer)
            track.record_ideas_trades(con, port)
        note = ideas.reliability_note(con)
    from trend_bot.model import weather_text

    show = lambda df: df.assign(rank=df["pct"].map("{:.0%}".format), sector=df["sector"].fillna("-"))[
        ["close", "rank", "sector", "why"]].to_string(
        float_format="{:,.2f}".format)
    print(f"\nTop ideas · {date} · {len(ranked):,} stocks ranked\n\nMarket weather: {weather_text(ws)}\n")
    print(f"== ⚠️  Most likely to lag ==\n{show(ranked.tail(args.bottom).iloc[::-1])}")
    print(f"\n== 💡 Top ideas ==\n{show(ranked.head(args.top))}")
    if port:
        print(f"\n== 📋 The portfolio to follow (top {args.hold}, sell below rank {args.buffer}) ==")
        print("  🟢 Buy:  " + (", ".join(p["ticker"] for p in port["buys"]) or "nothing new"))
        print("  🔴 Sell: " + (", ".join(p["ticker"] for p in port["sells"]) or "nothing"))
        print("  Hold:    " + (", ".join(p["ticker"] for p in port["holds"]) or "-"))
        if not port["invest"]:
            print("  The weather filter says cash: the tested version held cash while the S&P 500 is below its "
                  "200-day average.")
    print(f"\nHow reliable is this? {note}")
    if not args.no_record:
        print(f"Recorded {len(new)} new picks (top and bottom 10%) for forward tracking ('track').")
    print("A starting point for your own research, not financial advice.")
    return 0


def _print_forecast(preds: pd.DataFrame, date: str, ws: dict, trained_on: str, note: str, top: int) -> None:
    from trend_bot.model import weather_text

    print(f"Outlook · {date}\n\nMarket weather: {weather_text(ws)}\n")
    show = lambda df: df.assign(p_up=df["p_up"].map("{:.0%}".format), p_beat=df["p_beat"].map("{:.0%}".format))[
        ["close", "p_beat", "p_up", "why"]].to_string(float_format="{:,.2f}".format)
    print(f"== ⚠️  Most likely to lag the S&P 500 over the next 30 days ==\n{show(preds.tail(top).iloc[::-1])}")
    print(f"\n== 💡 Ideas to research (highest odds, but see below) ==\n{show(preds.head(top))}")
    print(f"\nHow reliable is this? {note}")
    print(f"Learned from {trained_on}. Odds, not certainties; not financial advice.")


def cmd_health(args: argparse.Namespace) -> int:
    """Check the bot's own health; with --discord, warn there (only when something changed)."""
    from trend_bot import alerts, db, health

    if not Path(args.db).exists():
        problems, kind = [f"{args.db} not found: the database didn't carry over to this run."], "problems"
    else:
        with closing(db.connect(args.db)) as con:
            problems = health.check(con, failed_steps=health.read_problems_file(args.problems),
                                    job_status=args.job_status)
            kind = health.to_send(con, problems) if args.discord else None
    print("Bot health: all fine." if not problems else "Bot health problems:\n" + "\n".join(f"  - {p}" for p in problems))
    if args.discord and kind:
        webhook = alerts.load_webhook()
        if webhook:
            alerts.send(webhook, {"username": "Trend Bot", "embeds": [health.embed(kind, problems, args.run_url)]})
            print(f"Sent the health {'warning' if kind == 'problems' else 'all-clear'} to Discord.")
    return 0  # never fail the run over this


def cmd_send_report(args: argparse.Namespace) -> int:
    from trend_bot import alerts

    webhook = args.webhook or alerts.load_webhook()
    path = Path(args.path)
    if not webhook:
        print("No Discord webhook set (DISCORD_WEBHOOK_URL).", file=sys.stderr)
        return 2
    if not path.exists():
        print(f"{path} not found - run 'python -m trend_bot report' first.", file=sys.stderr)
        return 1
    alerts.send_file(webhook, path, args.message)
    print(f"Sent {path.name} to Discord.")
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
    al.add_argument("--forecast", action="store_true",
                    help="once a week, add the 30-day outlook's top ideas (needs the database)")
    al.add_argument("--model", action="store_true",
                    help="add the Trend Score model's monthly buys/sells and weather changes (needs the database)")
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
    dbp.add_argument("--sec-lookups", type=int, default=1500,
                     help="max company lookups per run for the one-time catch-ups (default 1500)")
    dbp.add_argument("--no-fundamentals", action="store_true", help="skip the weekly company-financials refresh")
    dbp.add_argument("--no-short", action="store_true", help="skip the FINRA short selling data")
    dbp.add_argument("--ignore-sec-pause", action="store_true",
                     help="try the SEC even if it asked us to slow down in the last 24 hours")
    dbp.add_argument("--insiders-all-companies", action="store_true",
                     help="read recent filings for every company, not just ones the screen can show")
    only = dbp.add_mutually_exclusive_group()
    only.add_argument("--universe-only", action="store_true")
    only.add_argument("--prices-only", action="store_true")
    only.add_argument("--insiders-only", action="store_true")
    only.add_argument("--short-only", action="store_true", help="only update FINRA short selling data")
    only.add_argument("--fates-only", action="store_true",
                      help="only look up what happened to companies without prices (use a big --sec-lookups "
                           "to catch up in one go)")
    only.add_argument("--fundamentals-only", action="store_true",
                      help="refresh company financials now (normally weekly, as part of db update)")
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
    sc.add_argument("--include-funds", action="store_true", help="also show closed-end funds and ETFs")
    sc.add_argument("--record", action="store_true", help="save today's picks for 'track' (alert --market does this)")
    sc.set_defaults(func=cmd_screen)

    st = sub.add_parser("study", parents=[common, market], help="test a signal on the whole market's history")
    st.add_argument("signal", choices=["insiders", "trend", "both", "momentum", "model", "forecast", "factors", "ml",
                                       "ideas"])
    st.add_argument("--split-year", type=int, default=2018, help="factors: compare before/after this year")
    st.add_argument("--trees", type=int, default=300, help="ml: boosting rounds (default 300)")
    st.add_argument("--first-year", type=int, default=2011,
                    help="forecast: first year to predict (each year uses only earlier years)")
    st.add_argument("--since", default="2006-01-01", help="first event date")
    st.add_argument("--until", help="last event date, e.g. 2015-12-31 (to test on one period, confirm on another)")
    st.add_argument("--max-tickers", type=int, help="trend study: limit the number of stocks (faster)")
    st.add_argument("--top", type=float, default=0.1, help="momentum: share of stocks to hold (default 0.1 = top 10%%)")
    st.add_argument("--lookback", type=int, default=12, help="momentum: months of past return to rank by")
    st.add_argument("--hold", type=int, default=20, help="model / ideas: stocks to hold (default 20)")
    st.add_argument("--universe", type=int, default=1000, help="model: most traded stocks to choose from")
    st.add_argument("--buffer", type=int, default=40, help="model: sell a stock once it falls below this rank")
    st.add_argument("--cash-rate", type=float, default=2.0, help="model: yearly %% earned while in cash")
    st.add_argument("--min-value", type=float, default=0,
                    help="insiders study: only clusters where insiders bought at least this many $ in total")
    st.add_argument("--officers-only", action="store_true",
                    help="insiders study: only count buys by executives (CEO, CFO, president...)")
    st.add_argument("--benchmark", default="SPY",
                    help="compare against this ticker (default SPY; IWM = small companies, QQQ = Nasdaq-100)")
    st.set_defaults(func=cmd_study)

    rp = sub.add_parser("report", parents=[common, market], help="write today's market report as a web page")
    rp.add_argument("--out", default="reports", help="folder for the report (default: reports)")
    rp.add_argument("--benchmark", default="SPY", help="benchmark for the track record (default SPY)")
    rp.add_argument("--open", action="store_true", help="open the report in your browser")
    rp.set_defaults(func=cmd_report)

    md = sub.add_parser("model", help="Trend Score model: today's portfolio, buy/sell signals, market weather")
    md.add_argument("--db", default="market.db", help="database file (default: market.db)")
    md.add_argument("--hold", type=int, default=20, help="stocks to hold (default 20)")
    md.add_argument("--universe", type=int, default=1000, help="most traded stocks to choose from")
    md.add_argument("--buffer", type=int, default=40, help="sell a stock once it falls below this rank")
    md.add_argument("--top", type=int, default=30, help="top scores to list")
    md.add_argument("--rebalance", action="store_true", help="do the monthly check now")
    md.add_argument("--no-record", action="store_true", help="don't save buys/sells for 'track'")
    md.set_defaults(func=cmd_model)

    fc = sub.add_parser("forecast", help="30-day outlook: chance each stock goes up / beats the market")
    fc.add_argument("--db", default="market.db", help="database file (default: market.db)")
    fc.add_argument("--top", type=int, default=15, help="ideas to list at each end")
    fc.add_argument("--retrain", action="store_true", help="relearn from history now (otherwise monthly)")
    fc.add_argument("--no-record", action="store_true", help="don't save the ideas for 'track'")
    fc.set_defaults(func=cmd_forecast)

    ide = sub.add_parser("ideas", help="monthly top ideas: every signal ranked and averaged (the simple mix)")
    ide.add_argument("--db", default="market.db", help="database file (default: market.db)")
    ide.add_argument("--universe", type=int, default=1000, help="most traded stocks to rank (default 1000)")
    ide.add_argument("--top", type=int, default=15, help="top ideas to list")
    ide.add_argument("--bottom", type=int, default=10, help="most-likely-to-lag stocks to list")
    ide.add_argument("--hold", type=int, default=20, help="stocks in the portfolio to follow (default 20)")
    ide.add_argument("--buffer", type=int, default=40, help="sell a holding once it falls below this rank")
    ide.add_argument("--no-record", action="store_true",
                     help="don't save the list, update the portfolio or record picks for 'track'")
    ide.set_defaults(func=cmd_ideas)

    hp = sub.add_parser("health", help="check the bot's own health (fresh prices, data, SEC pause)")
    hp.add_argument("--db", default="market.db", help="database file (default: market.db)")
    hp.add_argument("--discord", action="store_true", help="post a warning (or all-clear) when something changed")
    hp.add_argument("--problems", help="file listing steps that didn't finish (one per line)")
    hp.add_argument("--job-status", help="the cloud run's status so far (success/failure)")
    hp.add_argument("--run-url", help="link to the cloud run, shown in the warning")
    hp.set_defaults(func=cmd_health)

    sr = sub.add_parser("send-report", help="post a file (the HTML report by default) to Discord")
    sr.add_argument("--path", default="reports/latest.html")
    sr.add_argument("--message", default="📄 Today's market report (open the file in your browser)")
    sr.add_argument("--webhook", help="Discord webhook URL (default: DISCORD_WEBHOOK_URL)")
    sr.set_defaults(func=cmd_send_report)

    tr = sub.add_parser("track", help="how the stocks the bot flagged have done since")
    tr.add_argument("--db", default="market.db", help="database file (default: market.db)")
    tr.add_argument("--benchmark", default="SPY", help="compare against this ticker (default SPY)")
    tr.add_argument("--signal", choices=["strong_insider", "insider_cluster", "uptrend", "downtrend"])
    tr.add_argument("--limit", type=int, default=20, help="recent picks to list")
    tr.set_defaults(func=cmd_track)

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
