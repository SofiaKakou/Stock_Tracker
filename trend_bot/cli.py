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
import sys

from trend_bot.strategy import STRATEGIES, MACrossover
from trend_bot.commands.watchlist import (
    cmd_scan,
    cmd_backtest,
    cmd_portfolio,
    cmd_alert,
    cmd_news,
    cmd_insiders,
)
from trend_bot.commands.data import cmd_db, cmd_monthly_due, cmd_screen, cmd_sec_check
from trend_bot.commands.studies import cmd_study
from trend_bot.commands.ai import cmd_ai
from trend_bot.commands.outputs import (
    cmd_daytrade,
    cmd_lookahead_combine,
    cmd_notify,
    cmd_paper,
    cmd_safety,
    cmd_send_email,
    cmd_why,
    cmd_track,
    cmd_report,
    cmd_model,
    cmd_forecast,
    cmd_ideas,
    cmd_health,
    cmd_send_guide,
    cmd_send_report,
)


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
    dbp.add_argument("--no-reports", action="store_true", help="skip reading annual/quarterly reports")
    dbp.add_argument("--report-downloads", type=int, default=1500,
                     help="max annual/quarterly reports to read per run (default 1500; stops after 45 min either way)")
    dbp.add_argument("--no-short", action="store_true", help="skip the FINRA short selling data")
    dbp.add_argument("--ignore-sec-pause", action="store_true",
                     help="try the SEC even if it asked us to slow down in the last 20 hours")
    dbp.add_argument("--insiders-all-companies", action="store_true",
                     help="read recent filings for every company, not just ones the screen can show")
    dbp.add_argument("--max-age-hours", type=float, default=6,
                     help="prices: skip stocks refreshed less than this many hours ago (default 6)")
    dbp.add_argument("--flag-incomplete", metavar="FILE",
                     help="prices: write the reason to FILE if Yahoo left tonight's prices incomplete")
    only = dbp.add_mutually_exclusive_group()
    only.add_argument("--universe-only", action="store_true")
    only.add_argument("--prices-only", action="store_true")
    only.add_argument("--insiders-only", action="store_true")
    only.add_argument("--short-only", action="store_true", help="only update FINRA short selling data")
    only.add_argument("--reports-only", action="store_true",
                      help="only read annual/quarterly reports for the 'report changed' signal")
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
                                       "ideas", "candidates", "lookahead", "mllab", "audit", "intraday"])
    st.add_argument("--split-year", type=int, default=2018, help="factors: compare before/after this year")
    st.add_argument("--months", help="lookahead: month-ends to check, e.g. 2012-06-30,2021-06-30, or 'yearly' (one per year)")
    st.add_argument("--intraday-db", default="intraday.db", help="intraday: the half-hour bars database")
    st.add_argument("--json", help="lookahead: also save the results as JSON (to combine parallel runs)")
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

    sc2 = sub.add_parser("sec-check", help="one small SEC request: is the SEC still refusing us? (lifts the pause if not)")
    sc2.add_argument("--db", default="market.db", help="database file (default: market.db)")
    sc2.set_defaults(func=cmd_sec_check)

    md2 = sub.add_parser("monthly-due", help="list the monthly checks due tonight (used by the nightly run)")
    md2.add_argument("--db", default="market.db", help="database file (default: market.db)")
    md2.add_argument("--mark", action="store_true", help="record that this month's checks are done")
    md2.add_argument("--force", action="store_true", help="list every check (manual runs)")
    md2.set_defaults(func=cmd_monthly_due)

    wy = sub.add_parser("why", help="why is (or isn't) a stock in the ranking?")
    wy.add_argument("tickers", nargs="+", help="one or more tickers, e.g. AAPL BRK-B")
    wy.add_argument("--db", default="market.db", help="database file (default: market.db)")
    wy.add_argument("--discord", action="store_true", help="also post the answer to Discord")
    wy.set_defaults(func=cmd_why)

    em = sub.add_parser("send-email", help="email the daily report (needs EMAIL_USER and EMAIL_APP_PASSWORD)")
    em.add_argument("--db", default="market.db", help="database file (default: market.db)")
    em.add_argument("--path", default="reports/latest.html", help="report to attach (default: reports/latest.html)")
    em.add_argument("--problems", help="file listing steps that didn't finish (one per line)")
    em.add_argument("--dry-run", action="store_true", help="print the email instead of sending it")
    em.set_defaults(func=cmd_send_email)

    dtp = sub.add_parser("daytrade", help="day-trading demo account (fake money, its own Alpaca paper account)")
    dtp.add_argument("--phase", choices=["auto", "morning", "afternoon", "report", "safety"], default="auto",
                     help="auto: whichever part of the day it is now (for the schedule)")
    dtp.add_argument("--intraday-db", default="intraday.db", help="stocks to watch and the 10-year test results")
    dtp.add_argument("--state", default="daytrade.json", help="running totals since the first day")
    dtp.add_argument("--log-csv", default="daytrade_log.csv", help="the trade log as a spreadsheet file")
    dtp.add_argument("--discord", action="store_true", help="post the evening report")
    dtp.set_defaults(func=cmd_daytrade)

    lc = sub.add_parser("lookahead-combine", help="combine look-ahead results saved by parallel runs (--json)")
    lc.add_argument("files", nargs="+", help="JSON files from study lookahead --json")
    lc.set_defaults(func=cmd_lookahead_combine)

    nt = sub.add_parser("notify", help="post a short warning to Discord")
    nt.add_argument("--title", required=True)
    nt.add_argument("--text", required=True)
    nt.set_defaults(func=cmd_notify)

    sf = sub.add_parser("safety", help="nightly safety check of the portfolio's stocks (emergency exits, warnings)")
    sf.add_argument("--db", default="market.db", help="database file (default: market.db)")
    sf.add_argument("--discord", action="store_true", help="post what it finds")
    sf.set_defaults(func=cmd_safety)

    pp = sub.add_parser("paper", help="demo account: follow the portfolio on Alpaca paper trading (fake money)")
    pp.add_argument("--db", default="market.db", help="database file (default: market.db)")
    pp.add_argument("--dry-run", action="store_true", help="show the orders it would send, send nothing")
    pp.add_argument("--discord", action="store_true", help="post a message when it trades")
    pp.add_argument("--morning", action="store_true",
                    help="morning check: report last night's orders as filled, then retry anything refused")
    pp.add_argument("--changed-flag", metavar="FILE", help="write FILE if the database changed (for the workflow)")
    pp.add_argument("--test-trade", action="store_true",
                    help="buy 1 SPY share and sell it straight back (market hours only), to test orders")
    pp.set_defaults(func=cmd_paper)

    sg = sub.add_parser("send-guide", help="post GUIDE.md (how to read the bot's messages) to Discord")
    sg.add_argument("--path", default="GUIDE.md")
    sg.add_argument("--db", default="market.db", help="database file (remembers what was posted)")
    sg.add_argument("--once", action="store_true", help="only if the guide changed since it was last posted")
    sg.set_defaults(func=cmd_send_guide)

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

    ai = sub.add_parser("ai", help="Claude reads a sample of company reports (paid trial; needs ANTHROPIC_API_KEY)")
    ai.add_argument("action", choices=["sample", "collect", "summary"],
                    help="sample = score N new reports; collect = finish earlier batches; summary = results so far")
    ai.add_argument("--db", default="market.db", help="database file (default: market.db)")
    ai.add_argument("--n", type=int, default=100, help="reports to score (default 100)")
    ai.add_argument("--wait", type=int, default=7200, help="seconds to wait for the batch to finish (default 2 h)")
    ai.add_argument("--max-dollars", type=float, default=12.0,
                    help="refuse to send if the worst-case cost is above this (default $12)")
    ai.set_defaults(func=cmd_ai)
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
    from trend_bot import runs

    name = " ".join(str(x) for x in (args.command, getattr(args, "signal", None) or getattr(args, "action", None))
                    if x) if hasattr(args, "command") else args.func.__name__.removeprefix("cmd_")
    runs.start(getattr(args, "db", None), name, runs.settings_of(args))
    try:
        code = args.func(args)
    except BaseException as e:
        runs.finish(1, f"{type(e).__name__}: {e}")
        raise
    runs.finish(code or 0)
    return code
