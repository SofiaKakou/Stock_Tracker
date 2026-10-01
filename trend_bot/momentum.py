"""Momentum: do last year's winners keep winning?

Each month-end, rank every eligible stock by its return over the past
`lookback` months, skipping the most recent `skip` month(s) (short-term moves
tend to reverse). Hold the top slice equally weighted for the next month,
then re-rank. Compared with holding every eligible stock equally, the bottom
slice, and a benchmark ETF.

Survivorship bias matters here too: only companies still listed today have
prices, and some of them were big winners, which flatters the results.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from trend_bot.company_info import fund_tickers
from trend_bot.db import load_many, load_prices

MONTHS = 12


def monthly_panel(con: sqlite3.Connection, chunk: int = 300) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Month-end closes and average daily $ volume per stock (months x tickers). Funds are left out."""
    skip = fund_tickers(con)
    tickers = [t for (t,) in con.execute("SELECT ticker FROM tickers WHERE last_date IS NOT NULL ORDER BY ticker")
               if t not in skip]
    closes, dollar_vol = {}, {}
    for i in range(0, len(tickers), chunk):
        for t, df in load_many(con, tickers[i : i + chunk]).items():
            m = df.resample("ME")
            closes[t] = m["Close"].last()
            dollar_vol[t] = (df["Close"] * df["Volume"]).resample("ME").mean()
    return pd.DataFrame(closes), pd.DataFrame(dollar_vol)


def backtest(closes: pd.DataFrame, dollar_vol: pd.DataFrame, lookback: int = 12, skip: int = 1,
             top: float = 0.1, min_price: float = 5, min_dollar_vol: float = 1e6,
             cost_bps: float = 10) -> pd.DataFrame:
    """Monthly returns of the top slice, bottom slice and all eligible stocks.

    The ranking uses only prices up to each month-end, and the portfolio earns
    the following month's return. Costs are charged on the share of the
    portfolio that changes each month.
    """
    score = closes.shift(skip) / closes.shift(lookback) - 1
    nxt = closes.shift(-1) / closes - 1
    rows, prev_top, prev_bottom = [], set(), set()
    for month in closes.index[lookback:-1]:
        ok = ((closes.loc[month] >= min_price) & (dollar_vol.loc[month] >= min_dollar_vol)
              & score.loc[month].notna() & nxt.loc[month].notna())
        s = score.loc[month][ok]
        if len(s) < 20:  # too few stocks to form slices
            continue
        k = max(1, int(len(s) * top))
        winners, losers = set(s.nlargest(k).index), set(s.nsmallest(k).index)
        r = nxt.loc[month]

        def turnover(new: set, old: set) -> float:
            return 1.0 if not old else 1 - len(new & old) / len(new)

        cost = cost_bps / 10_000 * 2  # sell the leavers, buy the joiners
        rows.append({
            "month": month + pd.offsets.MonthEnd(1),  # the month the return is earned in
            "top": r[list(winners)].mean() - cost * turnover(winners, prev_top),
            "bottom": r[list(losers)].mean() - cost * turnover(losers, prev_bottom),
            "all": r[ok].mean(),
            "stocks": int(ok.sum()),
        })
        prev_top, prev_bottom = winners, losers
    if not rows:
        return pd.DataFrame(columns=["top", "bottom", "all", "stocks"], index=pd.DatetimeIndex([], name="month"))
    return pd.DataFrame(rows).set_index("month")


def add_benchmark(con: sqlite3.Connection, monthly: pd.DataFrame, benchmark: str) -> pd.DataFrame:
    b = load_prices(con, benchmark)["Close"].resample("ME").last().pct_change()
    return monthly.join(b.rename(benchmark), how="left")


def stats(monthly: pd.DataFrame, columns: list[str], base: str = "all") -> pd.DataFrame:
    rows = []
    for col in columns:
        r = monthly[col].dropna()
        if r.empty:
            continue
        equity = (1 + r).cumprod()
        years = len(r) / MONTHS
        rows.append({
            "portfolio": col, "cagr": equity.iloc[-1] ** (1 / years) - 1 if years else np.nan,
            "volatility": r.std() * np.sqrt(MONTHS),
            "max_drawdown": (equity / equity.cummax() - 1).min(),
            "beat_all_months": (r > monthly.loc[r.index, base]).mean() if col != base else np.nan,
            "total": equity.iloc[-1] - 1,
        })
    return pd.DataFrame(rows).set_index("portfolio")


def yearly(monthly: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = (1 + monthly[columns]).groupby(monthly.index.year).prod() - 1
    return out.rename_axis("year")
