"""Trend Score model: combine several documented signals into one score per stock.

Every stock in the universe (the ~1,000 most traded stocks over $5, no funds)
gets a score from 0 to 100:

    25%  trend        price above its 50- and 200-day averages, 50 above 200
    25%  momentum     6- and 12-month return, skipping the latest month
    20%  52-week high how close the price is to its high of the past year
    15%  steadiness   low day-to-day swings over the past 6 months
    15%  insiders     cluster buying in the last 90 days adds, heavy selling subtracts

The weights are fixed round numbers, not fitted to the data, so the backtest
isn't tuned to the past. The model portfolio holds the top `hold` stocks and
checks once a month. A stock is only sold when it falls out of the top `buffer`
(fewer needless trades). The market "weather": when the S&P 500 (SPY) is below
its 200-day average at the monthly check, the model sits in cash.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import numpy as np
import pandas as pd

from trend_bot.company_info import fund_tickers
from trend_bot.db import get_meta, load_many, load_prices, set_meta

def _month_end(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Each date's calendar month-end, at midnight (works the same across pandas versions)."""
    return index.to_period("M").to_timestamp() + pd.offsets.MonthEnd(0)


def _monthly_last(s: pd.Series) -> pd.Series:
    out = s.groupby(s.index.to_period("M")).last()
    out.index = out.index.to_timestamp() + pd.offsets.MonthEnd(0)
    return out


WEIGHTS = {"trend": 0.25, "momentum": 0.25, "high52": 0.20, "steadiness": 0.15, "insiders": 0.15}
FEATURES = ["close", "trend", "mom12", "mom6", "high52", "vol", "dollar_vol"]
INSIDER_DAYS = 90


# --- Features ------------------------------------------------------------------------

def daily_features(df: pd.DataFrame) -> pd.DataFrame:
    """Per-day features for one stock, using only data up to each day."""
    c = df["Close"]
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    trend = ((c > sma50).astype(float) + (c > sma200).astype(float) + (sma50 > sma200).astype(float)) / 3
    return pd.DataFrame({
        "close": c,
        "trend": trend.where(sma200.notna()),
        "mom12": c.shift(21) / c.shift(252) - 1,
        "mom6": c.shift(21) / c.shift(126) - 1,
        "high52": c / c.rolling(252).max(),
        "vol": c.pct_change().rolling(126).std(),
        "dollar_vol": (c * df["Volume"]).rolling(21).mean(),
    })


def model_tickers(con: sqlite3.Connection) -> list[str]:
    funds = fund_tickers(con)
    rows = con.execute("SELECT ticker FROM tickers WHERE last_date IS NOT NULL ORDER BY ticker")
    return [t for (t,) in rows if t not in funds]


def monthly_panel(con: sqlite3.Connection, chunk: int = 300) -> dict[str, pd.DataFrame]:
    """Month-end features for every stock: {feature: months x tickers}."""
    cols: dict[str, dict[str, pd.Series]] = {f: {} for f in FEATURES}
    tickers = model_tickers(con)
    for i in range(0, len(tickers), chunk):
        for t, df in load_many(con, tickers[i : i + chunk]).items():
            if len(df) < 60:
                continue
            f = daily_features(df)
            m = f.groupby(f.index.to_period("M")).tail(1)
            m.index = _month_end(m.index)
            for name in FEATURES:
                cols[name][t] = m[name]
    return {name: pd.DataFrame(series) for name, series in cols.items()}


def insider_counts(con: sqlite3.Connection, dates: pd.DatetimeIndex, days: int = INSIDER_DAYS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(buyers, sellers): distinct insiders buying / selling (not pre-planned) in the `days` up to each date."""
    if len(dates) == 0:
        return pd.DataFrame(), pd.DataFrame()
    since = (dates.min() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    trades = pd.read_sql_query(
        """SELECT DISTINCT ticker, insider, code, filed FROM insider_trades
           WHERE ticker IS NOT NULL AND filed >= ?
             AND (code = 'P' OR (code = 'S' AND COALESCE(planned, 0) = 0))""",
        con, params=[since], parse_dates=["filed"])
    trades["insider"] = trades["insider"].astype("category")
    out = []
    for code in ("P", "S"):
        tr = trades[trades["code"] == code]
        parts = []
        for d in dates:
            window = tr[(tr["filed"] <= d) & (tr["filed"] > d - pd.Timedelta(days=days))]
            parts.append(window.groupby("ticker")["insider"].nunique().rename(d))
        out.append(pd.DataFrame(parts).reindex(dates).fillna(0) if parts else pd.DataFrame(index=dates))
    return out[0], out[1]


# --- Scoring -------------------------------------------------------------------------------

def eligible(feat: pd.DataFrame, universe: int = 1000, min_price: float = 5) -> pd.Series:
    """The `universe` most traded stocks over min_price with every feature available."""
    ok = feat[["trend", "mom12", "mom6", "high52", "vol", "dollar_vol"]].notna().all(axis=1) & (feat["close"] >= min_price)
    top = feat.loc[ok, "dollar_vol"].nlargest(universe).index
    return feat.index.isin(top)


def score(feat: pd.DataFrame, buyers: pd.Series, sellers: pd.Series) -> pd.DataFrame:
    """Scores (0-100) for the stocks in `feat` (rows = tickers), with the parts shown."""
    rank = lambda s: s.rank(pct=True)
    b = buyers.reindex(feat.index).fillna(0)
    s = sellers.reindex(feat.index).fillna(0)
    parts = pd.DataFrame({
        "trend": feat["trend"],
        "momentum": (rank(feat["mom12"]) + rank(feat["mom6"])) / 2,
        "high52": rank(feat["high52"]),
        "steadiness": rank(-feat["vol"]),
        "insiders": (0.5 + 0.5 * (b >= 2) - 0.5 * (s >= 3)).clip(0, 1),
    })
    parts["score"] = 100 * sum(parts[k] * w for k, w in WEIGHTS.items())
    parts["buyers"], parts["sellers"] = b.astype(int), s.astype(int)
    return parts.sort_values("score", ascending=False)


def rebalance(ranked: pd.Series, held: list[str], hold: int = 20, buffer: int = 40) -> tuple[list[str], list[str], list[str]]:
    """(new holdings, buys, sells). Keep stocks still in the top `buffer`, fill up from the top."""
    order = list(ranked.index)
    pos = {t: i for i, t in enumerate(order)}
    keep = [t for t in held if t in pos and pos[t] < buffer]
    keep = sorted(keep, key=pos.get)[:hold]
    buys = [t for t in order if t not in keep][: hold - len(keep)]
    new = keep + buys
    sells = [t for t in held if t not in new]
    return new, buys, sells


def spy_weather(spy: pd.Series) -> pd.Series:
    """True when SPY is above its 200-day average ("invest"), per day."""
    return (spy > spy.rolling(200).mean()).where(spy.rolling(200).mean().notna(), True)


# --- Backtest ---------------------------------------------------------------------------------

def backtest(con: sqlite3.Connection, hold: int = 20, universe: int = 1000, buffer: int = 40,
             min_price: float = 5, cost_bps: float = 10, cash_rate: float = 2.0,
             panel: dict[str, pd.DataFrame] | None = None, benchmark: str = "SPY") -> pd.DataFrame:
    """Monthly returns of the model (with and without the weather filter), SPY, and all eligible stocks."""
    panel = panel or monthly_panel(con)
    months = panel["close"].index
    buyers, sellers = insider_counts(con, months)
    spy_daily = load_prices(con, benchmark)["Close"]
    weather = spy_weather(spy_daily)
    spy_m = _monthly_last(spy_daily)
    weather_m = _monthly_last(weather)

    nxt = panel["close"].shift(-1) / panel["close"] - 1
    cash_m = (1 + cash_rate / 100) ** (1 / 12) - 1
    cost = cost_bps / 10_000 * 2
    held: list[str] = []
    rows = []
    for month in months[:-1]:
        feat = pd.DataFrame({f: panel[f].loc[month] for f in FEATURES})
        feat = feat[eligible(feat, universe, min_price)]
        if len(feat) < hold * 3:
            continue
        ranked = score(feat, buyers.loc[month] if month in buyers.index else pd.Series(dtype=float),
                       sellers.loc[month] if month in sellers.index else pd.Series(dtype=float))["score"]
        new, buys, _ = rebalance(ranked, held, hold, buffer)
        turnover = len(buys) / hold
        r = nxt.loc[month, new].dropna()
        gross = r.mean() if len(r) else 0.0
        invest = bool(weather_m.get(month, True))
        spy_r = spy_m.shift(-1).get(month, np.nan) / spy_m.get(month, np.nan) - 1
        rows.append({
            "month": month + pd.offsets.MonthEnd(1),
            "model": (gross - cost * turnover) if invest else cash_m,
            "model_no_weather": gross - cost * turnover,
            benchmark: spy_r,
            f"{benchmark}_weather": spy_r if invest else cash_m,
            "all_eligible": nxt.loc[month, feat.index].mean(),
            "invested": invest,
            "turnover": turnover,
        })
        held = new
    return pd.DataFrame(rows).set_index("month")


def summarize(monthly: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    rows = []
    for col in columns:
        r = monthly[col].dropna()
        if r.empty:
            continue
        eq = (1 + r).cumprod()
        years = len(r) / 12
        vol = r.std() * np.sqrt(12)
        cagr = eq.iloc[-1] ** (1 / years) - 1
        rows.append({"portfolio": col, "cagr": cagr, "volatility": vol,
                     "max_drawdown": (eq / eq.cummax() - 1).min(),
                     "return_per_risk": cagr / vol if vol else np.nan, "total": eq.iloc[-1] - 1})
    return pd.DataFrame(rows).set_index("portfolio")


# --- Live model portfolio -----------------------------------------------------------------------

def today_scores(con: sqlite3.Connection, universe: int = 1000, min_price: float = 5,
                 chunk: int = 300) -> tuple[pd.DataFrame, str]:
    """Scores for the latest day, and that day's date."""
    latest = con.execute("SELECT MAX(last_date) FROM tickers").fetchone()[0]
    start = (dt.date.fromisoformat(latest) - dt.timedelta(days=420)).isoformat()
    candidates = [t for (t,) in con.execute(
        "SELECT ticker FROM tickers WHERE last_date >= ? AND last_close >= ? ORDER BY dollar_vol DESC LIMIT ?",
        ((dt.date.fromisoformat(latest) - dt.timedelta(days=7)).isoformat(), min_price, universe * 2))]
    funds = fund_tickers(con)
    candidates = [t for t in candidates if t not in funds]
    rows = {}
    for i in range(0, len(candidates), chunk):
        for t, df in load_many(con, candidates[i : i + chunk], start).items():
            if len(df) >= 253:
                rows[t] = daily_features(df).iloc[-1]
    feat = pd.DataFrame(rows).T
    if feat.empty:
        return feat, latest
    feat = feat[eligible(feat, universe, min_price)]
    day = pd.Timestamp(latest)
    buyers, sellers = insider_counts(con, pd.DatetimeIndex([day]))
    b = buyers.iloc[0] if len(buyers.columns) else pd.Series(dtype=float)
    s = sellers.iloc[0] if len(sellers.columns) else pd.Series(dtype=float)
    out = score(feat, b, s)
    out["close"] = feat["close"].reindex(out.index)
    return out, latest


def update_portfolio(con: sqlite3.Connection, hold: int = 20, universe: int = 1000, buffer: int = 40,
                     min_price: float = 5, benchmark: str = "SPY", force: bool = False) -> dict:
    """Recompute today's scores; rebalance on the first run of a new month (or when forced).

    Returns what happened: scores, holdings, buys, sells, weather and whether it changed.
    """
    scores, latest = today_scores(con, universe, min_price)
    weather_now = bool(spy_weather(load_prices(con, benchmark)["Close"]).iloc[-1])
    prev_weather = get_meta(con, "model_weather")
    month = latest[:7]
    held = [t for (t,) in con.execute("SELECT ticker FROM model_holdings ORDER BY rank")]
    rebalanced = force or not held or get_meta(con, "model_month") != month
    buys: list[str] = []
    sells: list[str] = []
    if rebalanced and len(scores):
        new, buys, sells = rebalance(scores["score"], held, hold, buffer)
        entries = {t: (s, p) for t, s, p in con.execute("SELECT ticker, since, entry_price FROM model_holdings")}
        con.execute("DELETE FROM model_holdings")
        for rank, t in enumerate(new, 1):
            since, price = entries.get(t, (latest, float(scores.loc[t, "close"])))
            con.execute("INSERT INTO model_holdings VALUES (?, ?, ?, ?, ?)",
                        (t, rank, since, price, float(scores.loc[t, "score"])))
        set_meta(con, "model_month", month)
    set_meta(con, "model_weather", "invest" if weather_now else "cash")
    con.commit()
    holdings = pd.read_sql_query("SELECT * FROM model_holdings ORDER BY rank", con).set_index("ticker")
    if len(holdings) and len(scores):
        holdings["score_now"] = scores["score"].reindex(holdings.index)
        holdings["close"] = scores["close"].reindex(holdings.index)
        holdings["return"] = holdings["close"] / holdings["entry_price"] - 1
    return {
        "date": latest, "scores": scores, "holdings": holdings, "buys": buys, "sells": sells,
        "rebalanced": rebalanced, "weather": "invest" if weather_now else "cash",
        "weather_changed": prev_weather is not None and prev_weather != ("invest" if weather_now else "cash"),
    }
