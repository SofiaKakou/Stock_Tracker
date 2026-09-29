"""Company events and headlines from Yahoo Finance, used as context for signals.

This is *context*, not a signal: public news is usually priced in within
minutes. What's genuinely useful is knowing that a scheduled event (like
earnings) is coming up, because prices often gap on those days.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class Headline:
    title: str
    publisher: str
    url: str
    published: dt.datetime | None


def _ticker(symbol: str):
    import yfinance as yf

    return yf.Ticker(symbol)


def next_earnings(symbol: str, today: dt.date | None = None, ticker=None) -> dt.date | None:
    """Date of the next scheduled earnings report, or None if unknown (e.g. ETFs)."""
    today = today or dt.date.today()
    try:
        cal = (ticker or _ticker(symbol)).calendar
    except Exception:
        return None
    if cal is None:
        return None
    if isinstance(cal, dict):
        dates = cal.get("Earnings Date") or []
    else:  # older yfinance returned a DataFrame
        try:
            dates = list(cal.loc["Earnings Date"].dropna())
        except Exception:
            return None
    if not isinstance(dates, (list, tuple)):
        dates = [dates]
    upcoming = []
    for d in dates:
        if isinstance(d, dt.datetime):
            d = d.date()
        elif hasattr(d, "date") and callable(d.date):  # pandas Timestamp
            d = d.date()
        if isinstance(d, dt.date) and d >= today:
            upcoming.append(d)
    return min(upcoming) if upcoming else None


def _parse_headline(item: dict) -> Headline | None:
    content = item.get("content")
    if isinstance(content, dict):  # yfinance >= 0.2.48 format
        title = content.get("title")
        publisher = (content.get("provider") or {}).get("displayName", "")
        url = ((content.get("canonicalUrl") or {}).get("url")
               or (content.get("clickThroughUrl") or {}).get("url") or "")
        published = None
        if content.get("pubDate"):
            try:
                published = dt.datetime.fromisoformat(content["pubDate"].replace("Z", "+00:00"))
            except ValueError:
                pass
    else:  # older flat format
        title = item.get("title")
        publisher = item.get("publisher", "")
        url = item.get("link", "")
        ts = item.get("providerPublishTime")
        published = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc) if ts else None
    if not title:
        return None
    return Headline(title=title, publisher=publisher, url=url, published=published)


def headlines(symbol: str, limit: int = 5, ticker=None) -> list[Headline]:
    """Most recent headlines for a ticker (best effort; empty list on failure)."""
    try:
        items = (ticker or _ticker(symbol)).news or []
    except Exception:
        return []
    parsed = [h for h in (_parse_headline(i) for i in items if isinstance(i, dict)) if h]
    parsed.sort(key=lambda h: h.published or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
    return parsed[:limit]


def earnings_note(date: dt.date | None, today: dt.date | None = None) -> str:
    """Short text like 'in 3d (Oct 02)', or '' if unknown."""
    if date is None:
        return ""
    days = (date - (today or dt.date.today())).days
    when = "today" if days == 0 else f"in {days}d"
    return f"{when} ({date:%b %d})"
