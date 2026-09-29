import numpy as np
import pandas as pd
import pytest

from trend_bot import momentum


def panel(n_stocks=50, months=60, persistent=True, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2015-01-31", periods=months, freq="ME")
    drift = rng.normal(0, 0.02, n_stocks) if persistent else np.zeros(n_stocks)
    rets = drift + rng.normal(0, 0.03, (months, n_stocks))
    closes = pd.DataFrame(20 * np.exp(np.cumsum(rets, axis=0)), index=idx,
                          columns=[f"S{i:02d}" for i in range(n_stocks)])
    return closes, pd.DataFrame(5e6, index=idx, columns=closes.columns)


def test_winners_keep_winning_when_trends_persist():
    closes, dv = panel()
    m = momentum.backtest(closes, dv, lookback=12, top=0.1, cost_bps=0)
    s = momentum.stats(m, ["top", "all", "bottom"])
    assert s.loc["top", "cagr"] > s.loc["all", "cagr"] > s.loc["bottom", "cagr"]
    assert len(m) == 60 - 12 - 1 and m["stocks"].iloc[0] == 50  # later, some drift below $5 and drop out


def test_no_lookahead_and_filters():
    closes, dv = panel()
    m = momentum.backtest(closes, dv, cost_bps=0)
    first = m.index[0]
    # The first return is earned in the month after the first ranking (month 12).
    assert first == closes.index[13]
    # Penny stocks and thin trading are left out.
    dv.iloc[:, :40] = 1
    m2 = momentum.backtest(closes, dv, min_dollar_vol=1e6)
    assert m2.empty  # only 10 eligible stocks -> fewer than 20, no slices


def test_costs_lower_returns():
    closes, dv = panel()
    free = momentum.backtest(closes, dv, cost_bps=0)["top"].sum()
    costly = momentum.backtest(closes, dv, cost_bps=50)["top"].sum()
    assert costly < free


def test_yearly_table():
    closes, dv = panel()
    m = momentum.backtest(closes, dv)
    y = momentum.yearly(m, ["top", "all"])
    assert list(y.index) == sorted(set(m.index.year))
