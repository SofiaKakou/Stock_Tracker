"""Portfolio safety: checked every night for the stocks in the portfolio to follow.

- Emergency exit (the demo account sells at the next open, without waiting for the monthly update):
  a bankruptcy filing (8-K item 1.03), a stock-exchange delisting notice (3.01), or past financial
  statements that can no longer be relied on (4.02).
- Warning only (you decide): other red flags (auditor change, defaults, impairments, late reports),
  a fall of more than 25% since it was bought, or more than 15% in one day. In the 10-year tests,
  selling on these didn't help on average, so they're a heads-up rather than an automatic sale.
- Drawdown alarm for the demo account: more than 10% below its peak, or 10% behind the S&P 500.

Each event is reported once. An exited stock stays out of the demo account for EXCLUDE_DAYS (the
monthly update then fills its place from the ranking as usual).
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from trend_bot.db import get_meta, set_meta

EXIT_ITEMS = {"1.03": "bankruptcy filing", "3.01": "stock exchange delisting notice",
              "4.02": "past financial statements can't be relied on"}
DROP_SINCE_BUY, DROP_ONE_DAY = 0.25, 0.15
DRAWDOWN, BEHIND_SPY = 0.10, 0.10
EXCLUDE_DAYS = 90


def check(con: sqlite3.Connection, today: dt.date | None = None) -> dict:
    """{"exit": [(ticker, why)], "warn": [(ticker, why)]}: new events only (each is reported once)."""
    from trend_bot import events

    today = today or dt.date.today()
    held = con.execute("SELECT h.ticker, h.since, h.entry_price, t.cik FROM ideas_holdings h "
                       "LEFT JOIN tickers t ON t.ticker = h.ticker").fetchall()
    seen = set(json.loads(get_meta(con, "safety_seen") or "[]"))
    out: dict[str, list] = {"exit": [], "warn": []}
    new_seen = set(seen)
    for ticker, since, entry, cik in held:
        if cik is not None:
            rows = con.execute("SELECT accession, filed, items FROM events WHERE cik = ? AND filed >= ?",
                               (cik, (today - dt.timedelta(days=10)).isoformat())).fetchall()
            for acc, filed, items in rows:
                key = f"{ticker}:{acc}"
                if key in seen:
                    continue
                new_seen.add(key)
                codes = set(items.split(","))
                exits = [EXIT_ITEMS[c] for c in sorted(codes & set(EXIT_ITEMS))]
                if exits:
                    out["exit"].append((ticker, f"{', '.join(exits)} (filed {filed})"))
                elif items == "late" or codes & events.RED_FLAG_ITEMS:
                    out["warn"].append((ticker, f"{events.describe(items) or 'late report'} (filed {filed})"))
        closes = [c for (c,) in con.execute("SELECT close FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 2",
                                            (ticker,))]
        if closes:
            if entry and closes[0] < entry * (1 - DROP_SINCE_BUY):
                key = f"{ticker}:drop-since-buy:{since}"
                if key not in seen:
                    new_seen.add(key)
                    out["warn"].append((ticker, f"down {closes[0] / entry - 1:.0%} since it was bought on {since}"))
            if len(closes) == 2 and closes[0] < closes[1] * (1 - DROP_ONE_DAY):
                key = f"{ticker}:drop-day:{today}"
                if key not in seen:
                    new_seen.add(key)
                    out["warn"].append((ticker, f"fell {closes[0] / closes[1] - 1:.0%} in one day"))
    set_meta(con, "safety_seen", json.dumps(sorted(new_seen)))
    if out["exit"]:
        excluded = json.loads(get_meta(con, "paper_excluded") or "{}")
        for ticker, why in out["exit"]:
            excluded[ticker] = {"why": why, "date": today.isoformat()}
        set_meta(con, "paper_excluded", json.dumps(excluded))
    con.commit()
    return out


def excluded(con: sqlite3.Connection, today: dt.date | None = None) -> dict[str, str]:
    """Stocks the demo account keeps out after an emergency exit: {ticker: why} (for EXCLUDE_DAYS)."""
    today = today or dt.date.today()
    raw = json.loads(get_meta(con, "paper_excluded") or "{}")
    return {t: r["why"] for t, r in raw.items()
            if (today - dt.date.fromisoformat(r["date"])).days <= EXCLUDE_DAYS}


def drawdown(con: sqlite3.Connection) -> list[str]:
    """New drawdown alarms for the demo account (each kind once until it recovers)."""
    from trend_bot import paper

    status = json.loads(get_meta(con, "paper_status") or "{}")
    if not status.get("equity"):
        return []
    peak = max(status.get("peak", 0.0), status["equity"])
    status["peak"] = peak
    alarms, flags = [], status.setdefault("alarms", {})
    down = status["equity"] / peak - 1
    if down <= -DRAWDOWN and not flags.get("drawdown"):
        alarms.append(f"the demo account is {down:.0%} below its peak (${peak:,.0f})")
        flags["drawdown"] = True
    elif down > -DRAWDOWN / 2:
        flags["drawdown"] = False
    r = paper.record(con)
    if r and r.get("spy") is not None:
        behind = r["account"] - r["spy"]
        if behind <= -BEHIND_SPY and not flags.get("behind"):
            alarms.append(f"the demo account is {abs(behind):.0%} behind the S&P 500 since {r['start']}")
            flags["behind"] = True
        elif behind > -BEHIND_SPY / 2:
            flags["behind"] = False
    set_meta(con, "paper_status", json.dumps(status))
    con.commit()
    return alarms


def embed(found: dict, alarms: list[str]) -> dict | None:
    from trend_bot.alerts import RED, lead

    if not found["exit"] and not found["warn"] and not alarms:
        return None
    lines = [lead("a safety check on the portfolio found something. Emergencies are sold in the demo account "
                  "right away; warnings are for you to look at (nothing is sold for them)."), ""]
    if found["exit"]:
        lines.append("**🚨 Emergency exit** (sold at the next open in the demo account, kept out for "
                     f"{EXCLUDE_DAYS} days):")
        lines += [f"• {t}: {why}" for t, why in found["exit"]]
    if found["warn"]:
        lines += ["", "**⚠️ Warnings** (no automatic sale; in testing, selling on these didn't help on average):"]
        lines += [f"• {t}: {why}" for t, why in found["warn"]]
    if alarms:
        lines += ["", "**📉 Drawdown alarm:**"] + [f"• {a}" for a in alarms]
    return {"title": "🛡️ Portfolio safety", "description": "\n".join(lines)[:4000], "color": RED}
