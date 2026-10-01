"""Step D: one machine-learning model over every signal, tested only on years it never saw.

Each month-end, every stock in the universe gets ~30 inputs: price trend and
momentum, last month's move, volatility, size, insider buying and selling,
company financials, earnings surprises and short selling. All inputs are
turned into within-month ranks (0 = lowest, 1 = highest), and the model learns
to predict each stock's rank in next month's returns.

The model is a gradient-boosted tree ensemble (LightGBM). It can pick up
combinations a simple average can't, e.g. "cheap AND improving earnings", but it
can also fit noise. So it is tested walk-forward: each year is predicted by a
model trained only on the years before it, then compared with a simple
equal-weight mix of the same signals. It only counts as better if it beats that
mix on the unseen years.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from trend_bot import factors, fundamentals, model

PRICE = ["trend", "mom12", "mom6", "mom1", "high52", "vol", "dollar_vol", "market_cap"]
INSIDERS = ["insider_buyers", "insider_sellers"]
SIGNALS = list(fundamentals.EXPECTED)
FEATURES = PRICE + INSIDERS + SIGNALS

PARAMS = dict(objective="regression", learning_rate=0.03, num_leaves=15, min_data_in_leaf=300,
              feature_fraction=0.7, bagging_fraction=0.7, bagging_freq=1, lambda_l2=10.0,
              verbose=-1, seed=7, deterministic=True, num_threads=2)
TREES = 300
COST = 0.001  # per trade, each way


def dataset(con: sqlite3.Connection, since: str = "2009-06-30", universe: int = 1000,
            include_latest: bool = False, log=print) -> pd.DataFrame:
    """One row per (month, stock) with every input and next month's return."""
    df = factors.monthly_factors(con, since=since, universe=universe, log=log, extras=True,
                                 include_latest=include_latest)
    if df.empty:
        return df
    months = pd.DatetimeIndex(sorted(df["month"].unique()))
    buyers, sellers = model.insider_counts(con, months)
    for name, table in (("insider_buyers", buyers), ("insider_sellers", sellers)):
        long = table.stack().rename(name).rename_axis(["month", "ticker"]).reset_index() if len(table.columns) else None
        df = df.merge(long, on=["month", "ticker"], how="left") if long is not None else df.assign(**{name: np.nan})
        df[name] = df[name].fillna(0)
    for c in FEATURES:
        if c not in df:
            df[c] = np.nan
    return df


def ranked(df: pd.DataFrame) -> pd.DataFrame:
    """Inputs as within-month percentile ranks (missing stays missing)."""
    out = df[FEATURES].groupby(df["month"]).rank(pct=True)
    return out.astype(float)


def target(df: pd.DataFrame) -> pd.Series:
    """Next month's return as a within-month rank, centred on zero."""
    return df.groupby("month")["ret"].rank(pct=True) - 0.5


def train(X: pd.DataFrame, y: pd.Series, trees: int = TREES):
    import lightgbm as lgb

    return lgb.train(PARAMS, lgb.Dataset(X, y, free_raw_data=False), num_boost_round=trees)


def simple_mix(df: pd.DataFrame) -> pd.Series:
    """The baseline: equal-weight average rank of every signal, each pointed the expected way,
    plus 12-month momentum. No fitting at all."""
    cols = [c for c in SIGNALS if df[c].notna().any()]
    mix = factors.composite(df, cols) if cols else pd.Series(np.nan, index=df.index)
    mom = df.groupby("month")["mom12"].rank(pct=True)
    return pd.concat([mix * len(cols), mom], axis=1).sum(axis=1, min_count=1) / (len(cols) + 1)


def walk_forward(df: pd.DataFrame, first_year: int = 2014, trees: int = TREES, log=print) -> pd.DataFrame:
    """Out-of-sample predictions: each year scored by a model trained on earlier years only."""
    df = df.dropna(subset=["ret"]).reset_index(drop=True)
    X, y = ranked(df), target(df)
    out = []
    for year in sorted(y for y in df["month"].dt.year.unique() if y >= first_year):
        tr = df["month"].dt.year < year
        te = df["month"].dt.year == year
        if tr.sum() < 1000:
            continue
        m = train(X[tr], y[tr], trees)
        part = df.loc[te, ["month", "ticker", "ret"]].copy()
        part["model"] = m.predict(X[te])
        out.append(part)
        log(f"[ml] {year}: trained on {int(tr.sum()):,} rows, predicted {int(te.sum()):,}")
    if not out:
        return pd.DataFrame()
    preds = pd.concat(out)
    base = df.loc[preds.index]
    preds["simple_mix"] = simple_mix(df).loc[preds.index]
    preds["momentum"] = base["mom12"]
    preds["quality_value"] = factors.composite(df, factors.QUALITY_VALUE).loc[preds.index]
    return preds


def top_portfolio(preds: pd.DataFrame, col: str, top: float = 0.1) -> pd.DataFrame:
    """Hold the top `top` share each month, equal weight: monthly return, turnover, and the average stock."""
    rows, held = [], set()
    for month, g in preds.dropna(subset=[col]).groupby("month"):
        k = max(1, int(len(g) * top))
        pick = set(g.nlargest(k, col)["ticker"])
        turnover = 1.0 if not held else len(pick - held) / k
        ret = g[g["ticker"].isin(pick)]["ret"].mean()
        rows.append({"month": month, "gross": ret, "net": ret - 2 * COST * turnover, "turnover": turnover,
                     "average_stock": g["ret"].mean()})
        held = pick
    return pd.DataFrame(rows).set_index("month") if rows else pd.DataFrame()


def scorecard(preds: pd.DataFrame, split_year: int = 2020) -> dict[str, pd.DataFrame]:
    """IC, top-minus-bottom and a top-10% portfolio per approach, for all years and both halves."""
    approaches = ["model", "simple_mix", "quality_value", "momentum"]
    periods = {"all": preds, f"before {split_year}": preds[preds["month"].dt.year < split_year],
               f"{split_year} on": preds[preds["month"].dt.year >= split_year]}
    out = {}
    for label, part in periods.items():
        rows = {}
        for a in approaches:
            r = factors.evaluate(part, a)
            p = top_portfolio(part, a)
            if len(p):
                years = len(p) / 12
                r["top10_per_year"] = (1 + p["net"]).prod() ** (1 / years) - 1
                r["average_stock_per_year"] = (1 + p["average_stock"]).prod() ** (1 / years) - 1
                r["turnover"] = p["turnover"].iloc[1:].mean() if len(p) > 1 else np.nan
            rows[a] = r
        out[label] = pd.DataFrame(rows).T
    return out


def verdict(card: dict[str, pd.DataFrame], split_year: int = 2020) -> tuple[bool, str]:
    """Is the model better than the simple mix on unseen years, in both halves?"""
    halves = [k for k in card if k != "all"]
    wins = []
    for h in halves:
        t = card[h]
        if "model" not in t.index or pd.isna(t.loc["model", "mean_ic"]):
            continue
        wins.append(t.loc["model", "mean_ic"] > t.loc["simple_mix", "mean_ic"])
    all_ = card["all"]
    t_ok = all_.loc["model", "ic_t"] > 2 if "model" in all_.index else False
    passed = bool(wins) and all(wins) and bool(t_ok)
    if passed:
        return True, ("PASSED: on years it never saw, the model ranked stocks better than the simple mix "
                      f"in both halves (before and from {split_year}), and the edge is unlikely to be luck.")
    return False, ("NOT PASSED: on years it never saw, the model did not beat the simple equal-weight mix "
                   "consistently. Treat its rankings as no better than the simple mix.")


def importance(m) -> pd.Series:
    gain = pd.Series(m.feature_importance("gain"), index=m.feature_name())
    return (gain / gain.sum()).sort_values(ascending=False)


def today(df: pd.DataFrame, trees: int = TREES) -> tuple[pd.DataFrame, pd.Series]:
    """Train on all known months and rank the latest one. Returns (ranking, input importance)."""
    known = df.dropna(subset=["ret"])
    X_all = ranked(df)
    m = train(X_all.loc[known.index], target(known), trees)
    latest = df["month"].max()
    now = df[df["month"] == latest].copy()
    now["score"] = m.predict(X_all.loc[now.index])
    now["pct"] = now["score"].rank(pct=True)
    return now.sort_values("score", ascending=False).set_index("ticker"), importance(m)
