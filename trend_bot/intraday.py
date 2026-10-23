"""Day-trading research: do any well-known intraday patterns make money after costs?

Tested on years of half-hour price bars from Alpaca's free market data, with the same honesty rules
as the monthly research: costs on every trade, both halves of history judged separately, and a luck
check (the same trades in random directions). Only a pattern that passes everything would be worth
a slot in the demo account, and that's decided with you, never automatically.

The patterns (each from published research or long trader practice):
- intraday_momentum (Gao, Han, Li & Zhou 2018): the market's first half hour (from the previous
  close to 10:00 New York time) predicts its last half hour. Trade SPY/QQQ/IWM from 15:30 to the
  close in the direction of the first half hour.
- opening_range: a stock that breaks above its first half hour's high (below its low) keeps going:
  buy (short) at the next half hour's open, hold to the close.
- gap_fade: a stock that opens more than 2% away from yesterday's close drifts back: trade
  against the gap from the open to the close.

Data: its own small database (intraday.db; ~100 MB for 50 symbols since 2016), separate from
market.db, refreshed from Alpaca each time the study runs (only what's new).
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import time
from pathlib import Path

import numpy as np
import pandas as pd

DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
FIRST_DAY = "2016-01-01"
TIMEFRAME = "30Min"
ETFS = ["SPY", "QQQ", "IWM"]
N_STOCKS = 47                 # plus the 3 funds: 50 symbols
COST = {"etf": 0.0002, "stock": 0.0005}   # round trip (spread and fees), as a share of the trade
GAP = 0.02
SPLIT = "2021-01-01"          # the two halves of history, judged separately
NY = "America/New_York"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL, t TEXT NOT NULL,            -- t: bar start, UTC ISO
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, t)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: str | Path = "intraday.db") -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


class Data:
    """Alpaca's historical bars (same keys as the demo account)."""

    def __init__(self, key: str, secret: str, session=None):
        import requests

        self.http = session or requests.Session()
        self.http.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        self.feed = "sip"   # the whole market; the free plan may only allow IEX (one exchange)

    @classmethod
    def from_env(cls, key_env: str | None = None, secret_env: str | None = None) -> "Data | None":
        from trend_bot.paper import KEY_ENV, SECRET_ENV

        key, secret = os.environ.get(key_env or KEY_ENV), os.environ.get(secret_env or SECRET_ENV)
        return cls(key, secret) if key and secret else None

    def bars(self, symbols: list[str], start: str, end: str, pause: float = 0.35,
             timeframe: str = TIMEFRAME) -> pd.DataFrame:
        """[symbol, t, open, high, low, close, volume], following every page."""
        rows, token = [], None
        while True:
            params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start, "end": end,
                      "adjustment": "all", "feed": self.feed, "limit": 10_000}
            if token:
                params["page_token"] = token
            resp = self.http.get(DATA_URL, params=params, timeout=60)
            if resp.status_code == 403 and self.feed == "sip":   # not in this plan: fall back to IEX
                self.feed = "iex"
                continue
            if resp.status_code == 429:                            # rate limit: wait and retry
                time.sleep(30)
                continue
            resp.raise_for_status()
            body = resp.json()
            for sym, bars in (body.get("bars") or {}).items():
                rows += [(sym, b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in bars]
            token = body.get("next_page_token")
            if not token:
                break
            time.sleep(pause)
        return pd.DataFrame(rows, columns=["symbol", "t", "open", "high", "low", "close", "volume"])


def universe(market: sqlite3.Connection, n: int = N_STOCKS) -> list[str]:
    """The 3 index funds plus the n most traded stocks (by average daily dollar volume)."""
    from trend_bot.company_info import fund_tickers

    funds = fund_tickers(market)
    rows = market.execute("SELECT ticker FROM tickers WHERE dollar_vol IS NOT NULL AND last_close >= 5 "
                          "ORDER BY dollar_vol DESC LIMIT ?", (n * 3,)).fetchall()
    stocks = [t for (t,) in rows if t not in funds and t not in ETFS and "-" not in t][:n]
    return ETFS + stocks


def update(con: sqlite3.Connection, data: Data, symbols: list[str], today: dt.date | None = None,
           log=print) -> int:
    """Download what's new since the last stored bar, in yearly chunks. Returns rows added."""
    today = today or dt.date.today()
    last = dict(con.execute("SELECT symbol, MAX(t) FROM bars GROUP BY symbol"))
    added = 0
    groups: dict[str, list[str]] = {}
    for s in symbols:
        start = (pd.Timestamp(last[s]) + pd.Timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ") if s in last \
            else f"{FIRST_DAY}T00:00:00Z"
        groups.setdefault(start, []).append(s)
    end = f"{today:%Y-%m-%d}T00:00:00Z"
    for start, syms in groups.items():
        year = pd.Timestamp(start[:10]).year
        while year <= today.year:
            a = max(start, f"{year}-01-01T00:00:00Z")
            b = min(end, f"{year + 1}-01-01T00:00:00Z")
            if a < b:
                for i in range(0, len(syms), 10):
                    df = data.bars(syms[i:i + 10], a, b)
                    con.executemany("INSERT OR REPLACE INTO bars VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    df.itertuples(index=False, name=None))
                    added += len(df)
                con.commit()
                log(f"[intraday] {year}: {len(syms)} symbol(s) up to date ({added:,} bars so far, feed {data.feed})")
            year += 1
    con.execute("INSERT OR REPLACE INTO meta VALUES ('feed', ?)", (data.feed,))
    con.commit()
    return added


def sessions(con: sqlite3.Connection) -> pd.DataFrame:
    """Regular-hours half-hour bars with New York date and time: [symbol, date, slot, open..close].

    slot 0 = 9:30-10:00 ... slot 12 = 15:30-16:00."""
    df = pd.read_sql_query("SELECT * FROM bars", con)
    if df.empty:
        return df
    t = pd.to_datetime(df["t"], utc=True).dt.tz_convert(NY)
    minutes = t.dt.hour * 60 + t.dt.minute
    df = df.assign(date=t.dt.date.astype(str), slot=(minutes - (9 * 60 + 30)) // 30)
    return df[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)].drop(columns="t")


def _days(df: pd.DataFrame) -> pd.DataFrame:
    """Per symbol and day: first open, the first bar's high/low/close, the 15:30 open, and the close."""
    g = df.sort_values("slot").groupby(["symbol", "date"])
    first = df[df["slot"] == 0].set_index(["symbol", "date"])
    last = df[df["slot"] == 12].set_index(["symbol", "date"])
    out = pd.DataFrame({"open": first["open"], "high0": first["high"], "low0": first["low"], "close0": first["close"],
                        "open_last": last["open"], "close": last["close"]})
    out["bars"] = g.size()
    out = out.reset_index().sort_values(["symbol", "date"])
    out["prev_close"] = out.groupby("symbol")["close"].shift(1)
    return out[out["bars"] == 13]                       # full days only (no half days or gaps)


def intraday_momentum(df: pd.DataFrame) -> pd.DataFrame:
    """[date, symbol, ret] per trade: last half hour in the direction of the first (index funds)."""
    d = _days(df[df["symbol"].isin(ETFS)]).dropna(subset=["prev_close"])
    first = d["close0"] / d["prev_close"] - 1
    last = d["close"] / d["open_last"] - 1
    return pd.DataFrame({"date": d["date"], "symbol": d["symbol"], "ret": np.sign(first) * last,
                         "cost": COST["etf"]}).query("ret == ret")


def opening_range(df: pd.DataFrame) -> pd.DataFrame:
    """Break of the first half hour's range: in at the next bar's open, out at the close (stocks)."""
    stocks = df[~df["symbol"].isin(ETFS)].sort_values(["symbol", "date", "slot"])
    days = _days(stocks).set_index(["symbol", "date"])
    rows = []
    for (sym, date), g in stocks[stocks["slot"] >= 1].groupby(["symbol", "date"]):
        if (sym, date) not in days.index:
            continue
        day = days.loc[(sym, date)]
        closes, opens = g["close"].to_numpy(), g["open"].to_numpy()
        up = np.where(closes[:-1] > day["high0"])[0]       # a bar closes outside the range ...
        down = np.where(closes[:-1] < day["low0"])[0]
        first = min(up[0] if len(up) else 99, down[0] if len(down) else 99)
        if first == 99:
            continue
        side = 1 if len(up) and up[0] == first else -1
        entry = opens[first + 1]                           # ... in at the next bar's open
        rows.append((date, sym, side * (day["close"] / entry - 1)))
    return pd.DataFrame(rows, columns=["date", "symbol", "ret"]).assign(cost=COST["stock"])


def gap_fade(df: pd.DataFrame, gap: float = GAP) -> pd.DataFrame:
    """Against an opening gap of more than `gap`: from the open to the close (stocks)."""
    d = _days(df[~df["symbol"].isin(ETFS)]).dropna(subset=["prev_close"])
    g = d["open"] / d["prev_close"] - 1
    d = d[g.abs() > gap]
    return pd.DataFrame({"date": d["date"], "symbol": d["symbol"],
                         "ret": -np.sign(g[g.abs() > gap]) * (d["close"] / d["open"] - 1),
                         "cost": COST["stock"]})


STRATEGIES = {"intraday_momentum": intraday_momentum, "opening_range": opening_range, "gap_fade": gap_fade}
NAMES = {"intraday_momentum": "Market's last half hour follows its first",
         "opening_range": "Breakout from the first half hour's range",
         "gap_fade": "Fade big opening gaps"}


def evaluate(trades: pd.DataFrame, draws: int = 500, seed: int = 0) -> dict:
    """Daily results (equal amounts in each of a day's trades) after costs, both halves, luck check."""
    if trades.empty:
        return {"days": 0}
    net = trades["ret"] - trades["cost"]
    daily = net.groupby(trades["date"]).mean().sort_index()
    yearly = lambda r: float((1 + r).prod() ** (252 / len(r)) - 1) if len(r) else np.nan
    t = lambda r: float(r.mean() / r.std(ddof=1) * np.sqrt(len(r))) if len(r) > 2 and r.std() > 0 else 0.0
    early, late = daily[daily.index < SPLIT], daily[daily.index >= SPLIT]
    # Luck check: the same trades in random directions; how often do they do as well?
    rng = np.random.default_rng(seed)
    raw, dates = trades["ret"].to_numpy(), trades["date"].to_numpy()
    real = daily.mean()
    better = 0
    for _ in range(draws):
        flip = raw * rng.choice([-1.0, 1.0], len(raw)) - trades["cost"].to_numpy()
        better += pd.Series(flip).groupby(dates).mean().mean() >= real
    return {"trades": len(trades), "days": len(daily), "per_trade": float(net.mean()),
            "gross_per_trade": float(trades["ret"].mean()), "yearly": yearly(daily),
            "early": yearly(early), "late": yearly(late), "hit": float((net > 0).mean()), "t": t(daily),
            "luck": better / draws, "worst_day": float(daily.min()) if len(daily) else np.nan}


def verdict(r: dict) -> str:
    if not r.get("days"):
        return "no trades"
    ok = r["early"] > 0 and r["late"] > 0 and r["t"] > 2 and r["luck"] < 0.05
    return ("PASSES: positive after costs in both halves, unlikely to be luck" if ok else
            "doesn't pass: " + "; ".join(x for x in (
                "loses money after costs" if r["yearly"] <= 0 else "",
                "not positive in both halves" if not (r["early"] > 0 and r["late"] > 0) else "",
                "could easily be luck" if r["luck"] >= 0.05 or r["t"] <= 2 else "") if x))


def study(df: pd.DataFrame) -> dict[str, dict]:
    return {name: evaluate(f(df)) for name, f in STRATEGIES.items()}


def text(results: dict[str, dict], feed: str, first: str, last: str) -> str:
    lines = [f"Day-trading research: half-hour bars {first} to {last} (Alpaca, feed: {feed}). "
             f"Costs per round trip: funds {COST['etf']:.2%}, stocks {COST['stock']:.2%}. "
             f"Halves: before / from {SPLIT[:4]}.", ""]
    for name, r in results.items():
        lines.append(f"== {NAMES[name]} ({name}) ==")
        if not r.get("days"):
            lines += ["  no trades", ""]
            continue
        lines += [f"  {r['trades']:,} trades on {r['days']:,} days; per trade {r['per_trade']:+.3%} after costs "
                  f"({r['gross_per_trade']:+.3%} before); right {r['hit']:.0%} of the time",
                  f"  yearly {r['yearly']:+.1%} (before {SPLIT[:4]}: {r['early']:+.1%}, from {SPLIT[:4]}: {r['late']:+.1%}); "
                  f"worst day {r['worst_day']:+.2%}",
                  f"  luck check: random directions did as well {r['luck']:.0%} of the time (t = {r['t']:.1f})",
                  f"  → {verdict(r)}", ""]
    passed = [NAMES[n] for n, r in results.items() if verdict(r).startswith("PASSES")]
    lines.append("Passed: " + (", ".join(passed) + ". Worth discussing a small slot in the demo account."
                               if passed else "none. No day-trading pattern here beats its costs reliably; "
                               "the demo account stays on the monthly portfolio."))
    if feed == "iex":
        lines.append("(Prices from IEX only, one exchange: the free plan's feed. Fine for a first look; "
                     "bar highs and lows are less exact than the whole market's.)")
    return "\n".join(lines)
