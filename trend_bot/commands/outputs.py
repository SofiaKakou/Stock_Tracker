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
