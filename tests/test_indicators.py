import numpy as np
import pandas as pd

from trend_bot import indicators as ind


def test_sma():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    assert ind.sma(s, 3).tolist()[2:] == [2.0, 3.0, 4.0]
    assert ind.sma(s, 3).isna().sum() == 2


def test_rsi_bounds_and_extremes():
    rising = pd.Series(np.arange(1, 50, dtype=float))
    assert ind.rsi(rising).dropna().eq(100).all()
    falling = rising[::-1].reset_index(drop=True)
    assert ind.rsi(falling).dropna().lt(1).all()
    noisy = pd.Series(np.random.default_rng(0).normal(0, 1, 500).cumsum() + 100)
    r = ind.rsi(noisy).dropna()
    assert r.between(0, 100).all()


def test_macd_columns():
    s = pd.Series(np.linspace(1, 100, 100))
    m = ind.macd(s)
    assert list(m.columns) == ["macd", "signal", "hist"]
    assert m["macd"].dropna().gt(0).all()  # steady uptrend: fast EMA above slow


def test_slope_sign():
    assert ind.slope(pd.Series(np.linspace(1, 2, 50))).dropna().gt(0).all()
    assert ind.slope(pd.Series(np.linspace(2, 1, 50))).dropna().lt(0).all()
