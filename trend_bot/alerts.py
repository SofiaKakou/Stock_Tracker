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
MAX_CHARS = 5900  # Discord allows 6,000 characters of embed text per message; a little margin


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


def lead(text: str) -> str:
    """The first line of every message: what it means for you, in plain words."""
    return f"**What this means:** {text}"


def change_embed(row: dict, strategy_label: str, earnings: str = "", news: list[Headline] | None = None,
                 insiders: InsiderSummary | None = None) -> dict:
    up = row["trend"] == "UP"
    rsi = row.get("rsi")
    fields = [
        {"name": "Price", "value": f"{row['close']:,.2f}", "inline": True},
        {"name": "Last 20 trading days", "value": row["20d_chg"], "inline": True},
        {"name": "Speed gauge (RSI)", "value": (f"{rsi} " + ("(ran up fast)" if rsi >= 70 else "(fell fast)"
                                                             if rsi <= 30 else "(normal)")) if rsi is not None else "–",
         "inline": True},
    ]
    if earnings:
        fields.append({"name": "Next earnings", "value": earnings, "inline": True})
    if insiders is not None:
        fields.append({"name": "Insiders (90 days)", "value": insiders.short, "inline": True})
    if news:
        fields.append({"name": "Recent headlines", "value": _headline_lines(news), "inline": False})
    what = ("its price has been climbing lately: the average price of the last 50 days rose above the average "
            "of the last 200 days." if up else
            "its price has been sliding lately: the average price of the last 50 days fell below the average "
            "of the last 200 days.")
    return {
        "title": f"{'🟢 Price trend turned up' if up else '🔴 Price trend turned down'}: {row['ticker']}",
        "description": (lead(f"{row['ticker']}, on your watchlist, {what}") +
                        "\nA prompt to take a look, not a reason to buy or sell on its own: in our tests this "
                        f"signal alone didn't beat the market. (As of {row['date']}.)"),
        "color": GREEN if up else RED,
        "fields": fields,
    }


def insider_embed(ticker: str, cluster: list[InsiderTrade], trend: str | None = None) -> dict:
    buyers = sorted({t.insider for t in cluster})
    total = sum(t.value for t in cluster)
    lines = []
    for t in cluster[:8]:
        price = f" at {t.price:,.2f}" if t.price else ""
        lines.append(f"• {t.date:%b %d}  **{t.insider}** ({t.role}): bought {t.shares:,.0f} shares{price}")
    trend_note = (f"\nThe price trend right now is **{'up' if trend == 'UP' else 'down' if trend == 'DOWN' else trend}**."
                  if trend else "")
    return {
        "title": f"🔔 Insiders are buying: {ticker}",
        "description": (lead(f"{len(buyers)} insiders (people who run {ticker}: executives or board members) "
                             f"bought its shares with their own money, about ${total:,.0f} in total. They know "
                             "the company best, so it can be a good sign; on its own it hasn't beaten the "
                             f"market in our tests.") + f"{trend_note}\n\n" + "\n".join(lines)),
        "color": GOLD,
    }


def summary_embed(rows: list[dict], strategy_label: str) -> dict:
    lines = [f"{'Stock':<7}{'Price':>10}{'20 days':>9}  Trend"]
    for r in rows:
        trend = {"UP": "up", "DOWN": "down"}.get(r["trend"], r["trend"])
        lines.append(f"{r['ticker']:<7}{r['close']:>10,.2f}{r['20d_chg']:>9}  {trend}")
    return {
        "title": "👀 Your watchlist today",
        "description": ("Price, change over the last 20 trading days (about a month) and the price trend "
                        "(up = the 50-day average is above the 200-day average).\n```\n" + "\n".join(lines) + "\n```"),
        "color": GRAY,
    }


def embed_size(embed: dict) -> int:
    """The characters Discord counts against its per-message limit (title, description, fields, footer, author)."""
    n = len(embed.get("title") or "") + len(embed.get("description") or "")
    n += sum(len(f.get("name") or "") + len(f.get("value") or "") for f in embed.get("fields") or [])
    n += len((embed.get("footer") or {}).get("text") or "") + len((embed.get("author") or {}).get("name") or "")
    return n


def _fit(embed: dict) -> dict:
    """Shorten a single embed that is too big on its own: drop fields from the end, then cut the description."""
    if embed_size(embed) <= MAX_CHARS:
        return embed
    embed = {**embed, "fields": list(embed.get("fields") or [])}
    while embed["fields"] and embed_size(embed) > MAX_CHARS:
        embed["fields"].pop()
    over = embed_size(embed) - MAX_CHARS
    if over > 0:
        desc = embed.get("description") or ""
        embed["description"] = desc[: max(0, len(desc) - over - 30)] + "\n… (shortened; see the report)"
    return embed


def payloads(embeds: list[dict]) -> list[dict]:
    """Split embeds into as many Discord messages as needed: at most 10 and 6,000 characters each."""
    out, batch, size = [], [], 0
    for e in map(_fit, embeds):
        n = embed_size(e)
        if batch and (len(batch) == MAX_EMBEDS or size + n > MAX_CHARS):
            out.append({"username": "Trend Bot", "embeds": batch})
            batch, size = [], 0
        batch.append(e)
        size += n
    if batch:
        out.append({"username": "Trend Bot", "embeds": batch})
    return out


def send(webhook: str, payload: dict, timeout: float = 15) -> None:
    resp = requests.post(webhook, json=payload, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"Discord returned {resp.status_code}: {resp.text[:200]}")


def send_with_image(webhook: str, embed: dict, png: bytes | None, name: str = "accounts.png", timeout: float = 60) -> None:
    """An embed with a PNG attached and shown inside it (the embed refers to attachment://<name>)."""
    if png is None:
        embed = {k: v for k, v in embed.items() if k != "image"}
        return send(webhook, {"username": "Trend Bot", "embeds": [embed]}, timeout)
    resp = requests.post(webhook, data={"payload_json": json.dumps({"username": "Trend Bot", "embeds": [embed]})},
                         files={"files[0]": (name, png, "image/png")}, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"Discord returned {resp.status_code}: {resp.text[:200]}")


def send_file(webhook: str, path: str | Path, message: str = "", timeout: float = 60) -> None:
    """Post a file (e.g. the HTML report) to the channel as an attachment."""
    path = Path(path)
    with path.open("rb") as f:
        resp = requests.post(webhook, data={"payload_json": json.dumps({"username": "Trend Bot", "content": message})},
                             files={"files[0]": (path.name, f, {".html": "text/html", ".md": "text/markdown"}.get(
                                 path.suffix, "text/plain"))},
                             timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"Discord returned {resp.status_code}: {resp.text[:200]}")


def market_embed(flips, clusters, strategy_label: str, limit: int = 10,
                 new: set[tuple[str, str]] | None = None) -> dict:
    """Strong insider picks first, then new uptrends, then the biggest insider clusters."""
    new = new or set()
    lines = [lead("stocks across the whole market whose price started climbing today, or where several "
                  "insiders bought shares. Ideas to look into, not tips."), ""]
    strong = clusters[clusters["strong"]] if len(clusters) and "strong" in clusters else clusters.iloc[0:0]
    if len(strong):
        lines.append(f"**🔔 Strong insider buying, price climbing** ({len(strong)})")
        for t, r in strong.head(limit).iterrows():
            tag = " **NEW**" if (t, "strong_insider") in new else ""
            lines.append(f"`{t:<6}` {int(r['buyers'])} insiders bought ${r['buy_value']:,.0f}, "
                         f"price {r['close']:,.2f}{tag}")
        lines.append("")
    buys = flips[flips["signal"] == "BUY"] if len(flips) else flips
    if len(buys):
        lines.append(f"**New uptrends today** ({len(buys)} stocks; change over 20 trading days)")
        for t, r in buys.head(limit).iterrows():
            star = " 🔔" if r["cluster"] else ""
            lines.append(f"`{t:<6}` {r['close']:>9,.2f}  {r['chg_20d']:+.1%}{star}")
    sells = flips[flips["signal"] == "SELL"] if len(flips) else flips
    if len(sells):
        lines.append(f"\n**New downtrends today:** {len(sells)} stocks")
    others = clusters[~clusters["strong"]] if len(clusters) and "strong" in clusters else clusters
    if len(others):
        lines.append(f"\n**Other insider buying** ({len(others)} stocks, biggest first)")
        for t, r in others.head(limit).iterrows():
            tag = " NEW" if (t, "insider_cluster") in new else ""
            trend = {"UP": "up", "DOWN": "down"}.get(r["trend"], r["trend"])
            lines.append(f"`{t:<6}` {int(r['buyers'])} insiders  ${r['buy_value']:,.0f}  trend {trend}{tag}")
    if len(lines) == 2:
        lines.append("Nothing new today.")
    return {
        "title": "🌎 Market screen",
        "description": "\n".join(lines)[:4000],
        "color": GOLD if len(strong) else GRAY,
    }


def weather_line(invest: bool) -> str:
    return ("☀️ **Market mood: steady.** The S&P 500 (the 500 biggest US companies) is above its average "
            "price of the last 200 trading days." if invest else
            "🌧️ **Market mood: shaky.** The S&P 500 (the 500 biggest US companies) is below its average price "
            "of the last 200 trading days, so expect a bumpier ride. The portfolio stays invested: in testing, "
            "moving to cash in these spells cost more than it saved.")


def model_embed(res: dict) -> dict | None:
    """Monthly model update (buys/sells), or a note when the market weather changes. None otherwise."""
    invest = res["weather"] == "invest"
    if res["rebalanced"]:
        scores = res["scores"]
        buy = [f"`{t:<6}` price {scores.loc[t, 'close']:,.2f}" for t in res["buys"] if t in scores.index]
        lines = [lead("an older, simpler model, kept to compare with the top-ideas portfolio. It picks 20 "
                      "stocks whose prices have been rising steadily, and holds cash when the market mood is "
                      "shaky. Its monthly changes:"), "",
                 f"**🟢 Buy ({len(res['buys'])})**", *(buy or ["nothing new"]),
                 "", f"**🔴 Sell ({len(res['sells'])})**", ", ".join(f"`{t}`" for t in res["sells"]) or "nothing",
                 "", f"Holding {len(res['holdings'])} stocks, equal amounts. Next check: early next month.",
                 "", weather_line(invest)]
        return {"title": "📈 Trend model: monthly check", "description": "\n".join(lines)[:4000],
                "color": GREEN if invest else RED}
    if res["weather_changed"]:
        return {"title": "🌦️ The market mood changed",
                "description": weather_line(invest) + ("" if invest else
                                                       "\nNothing to do: this is a heads-up only."),
                "color": GREEN if invest else RED}
    return None


def ideas_embed(ranking: dict | None, weather: dict, note: str, live: str = "") -> dict | None:
    """Monthly top ideas from the simple mix (every signal ranked and averaged), with its track record."""
    if not ranking:
        return None
    line = lambda p: (f"`{p['ticker']:<6}`" + (f" {p['close']:,.2f}" if p.get("close") else "")
                      + (f" · {p['sector']}" if p.get("sector") else "") + f" · {p['why']}")
    lines = [lead(f"the bot scored {ranking['stocks']:,} large, actively traded US stocks on about 20 "
                  "measures (is it cheap for what it earns? profitable? are fewer people betting against it? has "
                  "its price been rising?) and averaged them. These are the best and worst scores this month. "
                  "Use them as ideas to research, not as advice."), "",
             "**💡 Best scores**", *[line(p) for p in ranking["top"]], "",
             "**⚠️ Worst scores (more likely to lag)**", *[line(p) for p in ranking["bottom"]], "",
             *([live, ""] if live else []),
             f"_How reliable is this? {note}_", "",
             weather_line(bool(weather.get("invest", True))),
             f"Ranked on {ranking['date']}; refreshed monthly. Not financial advice."]
    return {"title": "💡 Top ideas this month", "description": "\n".join(lines)[:4000],
            "color": GREEN if weather.get("invest") else RED}


def ideas_portfolio_embed(port: dict, note: str, live: str = "") -> dict:
    """Monthly: what to buy, sell and keep in the top-ideas portfolio (the rule study ideas tests)."""
    buy = lambda p: (f"`{p['ticker']:<6}` {p['close']:,.2f}" + (f" · {p['sector']}" if p.get("sector") else "")
                     + f" · {p['why']}")
    sell = lambda p: (f"`{p['ticker']:<6}` " + (f"dropped to #{p['rank']} in the ranking" if p.get("rank")
                                               else "no longer ranked")
                      + (f", {p['return']:+.0%} since {p['since']}" if p.get("return") is not None else ""))
    nb, ns = len(port["buys"]), len(port["sells"])
    todo = ("start the portfolio: buy these " + str(nb) + " stocks in equal amounts." if port.get("first") else
            "no changes this month: keep everything." if not nb and not ns else
            f"sell {ns}, buy {nb}, keep the other {len(port['holds'])}. Equal amounts in each.")
    lines = [lead("the bot's 20-stock portfolio, built from the top ideas, if you choose to follow it. "
                  f"This month: {todo}"), "",
             f"**🟢 Buy ({nb})**", *([buy(p) for p in port["buys"]] or ["nothing new"]), "",
             f"**🔴 Sell ({ns})**", *([sell(p) for p in port["sells"]] or ["nothing"]), "",
             f"**Keep ({len(port['holds'])})**: " + (", ".join(f"`{p['ticker']}`" for p in port["holds"]) or "-"), "",
             *([live, ""] if live else []),
             "A stock is only sold once it falls out of the top 40, so trades stay rare.",
             f"_How reliable is this? {note}_", "",
             weather_line(port["invest"]).replace("Market mood: shaky.", "Heads-up, market mood: shaky."),
             f"As of {port['date']}. Not financial advice."]
    return {"title": "📋 The portfolio to follow: monthly update", "description": "\n".join(lines)[:4000],
            "color": GREEN if port["invest"] else RED}


def scorecard_embed(card) -> dict:
    """Monthly: how every kind of pick has really done since it was made, vs the S&P 500."""
    lines = [lead("every pick the bot has ever made, checked against what the S&P 500 did over the same "
                  "days. ✅ = working so far, ❌ = not. For \"should lag\" picks (warnings), lagging counts as "
                  "working."), ""]
    for r in card.itertuples(index=False):
        mark = "✅" if r.worked else "❌"
        aim = "should lag" if r.meant_to_lag else "should beat"
        lines.append(f"{mark} **{r.label}** ({aim}): {r.picks} picks, held about {r.avg_days:.0f} days, "
                     f"**{r.avg_vs_bench:+.1%}** vs the S&P 500 on average, ahead {r.beat_rate:.0%} of the time")
    lines += ["", "Only picks at least 20 days old count; early numbers swing a lot."]
    worked = int(card["worked"].sum())
    return {"title": f"📊 Pick scorecard: {worked} of {len(card)} kinds of picks working so far",
            "description": "\n".join(lines)[:4000], "color": GREEN if worked * 2 >= len(card) else RED}


def red_flags_embed(flags: list[tuple[str, str, str, str]]) -> dict:
    """flags: (ticker, where it's from, filing date, what happened)."""
    lines = [f"**{t}** (in your {where}), filed {filed}: {what}" for t, where, filed, what in flags[:15]]
    return {
        "title": "🚩 Warning signs in your stocks",
        "description": lead("a company you hold or watch reported a warning sign to the SEC (the US market "
                            "regulator): for example its past results were wrong, its auditor left, or its "
                            "report will be late. These have tended to come before weaker returns, so it's "
                            "worth a look.") + "\n\n" + "\n".join(lines) + "\n\n(Checked weekly.)",
        "color": 0xE67E22,
    }


PLAIN_NAMES = {
    "report_change": "report rewritten a lot", "tone_negative": "gloomy wording in reports",
    "tone_change": "wording got gloomier", "uncertainty": "uncertain wording", "litigious": "legal wording",
    "risk_change": "risks section rewritten", "risk_growth": "risks section grew", "red_flags": "8-K warning signs",
    "exec_changes": "executives leaving or joining", "late_filing": "late reports", "net_issuance": "issuing new shares",
}
VERDICT_WORDS = {"PASSES": "passed the test", "promising": "promising, needs more proof",
                 "not proven": "didn't prove itself", "not enough history yet": "too new to judge",
                 "no data yet": "no data yet"}


def candidates_embed(board: dict) -> dict:
    """The monthly scoreboard of signals on probation (see candidates.py)."""
    rows = board["rows"]
    passing = [n for n, r in rows.items() if r.get("verdict") == "PASSES"]
    icon = {"PASSES": "✅", "promising": "🔶", "not proven": "❌", "not enough history yet": "⏳",
            "no data yet": "⏳"}
    lines = [lead("new measures the bot is testing before it's allowed to use them. Nothing changes unless "
                  "you say yes."), ""]
    order = {"PASSES": 0, "promising": 1}
    for n, r in sorted(rows.items(), key=lambda kv: (order.get(kv[1].get("verdict"), 2), kv[0])):
        ic = r.get("ic")
        detail = f" _(IC {ic:+.3f}, t {r['ic_t']:+.1f})_" if ic is not None and r.get("ic_t") is not None else ""
        name = PLAIN_NAMES.get(n, n.replace("_", " "))
        lines.append(f"{icon.get(r.get('verdict'), '•')} **{name}**: {VERDICT_WORDS.get(r.get('verdict'), r.get('verdict'))}{detail}")
    ask = (f"\n\n**{', '.join(PLAIN_NAMES.get(n, n) for n in passing)}** passed. Tell Claude if you want "
           f"{'it' if len(passing) == 1 else 'them'} added to the top ideas." if passing else
           "\n\nNone has passed yet; they're checked again next month.")
    return {
        "title": "🧪 Measures on trial: monthly check",
        "description": "\n".join(lines) + ask + "\n\nPassing means it picked better stocks in both halves of "
                       "its history, by a wide enough margin that luck is unlikely, and made the top ideas "
                       "better too. (For experts: IC = how well it ranked stocks; t = how sure we are it isn't luck, "
                       "3+ needed.)",
        "color": 0x9B59B6 if passing else 0x95A5A6,
    }
