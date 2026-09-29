"""Market-wide screening from the local database."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pandas as pd

from trend_bot.db import load_many
from trend_bot.strategy import Strategy


def liquid_tickers(con: sqlite3.Connection, min_price: float = 5, min_dollar_vol: float = 1e6,
                   stale_days: int = 7) -> list[str]:
    """Tickers with recent prices that aren't penny stocks or barely traded."""
    latest = con.execute("SELECT MAX(last_date) FROM tickers").fetchone()[0]
    if latest is None:
        return []
    fresh = (dt.date.fromisoformat(latest) - dt.timedelta(days=stale_days)).isoformat()
    rows = con.execute(
        "SELECT ticker FROM tickers WHERE last_date >= ? AND last_close >= ? AND dollar_vol >= ? ORDER BY ticker",
        (fresh, min_price, min_dollar_vol),
    )
    return [r[0] for r in rows]


def insider_buying(con: sqlite3.Connection, since: dt.date, min_buyers: int = 2) -> pd.DataFrame:
    """Per ticker: open-market insider buys filed on/after `since` (clusters when buyers >= min_buyers)."""
    df = pd.read_sql_query(
        """SELECT ticker, insider, filed, shares * COALESCE(price, 0) AS value
           FROM insider_trades WHERE code = 'P' AND ticker IS NOT NULL AND filed >= ?""",
        con, params=[since.isoformat()],
    )
    if df.empty:
        return pd.DataFrame(columns=["buyers", "buy_value", "last_buy_filed", "cluster"]).rename_axis("ticker")
    out = df.groupby("ticker").agg(buyers=("insider", "nunique"), buy_value=("value", "sum"),
                                   last_buy_filed=("filed", "max"))
    out["cluster"] = out["buyers"] >= min_buyers
    return out


def _chg(close: pd.Series, n: int) -> float:
    return float(close.iloc[-1] / close.iloc[-n - 1] - 1) if len(close) > n else float("nan")


def screen(con: sqlite3.Connection, strategy: Strategy, days: int = 1, min_price: float = 5,
           min_dollar_vol: float = 1e6, cluster_days: int = 30, min_buyers: int = 2,
           lookback_bars: int = 400, chunk: int = 300) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Trend flips in the last `days` bars across the liquid universe, plus insider clusters.

    Returns (flips, clusters). Both include current trend and insider columns.
    """
    tickers = liquid_tickers(con, min_price, min_dollar_vol)
    latest = con.execute("SELECT MAX(last_date) FROM tickers").fetchone()[0]
    if not tickers or latest is None:
        empty = pd.DataFrame()
        return empty, empty
    start = (dt.date.fromisoformat(latest) - dt.timedelta(days=int(lookback_bars * 1.5))).isoformat()
    names = dict(con.execute("SELECT ticker, name FROM tickers"))
    ins = insider_buying(con, dt.date.fromisoformat(latest) - dt.timedelta(days=cluster_days), min_buyers)

    state_rows, flip_rows = [], []
    for i in range(0, len(tickers), chunk):
        for t, prices in load_many(con, tickers[i : i + chunk], start).items():
            if len(prices) < 30:
                continue
            try:
                sig = strategy.generate(prices)
            except ValueError:
                continue
            pos, close = sig["position"], sig["Close"]
            base = {
                "ticker": t, "name": (names.get(t) or "")[:28], "date": sig.index[-1].date(),
                "close": round(float(close.iloc[-1]), 2), "chg_20d": _chg(close, 20),
                "rsi": round(float(sig["rsi"].iloc[-1]), 1) if "rsi" in sig and pd.notna(sig["rsi"].iloc[-1]) else None,
                "trend": "UP" if pos.iloc[-1] else "DOWN",
            }
            state_rows.append(base)
            changes = pos.diff().iloc[-days:]
            if changes.ne(0).any() and changes.notna().any():
                when = changes[changes != 0].index[-1]
                flip_rows.append(base | {"signal": "BUY" if changes[when] > 0 else "SELL", "flip_date": when.date()})

    def with_insiders(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.set_index("ticker").join(ins[["buyers", "buy_value", "cluster"]], how="left")
        df["buyers"] = df["buyers"].fillna(0).astype(int)
        df["buy_value"] = df["buy_value"].fillna(0.0)
        df["cluster"] = df["cluster"].fillna(False).astype(bool)
        return df

    flips = with_insiders(pd.DataFrame(flip_rows))
    if not flips.empty:
        flips = flips.sort_values(["cluster", "signal", "buyers", "chg_20d"], ascending=[False, True, False, False])
    states = with_insiders(pd.DataFrame(state_rows))
    clusters = states[states["cluster"]].sort_values("buy_value", ascending=False) if not states.empty else states
    return flips, clusters
