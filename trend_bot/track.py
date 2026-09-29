"""Forward tracking: record every stock the bot flags, then measure how it did.

This is the only fully honest test of a signal. Nobody can tune a rule to
prices that didn't exist yet when the pick was recorded.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import numpy as np
import pandas as pd

from trend_bot.db import load_many

SIGNALS = {
    "model_buy": "📈 Model buy",
    "model_sell": "📉 Model sell",
    "strong_insider": "🔔 3+ insiders, $250k+, uptrend",
    "insider_cluster": "Insider buying cluster",
    "uptrend": "New uptrend",
    "downtrend": "New downtrend",
}
# Insider clusters stay visible in the screen for weeks; only record them once per stretch.
REPEAT_AFTER_DAYS = 30


def _money(v: float) -> str:
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K"


def record_picks(con: sqlite3.Connection, flips: pd.DataFrame, clusters: pd.DataFrame) -> set[tuple[str, str]]:
    """Save today's flagged stocks. Returns the (ticker, signal) pairs that are new."""
    rows = []
    for t, r in flips.iterrows() if len(flips) else []:
        rows.append((str(r["date"]), t, "uptrend" if r["signal"] == "BUY" else "downtrend", r["close"],
                     f"20d {r['chg_20d']:+.1%}"))
    for t, r in clusters.iterrows() if len(clusters) else []:
        detail = f"{int(r['buyers'])} insiders, {_money(r['buy_value'])}, trend {r['trend']}"
        rows.append((str(r["date"]), t, "insider_cluster", r["close"], detail))
        if r.get("strong"):
            rows.append((str(r["date"]), t, "strong_insider", r["close"], detail))

    new = set()
    for date, ticker, signal, price, detail in rows:
        if signal in ("insider_cluster", "strong_insider"):
            since = (dt.date.fromisoformat(date) - dt.timedelta(days=REPEAT_AFTER_DAYS)).isoformat()
            seen = con.execute("SELECT 1 FROM picks WHERE ticker = ? AND signal = ? AND date >= ? AND date < ?",
                               (ticker, signal, since, date)).fetchone()
            if seen:
                continue
        cur = con.execute("INSERT OR IGNORE INTO picks VALUES (?, ?, ?, ?, ?)",
                          (date, ticker, signal, float(price), detail))
        if cur.rowcount:
            new.add((ticker, signal))
    con.commit()
    return new


def performance(con: sqlite3.Connection, benchmark: str = "SPY", signal: str | None = None) -> pd.DataFrame:
    """Every recorded pick with its return so far and the benchmark's over the same days."""
    q = "SELECT date, ticker, signal, price, detail FROM picks"
    params: list = []
    if signal:
        q += " WHERE signal = ?"
        params.append(signal)
    picks = pd.read_sql_query(q + " ORDER BY date, ticker", con, params=params, parse_dates=["date"])
    if picks.empty:
        return picks
    prices = load_many(con, sorted(set(picks["ticker"]) | {benchmark}), start=str(picks["date"].min().date()))
    bench = prices.get(benchmark, pd.DataFrame({"Close": []}))["Close"]

    def ret(close: pd.Series, start: pd.Timestamp) -> tuple[float, float, pd.Timestamp | None]:
        if close.empty:
            return np.nan, np.nan, None
        i = close.index.searchsorted(start)  # the pick day's close (same adjustment as today's)
        if i >= len(close):
            return np.nan, np.nan, None
        return float(close.iloc[i]), float(close.iloc[-1] / close.iloc[i] - 1), close.index[-1]

    out = []
    for p in picks.itertuples(index=False):
        close = prices.get(p.ticker, pd.DataFrame({"Close": []}))["Close"]
        entry, r, last = ret(close, p.date)
        _, b, _ = ret(bench, p.date)
        out.append({"date": p.date.date(), "ticker": p.ticker, "signal": p.signal, "detail": p.detail,
                    "entry": entry, "now": float(close.iloc[-1]) if len(close) else np.nan,
                    "days": (last - p.date).days if last is not None else np.nan,
                    "return": r, "vs_bench": r - b if pd.notna(r) and pd.notna(b) else np.nan})
    return pd.DataFrame(out)


def summary(perf: pd.DataFrame) -> pd.DataFrame:
    """Per signal: how many picks, average/median return, win rate, and vs the benchmark.

    For a downtrend signal "winning" means the stock went down, so its returns are
    shown as they are (negative is good) - the table says so in its label.
    """
    if perf.empty:
        return pd.DataFrame()
    rows = []
    for sig, g in perf.dropna(subset=["return"]).groupby("signal"):
        rows.append({"signal": SIGNALS.get(sig, sig), "picks": len(g), "avg_days": g["days"].mean(),
                     "avg_return": g["return"].mean(), "median_return": g["return"].median(),
                     "win_rate": (g["return"] > 0).mean(), "avg_vs_bench": g["vs_bench"].mean(),
                     "beat_bench_rate": (g["vs_bench"] > 0).mean()})
    order = list(SIGNALS.values())
    df = pd.DataFrame(rows).set_index("signal")
    return df.loc[[s for s in order if s in df.index] + [s for s in df.index if s not in order]]
