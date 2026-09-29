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
