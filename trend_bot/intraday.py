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
CREATE TABLE IF NOT EXISTS earnings (symbol TEXT NOT NULL, date TEXT NOT NULL,   -- the trading day it moved the stock
    PRIMARY KEY (symbol, date)) WITHOUT ROWID;
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


MOVERS_URL = "https://data.alpaca.markets/v1beta1/screener/stocks/movers"


def movers(data: Data, top: int = 50) -> list[str]:
    """Today's biggest risers in the whole market (Alpaca's screener), biggest first."""
    resp = data.http.get(MOVERS_URL, params={"top": top}, timeout=30)
    resp.raise_for_status()
    return [m["symbol"] for m in resp.json().get("gainers") or []]


QUOTES_URL = "https://data.alpaca.markets/v2/stocks/quotes/latest"


def latest_quotes(data: "Data", symbols: list[str]) -> dict[str, dict]:
    """The best bid and ask right now: {symbol: {"bid", "ask"}} (whole-market feed, else IEX)."""
    if not symbols:
        return {}
    for feed in (("sip", "iex") if data.feed == "sip" else ("iex",)):
        resp = data.http.get(QUOTES_URL, params={"symbols": ",".join(symbols), "feed": feed}, timeout=30)
        if resp.status_code == 403:
            continue
        resp.raise_for_status()
        return {s: {"bid": q.get("bp"), "ask": q.get("ap")} for s, q in (resp.json().get("quotes") or {}).items()}
    return {}


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


def _grid(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """One row per (symbol, date), one column per half-hour slot 0..12 (full days only)."""
    g = df.pivot_table(index=["symbol", "date"], columns="slot", values=col, aggfunc="first")
    return g.reindex(columns=range(13)).dropna()


def opening_range(df: pd.DataFrame) -> pd.DataFrame:
    """Break of the first half hour's range: in at the next bar's open, out at the close (stocks).
    Vectorized: a whole column of days at a time."""
    stocks = df[~df["symbol"].isin(ETFS)]
    if stocks.empty:
        return pd.DataFrame(columns=["date", "symbol", "ret", "cost"])
    close, opn, high = _grid(stocks, "close"), _grid(stocks, "open"), _grid(stocks, "high")
    low = _grid(stocks, "low")
    idx = close.index.intersection(opn.index).intersection(high.index).intersection(low.index)
    close, opn, high0, low0 = close.loc[idx], opn.loc[idx], high.loc[idx, 0], low.loc[idx, 0]
    c = close[list(range(1, 12))].to_numpy()                       # a bar closing outside the range ...
    up, down = c > high0.to_numpy()[:, None], c < low0.to_numpy()[:, None]
    hit = up | down
    any_hit = hit.any(axis=1)
    first = hit.argmax(axis=1)                                     # the first such bar (slot first + 1)
    rows = np.arange(len(c))
    side = np.where(up[rows, first], 1.0, -1.0)
    entry = opn.to_numpy()[rows, first + 2]                        # ... in at the next bar's open
    ret = side * (close[12].to_numpy() / entry - 1)
    keep = any_hit
    out = pd.DataFrame({"date": idx.get_level_values("date")[keep], "symbol": idx.get_level_values("symbol")[keep],
                        "ret": ret[keep]})
    return out.assign(cost=COST["stock"]).reset_index(drop=True)


def gap_fade(df: pd.DataFrame, gap: float = GAP) -> pd.DataFrame:
    """Against an opening gap of more than `gap`: from the open to the close (stocks)."""
    d = _days(df[~df["symbol"].isin(ETFS)]).dropna(subset=["prev_close"])
    g = d["open"] / d["prev_close"] - 1
    d = d[g.abs() > gap]
    return pd.DataFrame({"date": d["date"], "symbol": d["symbol"],
                         "ret": -np.sign(g[g.abs() > gap]) * (d["close"] / d["open"] - 1),
                         "cost": COST["stock"], "prev": d["prev_close"], "open": d["open"], "close": d["close"]})


def worst_text(df: pd.DataFrame, name: str = "gap_fade", n: int = 8) -> str:
    """A pattern's worst trades with their prices, to tell a real crash from bad data (a split, a stray
    print): a "gap" near a split ratio (1:2, 1:3, 10:1 ...) is almost always the data, not the market."""
    t = STRATEGIES[name](df)
    if t.empty:
        return ""
    lines = ["", f"Worst trades of {NAMES[name]} (check the prices; a split or a stray print shows here):"]
    for r in t.nsmallest(n, "ret").itertuples():
        extra = ""
        if hasattr(r, "prev"):
            ratio = r.open / r.prev
            split = any(abs(ratio * k - 1) < 0.03 or abs(ratio / k - 1) < 0.03 for k in (2, 3, 4, 5, 10, 20))
            extra = (f": yesterday {r.prev:,.2f}, open {r.open:,.2f} ({ratio - 1:+.0%}), close {r.close:,.2f}"
                     + (" ← looks like a split" if split else ""))
        lines.append(f"  {r.date} {r.symbol}: {r.ret:+.1%}{extra}")
    return "\n".join(lines)


def opening_drive(df: pd.DataFrame) -> pd.DataFrame:
    """The first half hour's direction carries on for the next hour: in at 10:00, out at 11:00 (stocks)."""
    stocks = df[~df["symbol"].isin(ETFS)]
    opn, close = _grid(stocks, "open"), _grid(stocks, "close")
    idx = opn.index.intersection(close.index)
    opn, close = opn.loc[idx], close.loc[idx]
    first = close[0] / opn[0] - 1
    ret = np.sign(first) * (close[2] / opn[1] - 1)
    keep = first.abs() > 0.005                                 # only a clear first half hour (over 0.5%)
    return pd.DataFrame({"date": idx.get_level_values("date")[keep], "symbol": idx.get_level_values("symbol")[keep],
                         "ret": ret[keep].to_numpy(), "cost": COST["stock"]})


def eod_reversal(df: pd.DataFrame, move: float = GAP) -> pd.DataFrame:
    """A stock that moved more than `move` from the open to 15:30 gives some back in the last half hour."""
    stocks = df[~df["symbol"].isin(ETFS)]
    opn, close = _grid(stocks, "open"), _grid(stocks, "close")
    idx = opn.index.intersection(close.index)
    opn, close = opn.loc[idx], close.loc[idx]
    day = opn[12] / opn[0] - 1
    keep = day.abs() > move
    ret = -np.sign(day) * (close[12] / opn[12] - 1)
    return pd.DataFrame({"date": idx.get_level_values("date")[keep], "symbol": idx.get_level_values("symbol")[keep],
                         "ret": ret[keep].to_numpy(), "cost": COST["stock"]})


STRATEGIES = {"intraday_momentum": intraday_momentum, "opening_range": opening_range, "gap_fade": gap_fade,
              "opening_drive": opening_drive, "eod_reversal": eod_reversal}
NAMES = {"intraday_momentum": "Market's last half hour follows its first",
         "opening_range": "Breakout from the first half hour's range",
         "gap_fade": "Fade big opening gaps",
         "opening_drive": "First half hour's direction carries on (new, tested only)",
         "eod_reversal": "Big daily movers give some back at the end (new, tested only)"}
LIVE = {"intraday_momentum", "opening_range", "gap_fade"}    # the ones the day-trading account trades


# --- Exits: hold to the close, or take the profit (or the loss) during the day? -------------------

EXITS = [(None, None), (0.05, None), (0.05, 0.02), (0.05, 0.03), (0.05, 0.05)]   # (stop, take profit)


def _walk(side: np.ndarray, entry: np.ndarray, start: np.ndarray, high: np.ndarray, low: np.ndarray,
          close: np.ndarray, stop: float | None, take: float | None) -> np.ndarray:
    """Each trade's return from `entry` (bought or sold short at half hour `start`) to the close, unless
    the stop or the take-profit level is touched first. If both are touched in the same half hour we
    don't know which came first, so the stop counts (the cautious choice)."""
    ret = side * (close[:, 12] / entry - 1)
    done = np.zeros(len(entry), bool)
    for k in range(13):
        live = ~done & (k >= start)
        if stop:
            bad = np.where(side > 0, low[:, k] <= entry * (1 - stop), high[:, k] >= entry * (1 + stop))
            hit = live & bad
            ret[hit], done = -stop, done | hit
        if take:
            live = ~done & (k >= start)
            good = np.where(side > 0, high[:, k] >= entry * (1 + take), low[:, k] <= entry * (1 - take))
            hit = live & good
            ret[hit], done = take, done | hit
    return ret


def _paths(df: pd.DataFrame) -> dict[str, dict]:
    """The stock patterns' trades with what's needed to follow them through the day."""
    stocks = df[~df["symbol"].isin(ETFS)]
    grids = [_grid(stocks, c) for c in ("open", "high", "low", "close")]
    idx = grids[0].index
    for g in grids[1:]:
        idx = idx.intersection(g.index)
    opn, high, low, close = (g.loc[idx].sort_index() for g in grids)
    idx = opn.index
    o, h, lo_, c = (x.to_numpy() for x in (opn, high, low, close))
    out = {}
    prev = close[12].groupby(level="symbol").shift(1).to_numpy()          # yesterday's close
    gap = o[:, 0] / prev - 1
    keep = np.abs(gap) > GAP
    out["gap_fade"] = {"side": -np.sign(gap[keep]), "entry": o[keep, 0], "start": np.zeros(keep.sum(), int),
                       "rows": keep}
    cc = c[:, 1:12]
    up, down = cc > h[:, [0]], cc < lo_[:, [0]]
    first = (up | down).argmax(axis=1)
    keep = (up | down).any(axis=1)
    rows = np.arange(len(cc))
    out["opening_range"] = {"side": np.where(up[rows, first], 1.0, -1.0)[keep],
                            "entry": o[rows, first + 2][keep], "start": (first + 2)[keep], "rows": keep}
    for p in out.values():
        r = p.pop("rows")
        p.update(high=h[r], low=lo_[r], close=c[r], date=idx.get_level_values("date")[r],
                 symbol=idx.get_level_values("symbol")[r])
    return out


def exits_study(df: pd.DataFrame, draws: int = 200) -> dict[str, dict[str, dict]]:
    """Per pattern: each exit rule (EXITS) on the same trades, after costs."""
    out = {}
    for name, p in _paths(df).items():
        out[name] = {}
        for stop, take in EXITS:
            ret = _walk(p["side"], p["entry"], p["start"], p["high"], p["low"], p["close"], stop, take)
            label = ("held to the close" if not stop else f"stop {stop:.0%}" + (f", take profit at +{take:.0%}" if take
                                                                                 else ", else to the close"))
            trades = pd.DataFrame({"date": p["date"], "symbol": p["symbol"], "ret": ret, "cost": COST["stock"]})
            out[name][label] = evaluate(trades, draws=draws)
    return out


def exits_text(ex: dict[str, dict[str, dict]]) -> str:
    lines = ["", "Exits (the same trades, sold at a profit target or a stop during the day instead of only at the "
                 "close; if both are touched in the same half hour, the stop counts):"]
    for name, rules in ex.items():
        lines.append(f"  {NAMES[name]}:")
        for label, r in rules.items():
            if not r.get("days"):
                continue
            lines.append(f"    {label:<40} per trade {r['per_trade']:+.3%} after costs, right {r['hit']:.0%}, "
                         f"yearly {r['yearly']:+.1%} → {verdict(r)}")
    return "\n".join(lines)


# --- Filters: does a pattern work better on some days? -----------------------------------------

def earnings_days(con: sqlite3.Connection, symbols: list[str], fetch=None, log=print) -> set[tuple[str, str]]:
    """(symbol, trading day the report hit the price) for each earnings report, from Yahoo (kept in the
    database; fetched once). Reports after the close count for the next trading day."""
    have = {s for (s,) in con.execute("SELECT DISTINCT symbol FROM earnings")}
    todo = [s for s in symbols if s not in ETFS and s not in have]
    if todo:
        fetch = fetch or _yahoo_earnings
        rows = []
        for s in todo:
            try:
                for ts in fetch(s):
                    t = pd.Timestamp(ts)
                    t = t.tz_convert(NY) if t.tzinfo else t
                    day = t.normalize() + (pd.offsets.BDay(1) if t.hour >= 16 else pd.Timedelta(0))
                    rows.append((s, f"{day:%Y-%m-%d}"))
            except Exception as e:  # noqa: BLE001 - one symbol missing just leaves it out of the filter
                log(f"[intraday] no earnings dates for {s} ({str(e)[:60]})")
        con.executemany("INSERT OR IGNORE INTO earnings VALUES (?, ?)", rows)
        con.commit()
    return {(s, d) for s, d in con.execute("SELECT symbol, date FROM earnings")}


def _yahoo_earnings(symbol: str) -> list:
    import yfinance as yf

    df = yf.Ticker(symbol).get_earnings_dates(limit=60)
    return list(df.index) if df is not None else []



def nervous_days(df: pd.DataFrame) -> set[str]:
    """Days the market's first half hour moved more than usual (bigger than the median of the previous
    year's days; known by 10:00, so a filter could really use it)."""
    d = _days(df[df["symbol"] == "SPY"]).dropna(subset=["prev_close"]).set_index("date").sort_index()
    move = (d["close0"] / d["prev_close"] - 1).abs()
    usual = move.rolling(250, min_periods=60).median().shift(1)
    return set(move.index[move > usual])


def filters(df: pd.DataFrame, earnings: set[tuple[str, str]] | None, draws: int = 200) -> dict[str, dict]:
    """Each pattern on subsets of its own trades: earnings days vs other days, nervous vs calm markets."""
    nervous = nervous_days(df)
    out = {}
    for name, f in STRATEGIES.items():
        t = f(df)
        if t.empty:
            continue
        parts = {"nervous market": t[t["date"].isin(nervous)], "calm market": t[~t["date"].isin(nervous)]}
        if earnings and name != "intraday_momentum":          # the index funds have no earnings days
            on = pd.Series([(s, d) in earnings for s, d in zip(t["symbol"], t["date"])], index=t.index)
            parts.update({"earnings days": t[on], "other days": t[~on]})
        out[name] = {k: evaluate(v, draws=draws) for k, v in parts.items()}
    return out


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
            "sd_trade": float(net.std(ddof=1)) if len(net) > 1 else np.nan,
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


def filters_text(f: dict[str, dict]) -> str:
    if not f:
        return ""
    lines = ["", "Filters (each pattern on part of its own trades; a filter is worth using only if one side "
                 "passes and the other doesn't):"]
    for name, parts in f.items():
        lines.append(f"  {NAMES[name]}:")
        for k, r in parts.items():
            if not r.get("days"):
                lines.append(f"    {k:<15} no trades")
                continue
            lines.append(f"    {k:<15} {r['trades']:>6,} trades, {r['per_trade']:+.3%} a trade, both halves "
                         f"{'+' if r['early'] > 0 and r['late'] > 0 else '–'}, luck {r['luck']:.0%} → "
                         f"{'passes' if verdict(r).startswith('PASSES') else 'no'}")
    return "\n".join(lines)


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
