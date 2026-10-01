"""Discord alerts for trend changes.

The bot remembers each ticker's last trend in a small JSON state file, so
if the computer was off for a few days, the next run still reports any
flips that happened in between.

Set the webhook URL in a `.env` file next to watchlist.txt:

    DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

or as an environment variable with the same name.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests

from trend_bot.insiders import InsiderSummary, InsiderTrade
from trend_bot.news import Headline

GREEN = 0x0CA30C
RED = 0xD03B3B
GRAY = 0x8D8C86
GOLD = 0xEDA100
MAX_EMBEDS = 10  # Discord's limit per message


def load_webhook(env_file: str | Path = ".env") -> str | None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if url:
        return url.strip()
    path = Path(env_file)
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "DISCORD_WEBHOOK_URL":
                return value.strip().strip('"').strip("'") or None
    return None


def load_state(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(path: str | Path, state: dict) -> None:
    Path(path).write_text(json.dumps(state, indent=2, sort_keys=True))


def find_changes(rows: list[dict], state: dict) -> list[dict]:
    """Rows whose trend differs from the last saved one.

    A ticker seen for the first time only alerts if its trend flipped today.
    """
    changes = []
    for row in rows:
        prev = state.get(row["ticker"], {}).get("trend")
        if prev is None:
            if row["action"] in ("BUY", "SELL"):
                changes.append(row)
        elif prev != row["trend"]:
            changes.append(row)
    return changes


def updated_state(rows: list[dict], state: dict) -> dict:
    new = {t: dict(v) for t, v in state.items()}
    for row in rows:
        new.setdefault(row["ticker"], {}).update(trend=row["trend"], date=str(row["date"]), close=row["close"])
    return new


def new_cluster(ticker: str, cluster: list[InsiderTrade] | None, state: dict) -> bool:
    """True if there's an insider buying cluster we haven't alerted about yet."""
    if not cluster:
        return False
    return state.get(ticker, {}).get("insider_alerted") != cluster[0].accession


def mark_cluster(ticker: str, cluster: list[InsiderTrade], state: dict) -> None:
    state.setdefault(ticker, {})["insider_alerted"] = cluster[0].accession


def _headline_lines(items: list[Headline], limit: int = 1000) -> str:
    out, used = [], 0
    for h in items:
        title = h.title if len(h.title) <= 110 else h.title[:107] + "..."
        line = f"• [{title}]({h.url})" if h.url else f"• {title}"
        if h.publisher:
            line += f" — {h.publisher}"
        if used + len(line) + 1 > limit:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def change_embed(row: dict, strategy_label: str, earnings: str = "", news: list[Headline] | None = None,
                 insiders: InsiderSummary | None = None) -> dict:
    up = row["trend"] == "UP"
    fields = [
        {"name": "Close", "value": f"{row['close']:,.2f}", "inline": True},
        {"name": "20-day change", "value": row["20d_chg"], "inline": True},
        {"name": "RSI", "value": str(row["rsi"]) if row.get("rsi") is not None else "–", "inline": True},
    ]
    if earnings:
        fields.append({"name": "Next earnings", "value": earnings, "inline": True})
    if insiders is not None:
        fields.append({"name": "Insiders (90 days)", "value": insiders.short, "inline": True})
    if news:
        fields.append({"name": "Recent headlines", "value": _headline_lines(news), "inline": False})
    return {
        "title": f"{'🟢 BUY' if up else '🔴 SELL'}  {row['ticker']}",
        "description": f"Trend turned **{'up' if up else 'down'}** ({strategy_label}), as of {row['date']}.",
        "color": GREEN if up else RED,
        "fields": fields,
    }


def insider_embed(ticker: str, cluster: list[InsiderTrade], trend: str | None = None) -> dict:
    buyers = sorted({t.insider for t in cluster})
    total = sum(t.value for t in cluster)
    lines = []
    for t in cluster[:8]:
        price = f" @ {t.price:,.2f}" if t.price else ""
        lines.append(f"• {t.date:%b %d}  **{t.insider}** ({t.role}): {t.shares:,.0f} sh{price}")
    trend_note = f"\nPrice trend right now: **{trend}**." if trend else ""
    return {
        "title": f"🔔 Insider buying  {ticker}",
        "description": (f"{len(buyers)} insiders bought shares on the open market "
                        f"(about ${total:,.0f} in total).{trend_note}\n\n" + "\n".join(lines)),
        "color": GOLD,
    }


def summary_embed(rows: list[dict], strategy_label: str) -> dict:
    lines = [f"{'Ticker':<7}{'Close':>10}{'20d':>8}  Trend"]
    for r in rows:
        lines.append(f"{r['ticker']:<7}{r['close']:>10,.2f}{r['20d_chg']:>8}  {r['trend']}")
    return {
        "title": "Daily trend summary",
        "description": f"{strategy_label}\n```\n" + "\n".join(lines) + "\n```",
        "color": GRAY,
    }


def payloads(embeds: list[dict]) -> list[dict]:
    """Split embeds into as many Discord messages as needed."""
    return [
        {"username": "Trend Bot", "embeds": embeds[i : i + MAX_EMBEDS]}
        for i in range(0, len(embeds), MAX_EMBEDS)
    ]


def send(webhook: str, payload: dict, timeout: float = 15) -> None:
    resp = requests.post(webhook, json=payload, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"Discord returned {resp.status_code}: {resp.text[:200]}")


def send_file(webhook: str, path: str | Path, message: str = "", timeout: float = 60) -> None:
    """Post a file (e.g. the HTML report) to the channel as an attachment."""
    path = Path(path)
    with path.open("rb") as f:
        resp = requests.post(webhook, data={"payload_json": json.dumps({"username": "Trend Bot", "content": message})},
                             files={"files[0]": (path.name, f, "text/html" if path.suffix == ".html" else "text/plain")},
                             timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"Discord returned {resp.status_code}: {resp.text[:200]}")


def market_embed(flips, clusters, strategy_label: str, limit: int = 10,
                 new: set[tuple[str, str]] | None = None) -> dict:
    """Strong insider picks first, then new uptrends, then the biggest insider clusters."""
    new = new or set()
    lines = []
    strong = clusters[clusters["strong"]] if len(clusters) and "strong" in clusters else clusters.iloc[0:0]
    if len(strong):
        lines.append(f"**🔔 Strong insider buying in an uptrend** ({len(strong)})")
        for t, r in strong.head(limit).iterrows():
            tag = " **NEW**" if (t, "strong_insider") in new else ""
            lines.append(f"`{t:<6}` {int(r['buyers'])} insiders bought ${r['buy_value']:,.0f}, "
                         f"price {r['close']:,.2f}{tag}")
        lines.append("")
    buys = flips[flips["signal"] == "BUY"] if len(flips) else flips
    if len(buys):
        lines.append(f"**New uptrends today** ({len(buys)} stocks)")
        for t, r in buys.head(limit).iterrows():
            star = " 🔔" if r["cluster"] else ""
            lines.append(f"`{t:<6}` {r['close']:>9,.2f}  {r['chg_20d']:+.1%} 20d{star}")
    sells = flips[flips["signal"] == "SELL"] if len(flips) else flips
    if len(sells):
        lines.append(f"\n**New downtrends today:** {len(sells)} stocks")
    others = clusters[~clusters["strong"]] if len(clusters) and "strong" in clusters else clusters
    if len(others):
        lines.append(f"\n**Other insider buying clusters** ({len(others)} stocks, top by $)")
        for t, r in others.head(limit).iterrows():
            tag = " NEW" if (t, "insider_cluster") in new else ""
            lines.append(f"`{t:<6}` {int(r['buyers'])} insiders  ${r['buy_value']:,.0f}  trend {r['trend']}{tag}")
    if not lines:
        lines.append("No new trend flips or insider clusters today.")
    text = "\n".join(lines)
    return {
        "title": "🌎 Market screen",
        "description": (f"{strategy_label}\n\n" + text)[:4000],
        "color": GOLD if len(strong) else GRAY,
    }


def model_embed(res: dict) -> dict | None:
    """Monthly model update (buys/sells), or a note when the market weather changes. None otherwise."""
    invest = res["weather"] == "invest"
    weather = ("☀️ **Invest** - the S&P 500 is above its 200-day average" if invest
               else "🌧️ **Caution** - the S&P 500 is below its 200-day average; the model holds cash")
    if res["rebalanced"]:
        scores = res["scores"]
        buy = [f"`{t:<6}` score {scores.loc[t, 'score']:.0f}, price {scores.loc[t, 'close']:,.2f}"
               for t in res["buys"] if t in scores.index]
        lines = [weather, "", f"**🟢 Buy ({len(res['buys'])})**", *(buy or ["nothing new"]),
                 "", f"**🔴 Sell ({len(res['sells'])})**", ", ".join(f"`{t}`" for t in res["sells"]) or "nothing",
                 "", f"Holding {len(res['holdings'])} stocks, equal amounts. Next check: first run of next month."]
        return {"title": "📈 Trend Score model - monthly check", "description": "\n".join(lines)[:4000],
                "color": GREEN if invest else RED}
    if res["weather_changed"]:
        return {"title": "🌦️ Market weather changed", "description": weather, "color": GREEN if invest else RED}
    return None


def forecast_embed(preds, date: str, weather: dict, note: str, n: int = 10) -> dict:
    """Weekly outlook: market weather first, then warnings, then ideas, with how reliable they've been."""
    from trend_bot.model import weather_text

    line = lambda t, r: f"`{t:<6}` {r['p_beat']:.0%} chance to beat the S&P 500 · {r['why']}"
    lines = [weather_text(weather, markdown=True), "",
             "**⚠️ Most likely to lag (next 30 days)**", *[line(t, r) for t, r in preds.tail(n).iloc[::-1].iterrows()], "",
             "**💡 Ideas to research**", *[line(t, r) for t, r in preds.head(5).iterrows()], "",
             f"_How reliable is this? {note}_", f"As of {date}. Odds, not certainties."]
    return {"title": "🌦️ Weekly outlook", "description": "\n".join(lines)[:4000],
            "color": GREEN if weather["invest"] else RED}


def ml_embed(verdict: dict | None, ranking: dict | None) -> dict | None:
    """The all-signal model's ideas - only when it beat the simple mix on years it never saw."""
    if not verdict or not ranking or not verdict.get("passed"):
        return None
    line = lambda p: f"`{p['ticker']:<6}`" + (f" {p['close']:,.2f}" if p.get("close") else "")
    lines = [f"Passed its honesty test: on years it never saw it ranked stocks better than a simple mix of "
             f"the same signals (IC {verdict['ic']:+.3f} vs {verdict['mix_ic']:+.3f}).", "",
             "**⚠️ Most likely to lag**", *[line(p) for p in ranking["bottom"]], "",
             "**💡 Top ideas**", *[line(p) for p in ranking["top"]], "",
             f"_Ranking from {ranking['date']}, refreshed monthly. Its picks are tracked forward "
             "(see the report). Odds, not certainties._"]
    return {"title": "🤖 All-signal model", "description": "\n".join(lines)[:4000], "color": GOLD}


def ml_status_line(verdict: dict | None) -> str:
    """One line for the weekly outlook when the model's ideas are not shown."""
    if verdict and not verdict.get("passed"):
        return ("The all-signal model hasn't beaten a simple mix of the same signals on years it never saw, "
                "so its ideas aren't shown. Its picks are still tracked forward.")
    return ""


def ideas_embed(ranking: dict | None, weather: dict, note: str) -> dict | None:
    """Monthly top ideas from the simple mix (every signal ranked and averaged), with its track record."""
    if not ranking:
        return None
    from trend_bot.model import weather_text

    line = lambda p: (f"`{p['ticker']:<6}`" + (f" {p['close']:,.2f}" if p.get("close") else "") + f" · {p['why']}")
    lines = [weather_text(weather, markdown=True), "",
             "**⚠️ Most likely to lag**", *[line(p) for p in ranking["bottom"]], "",
             "**💡 Top ideas**", *[line(p) for p in ranking["top"]], "",
             f"_How reliable is this? {note}_",
             f"Ranked {ranking['stocks']:,} stocks on {ranking['date']}; refreshed monthly. Not financial advice."]
    return {"title": "💡 Top ideas (every signal, simple mix)", "description": "\n".join(lines)[:4000],
            "color": GREEN if weather.get("invest") else RED}
