"""Event studies: did a signal historically come before better-than-market returns?

For every event (e.g. an insider cluster buy becoming public), we buy at the
next trading day's close and measure the return over several horizons,
alone and relative to SPY over the same days.

Caveat (survivorship bias): the database only has prices for companies that
are still listed today. add_fates() puts back companies that went bankrupt or
were bought out, using what their SEC filings say happened to them.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from trend_bot.db import load_many, load_prices
from trend_bot.strategy import Strategy

HORIZONS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}


OFFICER_WORDS = ("chief", "ceo", "cfo", "coo", "president", "officer")


def cluster_events(con: sqlite3.Connection, window_days: int = 30, min_buyers: int = 2,
                   since: str = "2006-01-01", min_value: float = 0, officers_only: bool = False) -> pd.DataFrame:
    """One event per insider buying cluster: the day it became public (the filing that completed it).

    min_value: only count clusters whose buys total at least this many dollars.
    officers_only: only count buys by executives (CEO, CFO, president, ...), not
    directors or 10% owners.
    """
    df = pd.read_sql_query(
        """SELECT ticker, cik, insider, role, filed, price, shares * COALESCE(price, 0) AS value FROM insider_trades
           WHERE code = 'P' AND ticker IS NOT NULL AND filed >= ? ORDER BY ticker, filed""",
        con, params=[since], parse_dates=["filed"],
    )
    if officers_only:
        role = df["role"].fillna("").str.lower()
        df = df[role.apply(lambda r: any(w in r for w in OFFICER_WORDS))]
    events = []
    window = pd.Timedelta(days=window_days)
    for ticker, g in df.groupby("ticker", sort=False):
        last_event = None
        filed, insider, value = g["filed"].tolist(), g["insider"].tolist(), g["value"].tolist()
        prices, cik = g["price"].tolist(), g["cik"].dropna().iloc[0] if g["cik"].notna().any() else None
        lo = 0
        for hi in range(len(filed)):
            while filed[hi] - filed[lo] > window:
                lo += 1
            buyers = set(insider[lo : hi + 1])
            total = float(sum(value[lo : hi + 1]))
            if (len(buyers) >= min_buyers and total >= min_value
                    and (last_event is None or filed[hi] - last_event > window)):
                last_event = filed[hi]
                paid = [p for p in prices[lo : hi + 1] if p and p > 0]
                events.append({"ticker": ticker, "date": filed[hi], "buyers": len(buyers), "value": total,
                               "cik": cik, "insider_price": float(np.median(paid)) if paid else np.nan})
    return pd.DataFrame(events, columns=["ticker", "date", "buyers", "value", "cik", "insider_price"])


def _forward(close: pd.Series, dates: pd.Series, horizons: dict[str, int], after: bool = True) -> pd.DataFrame:
    """Forward returns from the first close after each date (or on it, with after=False)."""
    idx = close.index.searchsorted(pd.DatetimeIndex(dates), side="right" if after else "left")
    values = close.to_numpy()
    out = {"entry_date": [close.index[i] if i < len(close) else pd.NaT for i in idx],
           "entry_price": [values[i] if i < len(values) else np.nan for i in idx]}
    for name, h in horizons.items():
        out[name] = [values[i + h] / values[i] - 1 if i + h < len(values) else np.nan for i in idx]
    return pd.DataFrame(out, index=dates.index)


def add_returns(con: sqlite3.Connection, events: pd.DataFrame, strategy: Strategy | None = None,
                horizons: dict[str, int] = HORIZONS, benchmark: str = "SPY", chunk: int = 300) -> pd.DataFrame:
    """Attach forward returns (and excess over the benchmark) to events; optionally the trend at the event."""
    if events.empty:
        return events
    bench = load_prices(con, benchmark)["Close"]
    parts = []
    tickers = events["ticker"].unique().tolist()
    for i in range(0, len(tickers), chunk):
        prices = load_many(con, tickers[i : i + chunk])
        for t, ev in events[events["ticker"].isin(prices.keys())].groupby("ticker"):
            close = prices[t]["Close"]
            fwd = _forward(close, ev["date"], horizons)
            # Benchmark bought on the same day as the stock.
            b = _forward(bench, fwd["entry_date"].fillna(pd.Timestamp.max.normalize()), horizons, after=False)
            b.index = fwd.index
            for name in horizons:
                fwd[f"{name}_excess"] = fwd[name] - b[name]
            if strategy is not None:
                pos = strategy.generate(prices[t])["position"]
                at = pos.index.searchsorted(pd.DatetimeIndex(ev["date"]), side="right") - 1
                fwd["trend"] = ["UP" if j >= 0 and pos.iloc[j] else "DOWN" for j in at]
            parts.append(ev.join(fwd))
    return pd.concat(parts) if parts else events.iloc[0:0]


def add_fates(con: sqlite3.Connection, events: pd.DataFrame, priced: pd.DataFrame,
              horizons: dict[str, int] = HORIZONS, benchmark: str = "SPY") -> pd.DataFrame:
    """Returns for events whose company has no price data, from what happened to it.

    - bankrupt within the horizon  -> -100%
    - acquired within the horizon  -> same as the benchmark (a neutral assumption)
    - otherwise                    -> unknown (left out)
    The insiders' own purchase price stands in for the entry price (for the $5 filter).
    """
    missing = events.loc[events.index.difference(priced.index)]
    if missing.empty:
        return missing.assign(status=pd.Series(dtype=str))
    fates = {int(c): (s, d) for c, s, d in con.execute("SELECT cik, status, fate_date FROM company_fates")}
    bench = load_prices(con, benchmark)["Close"]
    values = bench.to_numpy()
    rows = []
    for idx, ev in missing.iterrows():
        status, when = fates.get(int(ev["cik"]), ("unchecked", None)) if pd.notna(ev["cik"]) else ("unchecked", None)
        i = bench.index.searchsorted(ev["date"], side="right")
        row = {"status": status, "entry_price": ev["insider_price"],
               "entry_date": bench.index[i] if i < len(bench) else pd.NaT}
        for name, h in horizons.items():
            ret = exc = np.nan
            if i + h < len(values) and when and status in ("bankrupt", "acquired"):
                b = values[i + h] / values[i] - 1
                if pd.Timestamp(when) <= bench.index[i + h]:
                    ret, exc = (-1.0, -1.0 - b) if status == "bankrupt" else (b, 0.0)
            row[name], row[f"{name}_excess"] = ret, exc
        rows.append(pd.Series(row, name=idx))
    return missing.join(pd.DataFrame(rows))


def trend_flip_events(con: sqlite3.Connection, strategy: Strategy, tickers: list[str],
                      since: str = "2006-01-01", chunk: int = 300) -> pd.DataFrame:
    """One event per BUY flip (trend turning up) in the given tickers."""
    events = []
    for i in range(0, len(tickers), chunk):
        for t, prices in load_many(con, tickers[i : i + chunk]).items():
            try:
                pos = strategy.generate(prices)["position"]
            except ValueError:
                continue
            flips = pos.index[pos.diff() > 0]
            events.extend({"ticker": t, "date": d} for d in flips if d >= pd.Timestamp(since))
    return pd.DataFrame(events, columns=["ticker", "date"])


def summarize(events: pd.DataFrame, horizons: dict[str, int] = HORIZONS, min_price: float = 5) -> pd.DataFrame:
    """Average/median return, win rate, and average excess vs the benchmark per horizon."""
    ev = events[events["entry_price"] >= min_price] if "entry_price" in events else events
    if ev.empty:
        return pd.DataFrame(columns=["events", "avg_return", "median_return", "win_rate", "avg_vs_bench",
                                     "median_vs_bench", "beat_bench_rate"], index=pd.Index(list(horizons), name="horizon"))
    rows = []
    for name in horizons:
        r, x = ev[name].dropna(), ev[f"{name}_excess"].dropna()
        rows.append({"horizon": name, "events": len(r), "avg_return": r.mean(), "median_return": r.median(),
                     "win_rate": (r > 0).mean(), "avg_vs_bench": x.mean(), "median_vs_bench": x.median(),
                     "beat_bench_rate": (x > 0).mean()})
    return pd.DataFrame(rows).set_index("horizon")
