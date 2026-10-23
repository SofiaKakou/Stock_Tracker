import numpy as np
import pandas as pd

from trend_bot import db, model


def raw(closes, adj=None):
    idx = pd.bdate_range("2024-01-01", periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    a = pd.Series(adj if adj is not None else closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "adj_close": a, "volume": 1000.0}, index=idx)


def test_zero_negative_and_missing_prices_are_missing_days():
    out = db._adjust(raw([10, 0, 11, -1, 12, np.nan, 13], adj=[10, 0, 11, 5, 12, 12, 0]))
    assert list(out["Close"]) == [10, 11, 12]                     # 0, negative, missing close, zero adj: gone
    assert (out["Close"] > 0).all() and np.isfinite(out["Close"].pct_change().dropna()).all()


def test_a_one_day_blip_is_dropped_but_a_lasting_move_is_kept():
    blip = db._adjust(raw([10, 10, 95, 10, 10]))
    assert 95 not in list(blip["Close"]) and len(blip) == 4
    lasting = db._adjust(raw([10, 10, 95, 96, 97]))
    assert 95 in list(lasting["Close"])


def test_unexplained_jumps_unless_a_split_is_nearby():
    c = pd.Series([10, 10, 50, 51, 52, 52, 10], index=pd.bdate_range("2024-03-01", periods=7), dtype=float)
    j = model.unexplained_jumps(c)
    assert list(j[j].index.strftime("%Y-%m-%d")) == ["2024-03-05", "2024-03-11"]
    with_split = model.unexplained_jumps(c, pd.DatetimeIndex(["2024-03-04"]))
    assert list(with_split[with_split].index.strftime("%Y-%m-%d")) == ["2024-03-11"]


def test_panel_flags_jump_months(tmp_path):
    con = db.connect(tmp_path / "m.db")
    days = pd.bdate_range("2024-01-01", periods=120)
    rows = []
    for t, jump_day in (("AAA", 70), ("BBB", None)):
        for i, d in enumerate(days):
            price = 10.0 * (5.0 if jump_day and i >= jump_day else 1.0)
            rows.append((t, d.strftime("%Y-%m-%d"), price, price, price, price, price, 1e6))
    con.executemany("INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.executemany("INSERT INTO tickers(ticker, last_date) VALUES (?, ?)", [("AAA", "2024-06-14"), ("BBB", "2024-06-14")])
    panel = model.monthly_panel(con)
    jump = panel["jump"]
    month = (days[70] + pd.offsets.MonthEnd(0))
    assert jump.loc[month, "AAA"] and jump["AAA"].sum() == 1 and not jump["BBB"].any()
