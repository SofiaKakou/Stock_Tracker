"""Daily HTML report: today's picks, small price charts, and the track record.

A single self-contained file (no internet needed to view it) that follows the
system's light/dark setting.
"""

from __future__ import annotations

import datetime as dt
import html
import sqlite3

import pandas as pd

from trend_bot import track
from trend_bot.db import load_many
from trend_bot.screen import screen
from trend_bot.strategy import Strategy

CSS = """
:root {
  color-scheme: light;
  --bg: #fcfcfb; --card: #ffffff; --ink: #0b0b0b; --ink-2: #52514e; --muted: #8d8c86;
  --line: rgba(11,11,11,0.10); --up: #006300; --down: #b42f2f; --accent: #2a78d6;
  --gold-bg: #fff6e0; --gold: #8a5a00; --spark: #52514e;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --bg: #1a1a19; --card: #232322; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #8d8c86;
    --line: rgba(255,255,255,0.10); --up: #0ca30c; --down: #e66767; --accent: #3987e5;
    --gold-bg: #3a2f14; --gold: #f2c35b; --spark: #c3c2b7;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
       font: 15px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 1040px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 36px 0 10px; }
.sub { color: var(--ink-2); margin: 0 0 20px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
.tile .n { font-size: 26px; font-weight: 650; font-variant-numeric: tabular-nums; }
.tile .l { color: var(--ink-2); font-size: 13px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.card { background: var(--gold-bg); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
.card .t { font-weight: 650; font-size: 17px; }
.card .d { color: var(--ink-2); font-size: 13px; }
.badge { display: inline-block; font-size: 11px; font-weight: 700; letter-spacing: .04em; padding: 1px 6px;
         border-radius: 4px; background: var(--gold); color: var(--bg); margin-left: 6px; vertical-align: 2px; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line);
        border-radius: 10px; overflow: hidden; font-variant-numeric: tabular-nums; }
th, td { padding: 7px 10px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }
th { color: var(--ink-2); font-weight: 600; font-size: 13px; }
th:first-child, td:first-child, td.l, th.l { text-align: left; }
tr:last-child td { border-bottom: 0; }
.up { color: var(--up); } .down { color: var(--down); }
.empty { color: var(--muted); font-style: italic; }
.note { color: var(--muted); font-size: 13px; margin-top: 8px; }
svg.spark { display: block; }
.weather { border-radius: 12px; padding: 16px 20px; margin: 4px 0 20px; border: 1px solid var(--line); }
.weather.sun { background: color-mix(in srgb, var(--up) 12%, var(--card)); }
.weather.rain { background: color-mix(in srgb, var(--down) 12%, var(--card)); }
.weather .wl { font-size: 13px; color: var(--ink-2); font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
.weather .wt { font-size: 16px; font-weight: 600; margin-top: 4px; }
.start { background: var(--card); border: 2px solid var(--accent); border-radius: 14px; padding: 18px 20px;
         margin: 8px 0 24px; }
.start h2 { margin: 0 0 8px; }
.start p { margin: 6px 0; }
.start .todo { font-size: 17px; font-weight: 650; }
details.curious { margin-top: 40px; border-top: 1px solid var(--line); padding-top: 12px; }
details.curious > summary { cursor: pointer; font-size: 18px; font-weight: 650; padding: 8px 0; }
details.curious > summary .hint { font-weight: 400; color: var(--ink-2); font-size: 14px; }
.explain { color: var(--ink-2); font-size: 14px; margin: 0 0 10px; }
@media (max-width: 600px) {
  main { padding: 20px 12px 48px; }
  h1 { font-size: 21px; }
  .tile .n { font-size: 21px; }
  th, td { padding: 6px 7px; font-size: 14px; }
}
"""


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return "–"
    cls = "up" if v > 0 else "down" if v < 0 else ""
    return f'<span class="{cls}">{v:+.1%}</span>'


def _money(v: float) -> str:
    return f"${v / 1e6:,.1f}M" if v >= 1e6 else f"${v / 1e3:,.0f}K"


def sparkline(close: pd.Series, width: int = 120, height: int = 28) -> str:
    """Last ~6 months of prices as a tiny line chart; the end dot shows the direction."""
    s = close.dropna().iloc[-126:]
    if len(s) < 2:
        return ""
    lo, hi = float(s.min()), float(s.max())
    span = hi - lo or 1.0
    step = (width - 4) / (len(s) - 1)
    pts = [(2 + i * step, 2 + (height - 4) * (1 - (v - lo) / span)) for i, v in enumerate(s)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    up = s.iloc[-1] >= s.iloc[0]
    x, y = pts[-1]
    return (f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="6-month price {"up" if up else "down"} {s.iloc[-1] / s.iloc[0] - 1:+.0%}">'
            f'<polyline points="{path}" fill="none" stroke="var(--spark)" stroke-width="1.5" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="var({"--up" if up else "--down"})"/></svg>')


def _table(headers: list[str], rows: list[list[str]], left: set[int] = frozenset({0, 1})) -> str:
    if not rows:
        return '<p class="empty">None today.</p>'
    th = "".join(f'<th class="{"l" if i in left else ""}">{h}</th>' for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td class="{"l" if i in left else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>"
        for r in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def _ideas_section(con: sqlite3.Connection, esc) -> list[str]:
    """The monthly simple-mix ideas with the reasons, and how reliable they've been."""
    from trend_bot import ideas, paper

    r = ideas.load(con)
    if not r:
        return []
    row = lambda p: [f"<b>{esc(p['ticker'])}</b>", f"{p['close']:,.2f}" if p.get("close") else "–",
                     esc(p.get("sector") or "–"), esc(p["why"])]
    heads = ["Stock", "Price", "Industry", "Mostly because"]
    return ["<h2>💡 Top ideas this month</h2>",
            f'<p class="explain">The bot scored {r["stocks"]:,} large, actively traded US stocks on about 20 measures '
            "(is it cheap for what it earns? profitable? are fewer people betting against it? has its price been "
            f"rising?) and averaged them, on {esc(r['date'])}. Ideas to research, not advice.</p>",
            "<h3>💡 Best scores</h3>", _table(heads, [row(p) for p in r["top"]], left={0, 2, 3}),
            "<h3>⚠️ Worst scores (more likely to lag)</h3>", _table(heads, [row(p) for p in r["bottom"]], left={0, 2, 3}),
            *([f'<p class="note">{esc(ideas.live_line(con).replace("**", ""))}</p>'] if ideas.live_line(con) else []),
            *([f'<p class="note">{esc(paper.line(con).replace("**", ""))}</p>'] if paper.line(con) else []),
            f'<p class="note"><b>How reliable is this?</b> {esc(ideas.reliability_note(con))}</p>']


def _glance_section(con: sqlite3.Connection, esc) -> list[str]:
    """The dashboard at the top: the portfolio to follow, how it's really doing vs the S&P 500,
    red flags in its stocks, and the signals on probation."""
    from trend_bot import events, ideas

    parts = ["<h2>📋 The portfolio to follow</h2>",
             '<p class="explain">20 stocks chosen from the top ideas, equal amounts in each, checked once a month. '
             "Follow it only if you want to: in testing it did not reliably beat an S&P 500 index fund.</p>"]
    rec = ideas.live_record(con)
    if rec and rec["spy"] is not None and rec["days"] >= 1:
        ahead = rec["portfolio"] - rec["spy"]
        tiles = [(_pct(rec["portfolio"]), f"top-ideas portfolio since {esc(rec['start'])} (real, not a test)"),
                 (_pct(rec["spy"]), "S&P 500 over the same days"),
                 (f'<span class="{"up" if ahead >= 0 else "down"}">{ahead:+.1%}</span>',
                  "ahead of the S&P 500" if ahead >= 0 else "behind the S&P 500")]
        parts.append('<div class="tiles">' + "".join(
            f'<div class="tile"><div class="n">{n}</div><div class="l">{l}</div></div>' for n, l in tiles) + "</div>")
    elif rec:
        parts.append(f'<p class="sub">The live record starts {esc(rec["start"])}; it is compared with the '
                     "S&P 500 from then on.</p>")

    curve = ideas.growth_curve(con)
    if curve is not None:
        from trend_bot import charts

        parts += [f"<h3>What $10,000 would have become in testing ({curve.index[0]:%Y}–{curve.index[-1]:%Y})</h3>",
                  charts.growth_svg(curve),
                  f'<p class="explain">{esc(ideas.backtest_note(con))}</p>',
                  *([f'<p class="explain"><b>The bad times:</b> {esc(" ".join(ideas.bad_times(con)))}</p>']
                    if ideas.bad_times(con) else []),
                  '<p class="note">A test on past data, after trading costs, always invested. The line is drawn on '
                  "a scale where equal % moves look equal. The real record since the portfolio started is the "
                  "numbers above.</p>"]
    held = pd.read_sql_query("SELECT * FROM ideas_holdings ORDER BY rank", con)
    if held.empty:
        parts.append('<p class="empty">The top-ideas portfolio starts at its first monthly update.</p>')
    else:
        closes = {t: df["Close"] for t, df in load_many(
            con, list(held["ticker"]), start=(dt.date.today() - dt.timedelta(days=200)).isoformat()).items()}
        rows = []
        for h in held.itertuples(index=False):
            c = closes.get(h.ticker, pd.Series(dtype=float))
            now = float(c.iloc[-1]) if len(c) else float("nan")
            rows.append([f"<b>{esc(h.ticker)}</b>", esc(str(h.since)), sparkline(c),
                         f"{h.entry_price:,.2f}" if h.entry_price else "–", f"{now:,.2f}",
                         _pct(now / h.entry_price - 1 if h.entry_price else None),
                         esc(h.sector or "–"), esc(h.why or "")])
        parts += ["<h3>What it holds now</h3>",
                  _table(["Stock", "Held since", "Last 6 months", "Bought at", "Price now", "Gain or loss", "Industry",
                          "Mostly because"], rows, left={0, 1, 6, 7})]
        cik_of = dict(con.execute(f"SELECT cik, ticker FROM tickers WHERE cik IS NOT NULL AND ticker IN "
                                  f"({','.join('?' * len(held))})", list(held["ticker"])).fetchall())
        flags = events.recent_flags(con, list(cik_of), days=90)
        parts.append("<h3>🚩 Warning signs in these stocks (last 90 days)</h3>")
        parts.append('<p class="explain">Problems a company reported to the SEC (the US market regulator), such as '
                     "wrong past results, an auditor leaving or a late report. These have tended to come before "
                     "weaker returns.</p>")
        parts.append(_table(["Stock", "Filed", "What happened"],
                            [[f"<b>{esc(cik_of[r.cik])}</b>", esc(r.filed), esc(events.describe(r.items))]
                             for r in flags.itertuples(index=False)], left={0, 1, 2})
                     if len(flags) else '<p class="empty">None.</p>')

    return parts


def _probation_section(con: sqlite3.Connection, esc) -> list[str]:
    """New measures being tested before the bot may use them."""
    from trend_bot import alerts, candidates

    board = candidates.load(con)
    if not board:
        return []
    icon = {"PASSES": "✅", "promising": "🔶", "not proven": "❌"}
    order = {"PASSES": 0, "promising": 1, "not proven": 2}
    rows = [[f"{icon.get(r.get('verdict'), '⏳')} <b>{esc(alerts.PLAIN_NAMES.get(n, n))}</b>",
             esc(alerts.VERDICT_WORDS.get(r.get("verdict"), r.get("verdict") or "")),
             f"{r['ic']:+.3f}" if r.get("ic") is not None else "–",
             f"{r['ic_t']:+.1f}" if r.get("ic_t") is not None else "–", esc(r.get("from") or "–")]
            for n, r in sorted(board["rows"].items(), key=lambda kv: (order.get(kv[1].get("verdict"), 3), kv[0]))]
    return [f"<h2>🧪 Measures on trial (checked {esc(board['date'])})</h2>",
            '<p class="explain">New measures the bot tests before it may use them. Nothing joins the top ideas '
            "without your OK.</p>",
            _table(["Measure", "Verdict", "Ranking quality (IC)", "Sure it's not luck (t, 3+ needed)", "Data since"],
                   rows, left={0, 1})]


def _ml_section(con: sqlite3.Connection, esc) -> list[str]:
    """The all-signal model's ideas, only if it passed its walk-forward test."""
    from trend_bot import ml

    verdict, ranking = ml.load(con)
    if not verdict or not verdict.get("passed"):
        return []
    row = lambda p: [f"<b>{esc(p['ticker'])}</b>", f"{p['close']:,.2f}" if p.get("close") else "–"]
    return ["<h2>🤖 All-signal model</h2>",
            f'<p class="sub">Passed its honesty test: on years it never saw, it ranked stocks better than a simple '
            f"mix of the same signals (IC {verdict['ic']:+.3f} vs {verdict['mix_ic']:+.3f}). "
            f"Ranking from {esc(ranking['date'])}, refreshed monthly.</p>",
            "<h3>⚠️ Most likely to lag</h3>", _table(["Ticker", "Close"], [row(p) for p in ranking["bottom"]]),
            "<h3>💡 Top ideas</h3>", _table(["Ticker", "Close"], [row(p) for p in ranking["top"]])]


def _outlook(con: sqlite3.Connection, esc) -> list[str]:
    """Warnings first, then ideas, with how reliable they've been."""
    try:
        from trend_bot import forecast

        models, trained_on = forecast.load_models(con, log=lambda *_: None)
        preds, _, _ = forecast.predict_today(con, models)
        note = forecast.reliability_note(forecast.load_scorecard(con))
    except Exception:  # e.g. not enough history yet
        return ["<h2>⚠️ Most likely to lag (next 30 days)</h2>", '<p class="empty">Not available yet.</p>']
    if preds.empty:
        return ["<h2>⚠️ Most likely to lag (next 30 days)</h2>", '<p class="empty">Not available yet.</p>']
    picks = list(preds.index[:10]) + list(preds.index[-10:])
    closes = {t: df["Close"] for t, df in load_many(
        con, picks, start=(dt.date.today() - dt.timedelta(days=200)).isoformat()).items()}

    def rows(df: pd.DataFrame) -> list[list[str]]:
        return [[f"<b>{esc(t)}</b>", sparkline(closes.get(t, pd.Series(dtype=float))), f"{r['close']:,.2f}",
                 f"{r['p_beat']:.0%}", esc(r["why"])] for t, r in df.iterrows()]

    heads = ["Ticker", "6 months", "Close", "Chance to beat S&P 500", "Mostly because"]
    return ["<h2>⚠️ Most likely to lag (next 30 days)</h2>",
            '<p class="sub">Stocks to be careful with: in past tests this group did worse than the average stock.</p>',
            _table(heads, rows(preds.tail(10).iloc[::-1]), left={0, 4}),
            "<h2>💡 Ideas to research</h2>",
            '<p class="sub">Highest odds today, but in past tests the top ideas did not reliably beat the market. '
            "A starting point for your own research, not a buy list.</p>",
            _table(heads, rows(preds.head(10)), left={0, 4}),
            f'<p class="note"><b>How reliable is this?</b> {esc(note)} Learned from {esc(trained_on)}.</p>']


def _history_section(con: sqlite3.Connection, esc) -> list[str]:
    """The logbook: the last runs, what they did, and the code and data they used."""
    from trend_bot import runs

    rows = runs.history(con, limit=15)
    if not rows:
        return []
    def did(r):
        n = r["notes"]
        if "stocks_ranked" in n:
            return f"{n['stocks_ranked']:,} stocks ranked"
        if "prices" in n and isinstance(n["prices"], dict):
            p = n["prices"]
            return f"prices: {p.get('updated', 0):,} updated, {p.get('failed', 0):,} missing"
        return ""
    body = [[esc(r["started"].replace("T", " ")[:16]), esc(r["command"]),
             "✅" if r["ok"] else f'❌ {esc((r["error"] or "failed")[:60])}', esc(did(r)),
             f"{r['seconds'] / 60:.0f} min" if r["seconds"] and r["seconds"] >= 60 else f"{r['seconds'] or 0:.0f} s",
             esc(r["code"] or ""), esc(r["data"].get("id") or "")] for r in rows]
    return ["<h2>📒 Logbook: the last runs</h2>",
            '<p class="explain">Every run leaves a line. The code and data columns are fingerprints: two results '
            "with the same ones came from the same code and the same data.</p>",
            _table(["Started (UTC)", "What", "Result", "Did", "Took", "Code", "Data"], body, left={0, 1, 2, 3})]


def _start_here(con: sqlite3.Connection, esc) -> list[str]:
    """The box at the top: what the bot is, what it found, and what to do this month, in plain words."""
    from trend_bot import email_report, health

    try:
        problems = health.check(con)
    except Exception:  # noqa: BLE001 - the box must never break the report
        problems = []
    short = email_report.in_short(email_report.digest(con, problems)[1:], problems)
    return ['<div class="start"><h2>👋 Start here</h2>',
            "<p><b>What is this?</b> A research assistant. Every month it scores about 1,000 large US stocks on "
            "about 20 measures and suggests a 20-stock portfolio. It also warns you about problems in your "
            "stocks.</p>",
            "<p><b>What has it found?</b> In 20 years of testing, its best-scored stocks beat the average stock, "
            "but they did not reliably beat simply owning an <b>S&P 500 index fund</b> (one fund holding the 500 "
            "biggest US companies), especially since 2018. For real money, an index fund is the honest default.</p>",
            *[f'<p class="todo">{esc(x)}</p>' for x in short[:1]],
            *[f"<p>{esc(x)}</p>" for x in short[1:]],
            "</div>"]


def build_report(con: sqlite3.Connection, strategy: Strategy, benchmark: str = "SPY", limit: int = 25,
                 new: set[tuple[str, str]] | None = None, **screen_args) -> str:
    new = new or set()
    flips, clusters, states = screen(con, strategy, with_states=True, **screen_args)
    shown = set(flips.index[:limit]) | set(clusters.index[:limit])
    closes = {t: df["Close"] for t, df in load_many(
        con, sorted(shown), start=(dt.date.today() - dt.timedelta(days=200)).isoformat()).items()}
    esc = html.escape
    date = str(states["date"].max()) if len(states) else "–"

    up_share = (states["trend"] == "UP").mean() if len(states) else float("nan")
    buys = flips[flips["signal"] == "BUY"] if len(flips) else flips
    sells = flips[flips["signal"] == "SELL"] if len(flips) else flips
    strong = clusters[clusters["strong"]] if len(clusters) else clusters
    tiles = [
        (f"{len(states):,}", "stocks screened"),
        (f"{up_share:.0%}" if pd.notna(up_share) else "–", "with a rising price trend"),
        (f"{len(buys)} / {len(sells)}", "new uptrends / downtrends"),
        (f"{len(clusters)}", "insider buying clusters"),
        (f"{len(strong)}", "🔔 strong insider picks"),
    ]
    try:
        from trend_bot.model import weather_status, weather_text

        ws = weather_status(con, benchmark)
        banner = (f'<div class="weather {"sun" if ws["invest"] else "rain"}"><div class="wl">Market weather (market mood)</div>'
                  f'<div class="wt">{esc(weather_text(ws))}</div></div>')
    except Exception:  # no benchmark prices yet
        banner = ""
    parts = [f"<h1>Your Trend Bot report · {esc(date)}</h1>", *_start_here(con, esc), banner]
    parts += _glance_section(con, esc)
    parts += _ideas_section(con, esc)
    parts.append('<details class="curious"><summary>🔍 For the curious: more research and market screens '
                 '<span class="hint">(tap to open)</span></summary>')
    parts.append('<p class="explain">Everything below is extra detail. None of it changes what the portfolio does.</p>')
    parts.append('<div class="tiles">' + "".join(f'<div class="tile"><div class="n">{n}</div><div class="l">{l}</div></div>'
                                                 for n, l in tiles) + "</div>")
    parts.append(f'<p class="note">Screened: {esc(strategy.label)} · stocks over $5 trading $1M+ a day · funds left out</p>')
    parts += _probation_section(con, esc)
    parts += _outlook(con, esc)
    parts += _ml_section(con, esc)
    parts.append("<h2>🔔 Strong insider buying in an uptrend</h2>")
    parts.append('<p class="explain">Several people who run a company (executives, board members) bought its shares '
                 "with their own money while its price was climbing.</p>")
    if len(strong):
        cards = []
        for t, r in strong.head(limit).iterrows():
            badge = '<span class="badge">NEW</span>' if (t, "strong_insider") in new else ""
            cards.append(
                f'<div class="card"><div class="t">{esc(t)}{badge}</div>'
                f'<div class="d">{esc(str(r["name"]))}</div>'
                f'<div style="margin:8px 0">{sparkline(closes.get(t, pd.Series(dtype=float)), 260, 44)}</div>'
                f'<div>{int(r["buyers"])} insiders bought {_money(r["buy_value"])} · price {r["close"]:,.2f} · '
                f'20d {_pct(r["chg_20d"])}</div></div>')
        parts.append('<div class="cards">' + "".join(cards) + "</div>")
    else:
        parts.append('<p class="empty">None right now. This rule is rare: it needs 3+ insiders spending '
                     '$250k+ in a stock that is already trending up.</p>')

    def flip_rows(df: pd.DataFrame) -> list[list[str]]:
        return [[f"<b>{esc(t)}</b>", esc(str(r["name"])), sparkline(closes.get(t, pd.Series(dtype=float))),
                 f"{r['close']:,.2f}", _pct(r["chg_20d"]), f"{r['rsi']:.0f}" if r["rsi"] is not None else "–",
                 f"{int(r['buyers'])} · {_money(r['buy_value'])}" if r["buyers"] else ""]
                for t, r in df.head(limit).iterrows()]

    heads = ["Stock", "Company", "Last 6 months", "Price", "Last 20 days", "Speed (RSI)", "Insiders buying"]
    parts += [f"<h2>New uptrends ({len(buys)})</h2>",
              '<p class="explain">Prices that just started climbing: the 50-day average rose above the 200-day average. '
              "RSI above 70 means the price ran up fast, below 30 that it fell fast.</p>", _table(heads, flip_rows(buys)),
              f"<h2>New downtrends ({len(sells)})</h2>", _table(heads, flip_rows(sells))]

    cl_rows = [[f"<b>{esc(t)}</b>", esc(str(r["name"])), sparkline(closes.get(t, pd.Series(dtype=float))),
                f"{int(r['buyers'])}", _money(r["buy_value"]),
                f'<span class="{"up" if r["trend"] == "UP" else "down"}">{r["trend"]}</span>',
                "🔔" if r["strong"] else ""]
               for t, r in clusters.head(limit).iterrows()]
    parts += [f"<h2>Insider buying clusters, last 30 days ({len(clusters)})</h2>",
              _table(["Stock", "Company", "Last 6 months", "Insiders", "Bought", "Trend", "Strong"], cl_rows)]

    held = pd.read_sql_query("SELECT * FROM model_holdings ORDER BY rank", con)
    weather = con.execute("SELECT value FROM meta WHERE key = 'model_weather'").fetchone()
    parts.append("<h2>📈 Trend Score model portfolio (an older model, kept for comparison)</h2>")
    if weather:
        parts.append('<p class="sub">Market weather: ' + (
            '<b class="up">☀️ Invest</b>: the S&P 500 is above its 200-day average' if weather[0] == "invest"
            else '<b class="down">🌧️ Caution</b>: the S&P 500 is below its 200-day average, so the model holds cash')
            + "</p>")
    if held.empty:
        parts.append('<p class="empty">Not started yet. Run <code>python -m trend_bot model</code> '
                     "(the nightly alert does this).</p>")
    else:
        mcloses = {t: df["Close"] for t, df in load_many(
            con, list(held["ticker"]), start=(dt.date.today() - dt.timedelta(days=200)).isoformat()).items()}
        mrows = []
        for h in held.itertuples(index=False):
            c = mcloses.get(h.ticker, pd.Series(dtype=float))
            now = float(c.iloc[-1]) if len(c) else float("nan")
            mrows.append([f"<b>{esc(h.ticker)}</b>", esc(str(h.since)), sparkline(c), f"{h.entry_price:,.2f}",
                          f"{now:,.2f}", _pct(now / h.entry_price - 1 if h.entry_price else None),
                          f"{h.score:.0f}"])
        parts.append(_table(["Stock", "Held since", "Last 6 months", "Bought at", "Price now", "Gain or loss", "Score"],
                            mrows, left={0, 1}))
        parts.append('<p class="note">Equal amounts in each stock. Checked once a month: stocks that fall out of the '
                     "top 40 are sold and the best new ones bought. Theoretical, not advice.</p>")

    perf = track.performance(con, benchmark=benchmark)
    parts.append(f"<h2>Track record of every pick (vs {esc(benchmark)})</h2>")
    parts.append('<p class="explain">How each kind of pick really did after the bot made it, compared with the '
                 "S&P 500 over the same days.</p>")
    if perf.empty:
        parts.append('<p class="empty">No picks recorded yet. They are saved every night from now on.</p>')
    else:
        s = track.summary(perf)
        rows = [[esc(sig), f"{int(r['picks'])}", f"{r['avg_days']:.0f}", _pct(r["avg_return"]),
                 _pct(r["median_return"]), f"{r['win_rate']:.0%}", _pct(r["avg_vs_bench"]),
                 f"{r['beat_bench_rate']:.0%}"] for sig, r in s.iterrows()]
        parts.append(_table(["Signal", "Picks", "Avg days", "Avg return", "Median", "Went up",
                             f"Avg vs {esc(benchmark)}", f"Beat {esc(benchmark)}"], rows, left={0}))
        parts.append(f'<p class="note">Since {perf["date"].min()}. For new downtrends and model sells, a negative return '
                     "means the sell signal was right. Picks need months before these numbers mean much.</p>")

    parts += _history_section(con, esc)
    parts.append("</details>")
    from trend_bot import runs

    parts.append(f'<p class="note">Made by: {esc(runs.current_stamp() or "trend_bot")}</p>')
    parts.append(f'<p class="note">Generated {dt.datetime.now():%Y-%m-%d %H:%M} by trend_bot. Past patterns, not '
                 "certainties. Not financial advice.</p>")
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>Market report {esc(date)}</title><style>{CSS}</style></head>"
            f"<body><main>{''.join(parts)}</main></body></html>")
