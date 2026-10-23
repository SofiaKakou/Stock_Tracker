"""Extra data collected every night, for the record and for later research (nothing here changes how
the bot trades; a new signal would first have to pass the usual 10-year tests, with your OK).

- news: headlines for the stocks the bot follows (both portfolios, the day-trading list, the
  watchlist), from Alpaca's free news feed (same keys as the demo account), with a simple tone
  score from the financial word lists the bot already uses for company reports.
- macro: the economy and market mood from FRED (the St. Louis Fed's free data; needs a free key,
  FRED_API_KEY): interest rates, the yield curve, inflation, jobs, credit spreads and the VIX.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import time

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
SERIES = {
    "DGS10": "10-year Treasury yield (%)",
    "DGS2": "2-year Treasury yield (%)",
    "T10Y2Y": "Yield curve: 10-year minus 2-year (%)",
    "FEDFUNDS": "Fed funds rate (%)",
    "CPIAUCSL": "Consumer prices (index)",
    "UNRATE": "Unemployment rate (%)",
    "BAMLH0A0HYM2": "High-yield bond spread (%)",
    "VIXCLS": "VIX, the market's fear gauge",
}


# --- News -------------------------------------------------------------------------------------------

def tone(text: str) -> float | None:
    """(positive - negative words) / words, with the financial word lists used for company reports."""
    from trend_bot.text_changes import tone as count

    ws = re.findall(r"[a-z]+", (text or "").lower())
    if not ws:
        return None
    c = count(ws)
    return (c.get("positive", 0) - c.get("negative", 0)) / len(ws)


def followed(con: sqlite3.Connection) -> list[str]:
    """Stocks worth collecting news for: both portfolios, the day-trading list, the watchlist."""
    from pathlib import Path

    syms = {t for (t,) in con.execute("SELECT ticker FROM ideas_holdings")}
    raw = con.execute("SELECT value FROM meta WHERE key = 'mix_ranking'").fetchone()
    if raw:
        syms |= {p["ticker"] for p in json.loads(raw[0]).get("top", [])}
    if Path("intraday.db").exists():
        ic = sqlite3.connect("intraday.db")
        row = ic.execute("SELECT value FROM meta WHERE key = 'universe'").fetchone() if \
            ic.execute("SELECT name FROM sqlite_master WHERE name = 'meta'").fetchone() else None
        ic.close()
        syms |= set(json.loads(row[0])) if row else set()
    if Path("watchlist.txt").exists():
        from trend_bot.data import read_watchlist

        syms |= set(read_watchlist("watchlist.txt"))
    return sorted(s.replace("-", ".") for s in syms)


def news(con: sqlite3.Connection, symbols: list[str], session, since: dt.date, log=print, pause: float = 0.35) -> int:
    """Headlines since `since` for `symbols` (Alpaca News), stored once each. Returns how many were new."""
    added = 0
    for i in range(0, len(symbols), 50):
        token = None
        while True:
            params = {"symbols": ",".join(symbols[i:i + 50]), "start": f"{since}T00:00:00Z", "limit": 50,
                      "sort": "asc"}
            if token:
                params["page_token"] = token
            resp = session.get(NEWS_URL, params=params, timeout=60)
            if resp.status_code == 429:
                time.sleep(30)
                continue
            resp.raise_for_status()
            body = resp.json()
            rows = [(n["id"], n["created_at"], ",".join(n.get("symbols") or []), n.get("headline"), n.get("summary"),
                     n.get("source"), n.get("url"), tone(f"{n.get('headline', '')} {n.get('summary', '')}"))
                    for n in body.get("news") or []]
            before = con.total_changes
            con.executemany("INSERT OR IGNORE INTO news VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            added += con.total_changes - before
            token = body.get("next_page_token")
            if not token:
                break
            time.sleep(pause)
    con.commit()
    log(f"[news] {added:,} new headline(s) for {len(symbols)} stocks since {since}")
    return added


def alpaca_session():
    import requests

    from trend_bot.paper import KEY_ENV, SECRET_ENV

    key, secret = os.environ.get(KEY_ENV), os.environ.get(SECRET_ENV)
    if not (key and secret):
        return None
    s = requests.Session()
    s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    return s


# --- The economy (FRED) -------------------------------------------------------------------------------

def macro(con: sqlite3.Connection, key: str, session=None, first: str = "2000-01-01", log=print) -> int:
    """Each FRED series since its last stored day (from `first` the first time). Returns rows added."""
    import requests

    session = session or requests.Session()
    added = 0
    for sid in SERIES:
        last = con.execute("SELECT MAX(date) FROM macro WHERE series = ?", (sid,)).fetchone()[0]
        start = (dt.date.fromisoformat(last) + dt.timedelta(days=1)).isoformat() if last else first
        resp = session.get(FRED_URL, params={"series_id": sid, "api_key": key, "file_type": "json",
                                             "observation_start": start}, timeout=60)
        resp.raise_for_status()
        rows = [(sid, o["date"], float(o["value"])) for o in resp.json().get("observations", [])
                if o.get("value") not in (None, ".", "")]
        con.executemany("INSERT OR REPLACE INTO macro VALUES (?, ?, ?)", rows)
        added += len(rows)
    con.commit()
    log(f"[macro] {added:,} new value(s) across {len(SERIES)} FRED series")
    return added
