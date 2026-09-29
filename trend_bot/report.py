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
        (f"{up_share:.0%}" if pd.notna(up_share) else "–", "in an uptrend (market breadth)"),
        (f"{len(buys)} / {len(sells)}", "new uptrends / downtrends"),
        (f"{len(clusters)}", "insider buying clusters"),
        (f"{len(strong)}", "🔔 strong insider picks"),
    ]
    parts = [
        f"<h1>Market report · {esc(date)}</h1>",
        f'<p class="sub">{esc(strategy.label)} · stocks over $5 trading $1M+ a day · funds left out</p>',
        '<div class="tiles">' + "".join(f'<div class="tile"><div class="n">{n}</div><div class="l">{l}</div></div>'
                                        for n, l in tiles) + "</div>",
        "<h2>🔔 Strong insider buying in an uptrend</h2>",
    ]
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

    heads = ["Ticker", "Company", "6 months", "Close", "20 days", "RSI", "Insiders buying"]
    parts += [f"<h2>New uptrends ({len(buys)})</h2>", _table(heads, flip_rows(buys)),
              f"<h2>New downtrends ({len(sells)})</h2>", _table(heads, flip_rows(sells))]

    cl_rows = [[f"<b>{esc(t)}</b>", esc(str(r["name"])), sparkline(closes.get(t, pd.Series(dtype=float))),
                f"{int(r['buyers'])}", _money(r["buy_value"]),
                f'<span class="{"up" if r["trend"] == "UP" else "down"}">{r["trend"]}</span>',
                "🔔" if r["strong"] else ""]
               for t, r in clusters.head(limit).iterrows()]
    parts += [f"<h2>Insider buying clusters, last 30 days ({len(clusters)})</h2>",
              _table(["Ticker", "Company", "6 months", "Insiders", "Bought", "Trend", "Strong"], cl_rows)]

    held = pd.read_sql_query("SELECT * FROM model_holdings ORDER BY rank", con)
    weather = con.execute("SELECT value FROM meta WHERE key = 'model_weather'").fetchone()
    parts.append("<h2>📈 Trend Score model portfolio</h2>")
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
        parts.append(_table(["Ticker", "Since", "6 months", "Bought at", "Now", "Return", "Score"], mrows, left={0, 1}))
        parts.append('<p class="note">Equal amounts in each stock. Checked once a month: stocks that fall out of the '
                     "top 40 are sold and the best new ones bought. Theoretical, not advice.</p>")

    perf = track.performance(con, benchmark=benchmark)
    parts.append(f"<h2>Track record (vs {esc(benchmark)})</h2>")
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

    parts.append(f'<p class="note">Generated {dt.datetime.now():%Y-%m-%d %H:%M} by trend_bot. '
                 "Not financial advice.</p>")
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>Market report {esc(date)}</title><style>{CSS}</style></head>"
            f"<body><main>{''.join(parts)}</main></body></html>")
