"""Everyday outputs: track, report, model, forecast, ideas, health, send-guide, send-report."""

from __future__ import annotations

import argparse
import sys
from contextlib import closing
from pathlib import Path

import pandas as pd

from trend_bot.commands.common import (
    build_strategy,
    open_file,
)


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
        from trend_bot import runs

        runs.note("stocks_ranked", len(ranked))
        runs.note("ranking_date", date)
        if not ranked.empty:
            from trend_bot import coverage

            coverage.save(con, ranked, date)
            runs.note("left_out_too_few_measures", len(ranked.attrs.get("too_few", {})))
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
        with closing(db.connect(args.db)) as con:
            live = ideas.live_line(con)
        if live:
            print("  " + live.replace("**", ""))
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
            catchup = health.catchup_embed(con)
            if catchup and args.discord and alerts.load_webhook():
                try:
                    alerts.send(alerts.load_webhook(), {"username": "Trend Bot", "embeds": [catchup]})
                    db.set_meta(con, "sec_catchup", "")
                    con.commit()
                    print("Sent 'SEC data is back' to Discord.")
                except Exception as e:  # noqa: BLE001 - never fail the run over this; tried again tomorrow
                    print(f"Could not send 'SEC data is back': {e}")
    print("Bot health: all fine." if not problems else "Bot health problems:\n" + "\n".join(f"  - {p}" for p in problems))
    if args.discord and kind:
        webhook = alerts.load_webhook()
        if webhook:
            alerts.send(webhook, {"username": "Trend Bot", "embeds": [health.embed(kind, problems, args.run_url)]})
            print(f"Sent the health {'warning' if kind == 'problems' else 'all-clear'} to Discord.")
    return 0  # never fail the run over this


def cmd_send_guide(args: argparse.Namespace) -> int:
    """Post GUIDE.md (how to read the bot's messages) to Discord; with --once, only when it changed."""
    import hashlib

    from trend_bot import alerts, db

    path = Path(args.path)
    if not path.exists():
        print(f"{path} not found.", file=sys.stderr)
        return 1
    digest = hashlib.sha1(path.read_bytes()).hexdigest()
    if args.once and Path(args.db).exists():
        with closing(db.connect(args.db)) as con:
            if db.get_meta(con, "guide_sent") == digest:
                print("The guide hasn't changed since it was last posted.")
                return 0
    webhook = alerts.load_webhook()
    if not webhook:
        print("No Discord webhook set (DISCORD_WEBHOOK_URL).", file=sys.stderr)
        return 2
    alerts.send_file(webhook, path, "📖 **How to read the bot's messages** (pin this one). "
                                    "Open the file for the full guide.")
    if Path(args.db).exists():
        with closing(db.connect(args.db)) as con:
            db.set_meta(con, "guide_sent", digest)
            con.commit()
    print(f"Sent {path.name} to Discord.")
    return 0


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


def cmd_send_email(args: argparse.Namespace) -> int:
    """Email the daily report with a short summary (only when EMAIL_USER and EMAIL_APP_PASSWORD are set)."""
    from trend_bot import db, email_report, health

    cfg = email_report.settings()
    if not cfg:
        print("Email isn't set up (EMAIL_USER / EMAIL_APP_PASSWORD); skipped.")
        return 0
    summary, chart, title, demo = [], None, None, None
    if Path(args.db).exists():
        from trend_bot import accounts, charts, ideas

        with closing(db.connect(args.db)) as con:
            problems = health.check(con, failed_steps=health.read_problems_file(args.problems))
            summary = email_report.digest(con, problems)
            curve = ideas.growth_curve(con)
            if curve is not None:
                chart = charts.growth_png(curve)
                title = f"What $10,000 would have become in testing ({curve.index[0]:%Y}–{curve.index[-1]:%Y})"
            demo = accounts.chart_png(accounts.load(con))
    report = Path(args.path)
    msg = email_report.build(cfg, summary, report if report.exists() else None, chart=chart, demo_chart=demo,
                             **({"chart_title": title} if title else {}))
    if args.dry_run:
        print(msg)
        return 0
    try:
        email_report.send(cfg, msg)
    except Exception as e:  # noqa: BLE001 - a failed email must not fail the nightly run
        print(f"Could not send the email: {e}", file=sys.stderr)
        return 1
    print(f"Emailed the report to {cfg['EMAIL_TO']}.")
    return 0


def cmd_daytrade(args: argparse.Namespace) -> int:
    """Day-trading demo account (fake money, its own paper account): one part of today's session."""
    import datetime as dt
    import json

    from trend_bot import alerts, daytrade, intraday

    broker = daytrade.DayBroker.from_env()
    if broker is None:
        print(f"The day-trading account isn't set up ({daytrade.KEY_ENV} / {daytrade.SECRET_ENV}); skipped.")
        return 0
    now = dt.datetime.now(daytrade.NY)
    phase = args.phase
    if phase == "safety":   # the independent safety net: only in its windows, so a summer/winter twin is harmless
        t = now.time()
        if not (dt.time(15, 50) <= t < dt.time(15, 59) or t >= dt.time(16, 2) or t < dt.time(9, 29)):
            print(f"[day] safety net: {now:%H:%M} New York is outside its windows; nothing to do")
            return 0
        alert = daytrade.safety_net(broker, now)
        print(alert["description"] if alert else "[day] safety net: nothing left open")
        if alert and args.discord and alerts.load_webhook():
            alerts.send(alerts.load_webhook(), {"username": "Trend Bot", "embeds": [alert]})
        return 0
    if phase == "auto":   # two schedules per part (summer and winter time): only the right one runs
        phase = ("morning" if dt.time(9, 0) <= now.time() < dt.time(9, 50) else
                 "afternoon" if dt.time(12, 0) <= now.time() < dt.time(12, 50) else "")
        if not phase:
            print(f"[day] {now:%H:%M} New York time is not a start time; nothing to do (the other schedule runs).")
            return 0
    if phase in ("morning", "afternoon"):
        clock = broker.clock()
        if not (clock.get("is_open") or str(clock.get("next_open", ""))[:10] == f"{now:%Y-%m-%d}"):
            print("[day] the market is closed today (weekend or holiday).")
            return 0
    meta = {}
    if Path(args.intraday_db).exists():
        with closing(intraday.connect(args.intraday_db)) as ic:
            meta = {k: json.loads(v) for k, v in ic.execute("SELECT key, value FROM meta WHERE key IN ('universe', 'results')")}
    symbols = meta.get("universe") or intraday.ETFS + daytrade.FALLBACK_STOCKS
    data = intraday.Data.from_env(daytrade.KEY_ENV, daytrade.SECRET_ENV)
    state = daytrade.load_state(args.state)
    session = daytrade.Session(broker, data, symbols, now.date(), share=state.get("share"),
                               paused=state.get("paused"))
    try:
        if phase == "morning":
            session.morning()
        elif phase == "afternoon":
            session.afternoon()
    finally:   # what each order expected, for the trade log (kept even if the session stops early)
        state.setdefault("pending", []).extend(session.entries)
        Path(args.state).write_text(json.dumps(state, indent=1))
    if phase in ("afternoon", "report"):
        state, embed = daytrade.report(session.today_orders(), float(broker.account()["equity"]),
                                       state, now.date(), meta.get("results"))
        Path(args.state).write_text(json.dumps(state, indent=1))
        daytrade.write_csv(state["log"], args.log_csv)
        print(embed["description"].replace("**", ""))
        if args.discord and alerts.load_webhook():
            alerts.send(alerts.load_webhook(), {"username": "Trend Bot", "embeds": [embed]})
    return 0


def cmd_lookahead_combine(args: argparse.Namespace) -> int:
    """Combine look-ahead results from parallel jobs into one text (and say if anything leaked)."""
    from trend_bot import lookahead

    results = lookahead.from_json([Path(f).read_text() for f in args.files])
    print(f"Look-ahead check of {len(results)} months, checked in parallel:")
    print(lookahead.text(results))
    return 0 if lookahead.verdict(results)[0] else 1


def cmd_journal(args: argparse.Namespace) -> int:
    """Write the night's trading journal into SQL Server (Azure SQL; open it in SSMS)."""
    import json

    from trend_bot import db, intraday, journal

    conn = journal.connect()
    if conn is None:
        print("The trading journal isn't set up (AZURE_SQL_SERVER / AZURE_SQL_USER / AZURE_SQL_PASSWORD); skipped.")
        return 0
    day_state = json.loads(Path(args.day_state).read_text()) if Path(args.day_state).exists() else {}
    results = None
    if Path(args.intraday_db).exists():
        with closing(intraday.connect(args.intraday_db)) as ic:
            row = ic.execute("SELECT value FROM meta WHERE key = 'results'").fetchone()
            results = json.loads(row[0]) if row else None
    try:
        with closing(db.connect(args.db)) as con:
            journal.sync(conn, con, day_state, results)
    finally:
        conn.close()
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    """Both demo accounts' history (for the report's chart); with --weekly on Fridays, the week's summary."""
    import datetime as dt
    import json

    from trend_bot import accounts, alerts, daytrade, db, ideas, paper

    monthly, day = paper.Paper.from_env(), daytrade.DayBroker.from_env()
    if monthly is None and day is None:
        print("No demo account is set up; skipped.")
        return 0
    with closing(db.connect(args.db)) as con:
        hist = accounts.collect(con, monthly, day)
        live = ideas.live_line(con)
    for name, h in hist.items():
        print(f"[accounts] {name}: {len(h['values'])} days, last {h['values'][-1]:,.2f}")
    today = dt.date.today()
    if args.weekly and (today.weekday() == 4 or args.force):
        state = json.loads(Path(args.day_state).read_text()) if Path(args.day_state).exists() else {}
        e = accounts.weekly(hist, state, today, live)
        print(e["description"].replace("**", ""))
        if args.discord and alerts.load_webhook():
            alerts.send_with_image(alerts.load_webhook(), e, accounts.chart_png(hist))
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    """Post a short warning to Discord (for workflow steps that failed)."""
    from trend_bot import alerts

    if alerts.load_webhook():
        alerts.send(alerts.load_webhook(), {"username": "Trend Bot", "embeds": [
            {"title": args.title, "description": args.text[:4000], "color": alerts.RED}]})
    print(f"{args.title}: {args.text}")
    return 0


def cmd_safety(args: argparse.Namespace) -> int:
    """Nightly safety check of the portfolio's stocks: emergency exits and warnings, posted once each."""
    from trend_bot import alerts, db, safety

    with closing(db.connect(args.db)) as con:
        found = safety.check(con)
        import datetime as dt
        import json

        db.set_meta(con, "safety_last", json.dumps({**found, "date": dt.date.today().isoformat()}))   # for the journal
        con.commit()
    e = safety.embed(found, [])
    for kind in ("exit", "warn"):
        for t, why in found[kind]:
            print(f"[safety] {'EXIT' if kind == 'exit' else 'warning'} {t}: {why}")
    if not e:
        print("[safety] nothing new")
    elif args.discord and alerts.load_webhook():
        alerts.send(alerts.load_webhook(), {"username": "Trend Bot", "embeds": [e]})
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    """Demo account: follow the portfolio on Alpaca's paper-trading account (fake money)."""
    from trend_bot import db, ideas, paper

    client = paper.Paper.from_env()
    if client is None:
        print(f"The demo account isn't set up ({paper.KEY_ENV} / {paper.SECRET_ENV}); skipped. "
              "See GUIDE.md, 'Demo account'.")
        return 0
    if args.test_trade:
        try:
            return 0 if paper.test_trade(client) else 1
        except paper.PaperError as e:
            print(f"[demo] the broker refused: {e}", file=sys.stderr)
            return 1
    embeds = []
    with closing(db.connect(args.db)) as con:
        try:
            if args.morning:   # last night's orders as filled, then anything refused overnight again
                f = paper.fills(con, client)
                if f:
                    embeds.append(paper.fills_embed(f))
                    print(f"[demo] {len(f['filled'])} order(s) filled, {f['waiting']} waiting"
                          + (f"; cost of buying the next morning {f['cost']:+.2%}" if f["cost"] is not None else ""))
            res = paper.sync(con, client, dry_run=args.dry_run)
        except paper.PaperError as e:
            print(f"[demo] the broker refused: {e}", file=sys.stderr)
            return 1
        print(f"[demo] account value ${res['status']['equity']:,.0f}"
              + (f"; nothing to do: {res['skipped']}" if res["skipped"] else ""))
        for s, side, qty in res.get("planned", []):
            print(f"  would {side} {qty:,} {s}")
        for s, price in res.get("left_out", []):
            print(f"  left out {s}: one share (${price:,.0f}) costs more than twice its share of the account")
        line = paper.line(con)
        if line:
            print(line.replace("**", ""))
        port = ideas.load_portfolio(con)
        e = paper.embed(res, port["date"]) if port and not args.dry_run else None
        if e:
            embeds.append(e)
        if not args.dry_run:
            from trend_bot import safety

            alarms = safety.drawdown(con)
            if alarms:
                embeds.append(safety.embed({"exit": [], "warn": []}, alarms))
    if args.changed_flag and (embeds or res["traded"]):
        Path(args.changed_flag).write_text("1\n")   # the workflow saves the database only then
    if embeds and args.discord:
        from trend_bot import alerts

        if alerts.load_webhook():
            alerts.send(alerts.load_webhook(), {"username": "Trend Bot", "embeds": embeds})
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    """Why is (or isn't) a stock in the ranking? Plain answers; with --discord, posted there too."""
    from trend_bot import alerts, coverage, db

    if not Path(args.db).exists():
        print(f"{args.db} not found.")
        return 1
    with closing(db.connect(args.db)) as con:
        answers = [line for t in args.tickers for line in coverage.why(con, t)]
    print("\n".join(answers))
    if args.discord:
        webhook = alerts.load_webhook()
        if webhook:
            alerts.send(webhook, {"username": "Trend Bot", "embeds": [{
                "title": "🔎 Why is (or isn't) this stock ranked?", "description": "\n\n".join(answers)[:4000],
                "color": alerts.GRAY}]})
    return 0

