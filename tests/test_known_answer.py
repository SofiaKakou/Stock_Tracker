"""Known-answer tests: made-up markets where the right result is known in advance.

If the pipeline finds exactly the rule that was planted (no more, no less), the plumbing between
the steps is right: signals pointed the right way, holdings chosen and costs paid as described, and
each return credited to the right month.
"""

import numpy as np
import pandas as pd
import pytest

from test_lookahead import _with_financials
from test_model import model_db  # noqa: F401
from trend_bot import ideas, ml

N, MONTHS, HOLD = 90, 24, 20


def planted(signal: str, high_is_good: bool) -> pd.DataFrame:
    """Every stock alike on every measure except `signal`; the 20 best on it rise exactly 1% next
    month, every other stock exactly 0%."""
    rows = []
    for m in pd.date_range("2019-01-31", periods=MONTHS, freq="ME"):
        f = pd.DataFrame({c: 1.0 for c in ml.FEATURES}, index=range(N))
        f[signal] = np.arange(N, dtype=float)
        best = f[signal] >= N - HOLD if high_is_good else f[signal] < HOLD
        f["ret"] = np.where(best, 0.01, 0.0) if len(rows) < MONTHS - 1 else np.nan   # the last: not known yet
        f["month"], f["ticker"], f["close"], f["sector"] = m, [f"T{i:02d}" for i in range(N)], 50.0, "Technology"
        f["gone"] = pd.Series([None] * N, dtype=object)
        rows.append(f)
    return pd.concat(rows, ignore_index=True)


@pytest.mark.parametrize("signal, high_is_good", [("book_to_market", True),    # cheap stocks: high is good
                                                  ("asset_growth", False)])     # fast-growing assets: low is good
def test_the_backtest_finds_exactly_the_planted_rule(signal, high_is_good):
    df = planted(signal, high_is_good)
    days = pd.bdate_range("2018-01-01", "2021-03-31")
    spy = pd.Series(np.linspace(100, 200, len(days)), index=days)     # always rising: always invested
    m = ideas.backtest(df, spy, hold=HOLD, buffer=40, cost_bps=10)
    first, rest = m["mix_always"].iloc[0], m["mix_always"].iloc[1:]
    assert first == pytest.approx(0.01 - 0.002)          # buy all 20: 0.1% each way, paid once
    assert rest.to_numpy() == pytest.approx(np.full(len(rest), 0.01), abs=1e-12)   # then hold: no trades
    assert (m["mix_turnover"].iloc[1:] == 0).all()
    assert (m["mix"] == m["mix_always"]).all()
    assert m["average_stock"].to_numpy() == pytest.approx(np.full(len(m), 0.01 * HOLD / N))
    assert m.index[0] == pd.Timestamp("2019-02-28")       # January's picks earn February's return
    assert len(m) == MONTHS - 1                            # the last month has no next month yet


def test_the_database_step_computes_returns_and_momentum_from_the_prices(model_db, tmp_path, monkeypatch):  # noqa: F811
    con = _with_financials(model_db, tmp_path, monkeypatch)
    table = ml.dataset(con, since="2000-01-01", log=lambda *_: None)
    prices = pd.read_sql("SELECT ticker, date, adj_close FROM prices", con, parse_dates=["date"])
    wide = prices.pivot(index="date", columns="ticker", values="adj_close").sort_index()
    ends = wide.groupby(wide.index.to_period("M")).tail(1)          # each month's last trading day
    checked = momentum = 0
    for _, r in table.dropna(subset=["ret"]).sample(200, random_state=0).iterrows():
        i = ends.index.get_indexer([ends.index[ends.index.to_period("M") == r["month"].to_period("M")][0]])[0]
        expected = ends[r["ticker"]].iloc[i + 1] / ends[r["ticker"]].iloc[i] - 1
        assert r["ret"] == pytest.approx(expected, rel=1e-9), (r["ticker"], r["month"])
        day = wide.index.get_loc(ends.index[i])
        c = wide[r["ticker"]]
        if day >= 252 and pd.notna(r["mom12"]):
            assert r["mom12"] == pytest.approx(c.iloc[day - 21] / c.iloc[day - 252] - 1, rel=1e-9)
            momentum += 1
        checked += 1
    assert checked == 200 and momentum > 50


def test_next_morning_vs_close_on_known_prices():
    days = pd.bdate_range("2020-01-01", "2020-04-30")
    # Every close is 1% above that morning's open; prices rise 0.1% a day.
    close = pd.Series(100 * 1.001 ** np.arange(len(days)), index=days)
    prices = {"A": pd.DataFrame({"Open": close / 1.01, "Close": close})}
    record = [{"month": pd.Timestamp(m), "held": ["A"]} for m in ("2020-01-31", "2020-02-29", "2020-03-31")]
    m = ideas.next_morning(record, prices)
    assert list(m.index) == [pd.Timestamp("2020-02-29"), pd.Timestamp("2020-03-31")]
    feb = close.loc["2020-02"]
    assert m.loc["2020-02-29", "close"] == pytest.approx(feb.iloc[-1] / close.loc[:"2020-01-31"].iloc[-1] - 1)
    # Morning to morning covers the same number of trading days, so it's the same 0.1%/day growth.
    assert m.loc["2020-02-29", "morning"] == pytest.approx(1.001 ** len(feb) - 1)
