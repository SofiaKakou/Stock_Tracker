"""The two demo accounts over time: their value next to the S&P 500, as a chart and a weekly summary.

Each night the account values are read from Alpaca (its daily history of each account) and saved
(meta demo_history), so the report and the email can draw them without asking Alpaca again. On
Fridays the nightly run also posts the week in one message, with the chart attached.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import sqlite3

import pandas as pd

from trend_bot.db import get_meta, set_meta

NAMES = {"monthly": "Monthly portfolio (demo)", "day": "Day trading (demo)", "small": "$100 account (demo)",
         "spy": "S&P 500"}
COLORS = {"monthly": "#2a78d6", "day": "#d97706", "small": "#16a34a", "spy": "#8d8c86"}


def history(client, start: str) -> pd.Series:
    """The account's value at each day's close since `start` (YYYY-MM-DD), from Alpaca."""
    r = client._call("GET", "/account/portfolio/history", params={"period": "1A", "timeframe": "1D"})
    s = pd.Series(r.get("equity") or [], index=pd.to_datetime(r.get("timestamp") or [], unit="s").date, dtype=float)
    s = s[(s > 0) & (s.index >= dt.date.fromisoformat(start))]
    return s[~s.index.duplicated(keep="last")]


def collect(con: sqlite3.Connection, monthly=None, day=None, start: str | None = None, small=None) -> dict:
    """Read the accounts' histories (whichever are set up) and the S&P 500, save them, return them."""
    start = start or json.loads(get_meta(con, "paper_status") or "{}").get("start_date") or \
        f"{dt.date.today() - dt.timedelta(days=30)}"
    out: dict[str, dict] = {}
    for name, client in (("monthly", monthly), ("day", day), ("small", small)):
        if client is None:
            continue
        try:
            s = history(client, start)
        except Exception as e:  # noqa: BLE001 - one account not answering mustn't stop the other
            print(f"[accounts] {name}: no history ({str(e)[:80]})")
            continue
        if len(s):
            out[name] = {"dates": [str(d) for d in s.index], "values": [float(v) for v in s]}
    spy = pd.read_sql_query("SELECT date, close FROM prices WHERE ticker = 'SPY' AND date >= ? ORDER BY date", con,
                            params=[start])
    if len(spy):
        out["spy"] = {"dates": spy["date"].tolist(), "values": spy["close"].astype(float).tolist()}
    set_meta(con, "demo_history", json.dumps(out))
    con.commit()
    return out


def load(con: sqlite3.Connection) -> dict:
    return json.loads(get_meta(con, "demo_history") or "{}")


def growth(hist: dict) -> pd.DataFrame:
    """Each line as % change since its own first day (so accounts and the S&P 500 compare directly)."""
    cols = {}
    for name, h in hist.items():
        s = pd.Series(h["values"], index=pd.to_datetime(h["dates"]))
        if len(s) >= 1 and s.iloc[0]:
            cols[name] = s / s.iloc[0] - 1
    return pd.DataFrame(cols).sort_index()


def chart_png(hist: dict) -> bytes | None:
    g = growth(hist)
    if g.empty or len(g) < 2:
        return None
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    fig, ax = plt.subplots(figsize=(6.4, 2.8), dpi=150)
    for name in ("spy", "small", "day", "monthly"):
        if name in g:
            s = g[name].dropna()
            ax.plot(s.index, s * 100, color=COLORS[name], lw=2.2 if name != "spy" else 1.8,
                    label=f"{NAMES[name]}: {s.iloc[-1]:+.1%}")
    ax.axhline(0, color="#bbbbbb", lw=0.8)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:+.0f}%"))
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    ax.tick_params(labelsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def chart_svg(hist: dict, width: int = 640, height: int = 220) -> str:
    """The same as a small inline SVG for the report (follows light/dark mode)."""
    from html import escape

    g = growth(hist)
    if g.empty or len(g) < 2:
        return ""
    left, right, top, bottom = 48, 12, 12, 22
    lo, hi = min(g.min().min(), 0.0), max(g.max().max(), 0.0)
    span = (hi - lo) or 0.01
    days = list(g.index)
    x = lambda d: left + (width - left - right) * days.index(d) / max(len(days) - 1, 1)
    y = lambda v: top + (height - top - bottom) * (1 - (v - lo) / span)
    parts = [f'<line x1="{left}" x2="{width - right}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="var(--line)"/>',
             f'<text x="{left - 6}" y="{y(hi) + 4:.1f}" text-anchor="end" font-size="11" fill="var(--ink-2)">{hi:+.1%}</text>',
             f'<text x="{left - 6}" y="{y(lo) + 4:.1f}" text-anchor="end" font-size="11" fill="var(--ink-2)">{lo:+.1%}</text>']
    legend = []
    for name in ("spy", "small", "day", "monthly"):
        if name not in g:
            continue
        s = g[name].dropna()
        pts = " ".join(f"{x(d):.1f},{y(v):.1f}" for d, v in s.items())
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{COLORS[name]}" stroke-width="2.2" '
                     'stroke-linejoin="round"/>')
        legend.append(f'<span style="color:{COLORS[name]}">━</span> {escape(NAMES[name])}: {s.iloc[-1]:+.1%}')
    parts.append(f'<text x="{left}" y="{height - 4}" font-size="11" fill="var(--ink-2)">{days[0]:%b %d}</text>'
                 f'<text x="{width - right}" y="{height - 4}" text-anchor="end" font-size="11" '
                 f'fill="var(--ink-2)">{days[-1]:%b %d}</text>')
    return (f'<div class="scroll"><svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px" '
            f'role="img" aria-label="Demo accounts vs the S&amp;P 500">{"".join(parts)}</svg></div>'
            f'<div class="note">{" &nbsp; ".join(legend)}</div>')


def weekly(hist: dict, day_state: dict, today: dt.date, live: str = "") -> dict:
    """The week in one message: both accounts and the S&P 500 this week and since the start, and the
    day-trading patterns' week (from its trade log)."""
    from trend_bot.alerts import GOLD, lead

    monday = today - dt.timedelta(days=today.weekday())
    lines = [lead("how the bot's practice accounts (fake money) did this week, next to the S&P 500. "
                  "One week is mostly luck; the 'since the start' numbers say more as they grow."), ""]
    for name in ("monthly", "day", "small", "spy"):
        h = hist.get(name)
        if not h or len(h["values"]) < 2:
            continue
        s = pd.Series(h["values"], index=pd.to_datetime(h["dates"]).date)
        before = s[s.index < monday]
        week = s.iloc[-1] / (before.iloc[-1] if len(before) else s.iloc[0]) - 1
        lines.append(f"**{NAMES[name]}**: this week **{week:+.2%}**, since {s.index[0]:%b %d} "
                     f"**{s.iloc[-1] / s.iloc[0] - 1:+.2%}**")
    days = {d: r for d, r in (day_state.get("days") or {}).items() if dt.date.fromisoformat(d) >= monday}
    if days:
        per: dict[str, list] = {}
        for r in days.values():
            for p, x in r.items():
                per.setdefault(p, []).append(x)
        lines += ["", "Day-trading patterns this week:"]
        lines += [f"• {p}: ${sum(x['pnl'] for x in xs):+,.0f} over {sum(x['trades'] for x in xs)} trades"
                  for p, xs in sorted(per.items())]
    if live:
        lines += ["", live]
    return {"title": f"📅 The week: {monday:%b %d} – {today:%b %d}", "description": "\n".join(lines)[:4000],
            "color": GOLD, "image": {"url": "attachment://accounts.png"}}
