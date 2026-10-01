"""Earnings surprises and post-earnings drift.

After a company reports earnings far above (or below) what its own history
suggested, its stock has tended to keep drifting the same way for weeks
("post-earnings-announcement drift"). Three signals per quarterly report:

- sue: standardized unexpected earnings. This quarter's net income minus the
  same quarter a year earlier (a "seasonal random walk" expectation), divided
  by how much that difference usually varies for this company (last 8 quarters).
- revenue_sue: the same for revenue.
- ear: earnings announcement return. The stock's return minus the S&P 500's
  over the 3 trading days around the filing.

Each report counts from the day its 10-Q/10-K was filed with the SEC (a bit
after the press release, so slightly conservative), and stays "fresh" for 95 days.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from trend_bot.db import load_many, load_prices

FRESH_DAYS = 95


def quarterly_series(facts: pd.DataFrame, item: str) -> pd.DataFrame:
    """One value per company and fiscal quarter, as first reported: [cik, end, filed, val].

    Fourth quarters are rarely reported on their own, so Q4 = full year minus the
    nine months to Q3 (or minus the three reported quarters).
    """
    f = facts[facts["item"] == item].copy()
    if f.empty:
        return pd.DataFrame(columns=["cik", "end", "filed", "val"])
    days = f["days"] if "days" in f else (f["end"] - f["start"]).dt.days
    first = lambda df: (df.sort_values(["priority", "filed"]).drop_duplicates(["cik", "end"], keep="first"))
    q = first(f[days.between(80, 100)])[["cik", "end", "filed", "val"]]
    fy = first(f[days.between(350, 380)])
    ytd9 = first(f[days.between(260, 285)])

    q4 = []
    ytd_by = {cik: g for cik, g in ytd9.groupby("cik")}
    q_by = {cik: g for cik, g in q.groupby("cik")}
    for r in fy.itertuples(index=False):
        nine = ytd_by.get(r.cik)
        val = None
        if nine is not None:
            m = nine[(nine["start"] == r.start) & (nine["end"] < r.end)]
            if len(m):
                val = r.val - m["val"].iloc[0]
        if val is None and r.cik in q_by:
            inside = q_by[r.cik]
            inside = inside[(inside["end"] > r.start) & (inside["end"] < r.end - pd.Timedelta(days=40))]
            if len(inside) == 3:
                val = r.val - inside["val"].sum()
        if val is not None:
            q4.append((r.cik, r.end, r.filed, val))
    q4 = pd.DataFrame(q4, columns=["cik", "end", "filed", "val"])
    out = pd.concat([q, q4], ignore_index=True)
    return out.sort_values(["cik", "end"]).drop_duplicates(["cik", "end"], keep="first").reset_index(drop=True)


def surprises(q: pd.DataFrame, history: int = 8, min_history: int = 4) -> pd.DataFrame:
    """Standardized unexpected values per quarter: [cik, end, filed, sue]."""
    rows = []
    for cik, g in q.groupby("cik", sort=False):
        g = g.sort_values("end").reset_index(drop=True)
        ends, vals = g["end"].to_numpy(), g["val"].to_numpy(float)
        diffs = []  # (index, change vs the same quarter a year earlier)
        for i in range(len(g)):
            # the quarter ending 350-380 days earlier
            lag = (ends[i] - ends[:i]).astype("timedelta64[D]").astype(int)
            j = np.where((lag >= 350) & (lag <= 380))[0]
            if len(j):
                diffs.append((i, vals[i] - vals[j[-1]]))
        for k, (i, d) in enumerate(diffs):
            past = [x for _, x in diffs[max(0, k - history):k]]
            if len(past) < min_history:
                continue
            sd = np.std(past, ddof=1)
            if sd > 0:
                rows.append((cik, g.at[i, "end"], g.at[i, "filed"], d / sd))
    return pd.DataFrame(rows, columns=["cik", "end", "filed", "sue"])


def announcement_returns(con: sqlite3.Connection, events: pd.DataFrame, tickers: dict[int, str],
                         benchmark: str = "SPY", chunk: int = 300) -> pd.Series:
    """Stock minus benchmark return from the close before the filing day to the close after it."""
    bench = load_prices(con, benchmark)["Close"]
    out = pd.Series(np.nan, index=events.index)
    events = events[events["cik"].isin(list(tickers))]
    by_ticker = {tickers[c]: g for c, g in events.groupby("cik")}
    names = sorted(by_ticker)
    for i in range(0, len(names), chunk):
        for t, df in load_many(con, names[i : i + chunk]).items():
            close = df["Close"]
            ev = by_ticker[t]
            pos = close.index.searchsorted(pd.DatetimeIndex(ev["filed"]))
            for idx, p in zip(ev.index, pos):
                if p < 1 or p + 1 >= len(close):
                    continue
                d0, d1 = close.index[p - 1], close.index[p + 1]
                if d0 not in bench.index or d1 not in bench.index:
                    continue
                out[idx] = (close.iloc[p + 1] / close.iloc[p - 1] - 1) - (bench[d1] / bench[d0] - 1)
    return out


def events(con: sqlite3.Connection, facts: pd.DataFrame, tickers: dict[int, str]) -> pd.DataFrame:
    """Every 10-Q/10-K filing with its announcement return, plus surprises where they can be computed.

    The reaction is measured for every filing; the surprises need a few years of quarterly history.
    """
    days = facts["days"] if "days" in facts else (facts["end"] - facts["start"]).dt.days
    reports = facts[facts["item"].isin(["net_income", "revenue"]) & days.notna()]
    ev = reports[["cik", "filed"]].drop_duplicates().reset_index(drop=True)
    for item, col in (("net_income", "sue"), ("revenue", "revenue_sue")):
        s = surprises(quarterly_series(facts, item)).rename(columns={"sue": col})
        s = s.sort_values("end").drop_duplicates(["cik", "filed"], keep="last")[["cik", "filed", col]]
        ev = ev.merge(s, on=["cik", "filed"], how="left")
    ev["ear"] = announcement_returns(con, ev, tickers) if len(ev) else np.nan
    return ev


def latest(ev: pd.DataFrame, when: pd.Timestamp, fresh_days: int = FRESH_DAYS) -> pd.DataFrame:
    """Per company: the most recent report filed in the `fresh_days` up to `when`."""
    recent = ev[(ev["filed"] <= when) & (ev["filed"] > when - pd.Timedelta(days=fresh_days))]
    return recent.sort_values("filed").drop_duplicates("cik", keep="last").set_index("cik")[
        ["sue", "revenue_sue", "ear"]]
