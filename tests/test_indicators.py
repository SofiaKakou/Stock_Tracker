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


def test_cache_expires(tmp_path, monkeypatch):
    import os
    import sys
    import types

    from conftest import make_prices
    from trend_bot import data

    monkeypatch.setattr(data, "CACHE_DIR", tmp_path)
    calls = []

    def fake_download(*a, **k):
        calls.append(1)
        return make_prices(np.linspace(1, 2, 30))

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=fake_download))
    data.load_yahoo("abc", period="1y")
    data.load_yahoo("abc", period="1y")
    assert len(calls) == 1  # second call served from cache
    cached = tmp_path / "ABC_1y.csv"
    old = cached.stat().st_mtime - 2 * 3600
    os.utime(cached, (old, old))
    data.load_yahoo("abc", period="1y")
    assert len(calls) == 2  # stale cache re-downloads
