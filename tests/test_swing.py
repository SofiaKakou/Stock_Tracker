import numpy as np
import pandas as pd
import pytest

from trend_bot import db, swing


def make_db(tmp_path, days=60):
    con = db.connect(tmp_path / "m.db")
    dates = [str(d.date()) for d in pd.bdate_range("2015-06-01", periods=days)]
    rows = []
    for t, base in (("JUMP", 50.0), ("FLAT", 20.0), ("SPY", 400.0)):
        for i, d in enumerate(dates):
            o = c = base
            if t == "JUMP" and i == 30:      # opens 12% up and keeps running to +20% by the close
                o, c = base * 1.12, base * 1.20
            elif t == "JUMP" and i > 30:
                o = c = base * 1.20
            rows.append((t, d, o, max(o, c), min(o, c), c, c, 1e6))
    con.executemany("INSERT INTO prices(ticker, date, open, high, low, close, adj_close, volume) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.executemany("INSERT INTO tickers(ticker, last_date) VALUES (?, ?)", [(t, dates[-1]) for t in ("JUMP", "FLAT")])
    con.commit()
    return con, dates


def test_a_gap_up_is_found_and_measured_from_the_open(tmp_path):
    con, dates = make_db(tmp_path)
    df = swing._daily(con, ["JUMP", "FLAT"], "2000-01-01")
    df["date"] = pd.to_datetime(df["date"])
    tr = swing.mover_trades(df, 1)
    assert list(tr["ticker"]) == ["JUMP"] and tr["gap"].iloc[0] == pytest.approx(0.12)
    assert tr["ret"].iloc[0] == pytest.approx(1.20 / 1.12 - 1)          # open to close the same day
    five = swing.mover_trades(df, 5)
    assert five["ret"].iloc[0] == pytest.approx(1.20 / 1.12 - 1)         # it stayed there


def test_illiquid_or_cheap_stocks_are_left_out(tmp_path):
    con, _ = make_db(tmp_path)
    con.execute("UPDATE prices SET volume = 10 WHERE ticker = 'JUMP'")      # barely traded
    df = swing._daily(con, ["JUMP"], "2000-01-01")
    df["date"] = pd.to_datetime(df["date"])
    assert swing.mover_trades(df, 1).empty


def test_earnings_drift_buys_the_day_after_the_filing_and_measures_vs_the_market(tmp_path):
    con, dates = make_db(tmp_path)
    df = swing._daily(con, ["JUMP"], "2000-01-01")
    df["date"] = pd.to_datetime(df["date"])
    spy = pd.Series(400.0, index=pd.to_datetime(dates))
    ev = pd.DataFrame({"ticker": ["JUMP"], "filed": [pd.Timestamp(dates[28])], "sue": [3.0]})
    tr = swing.earnings_trades(df, ev, spy, hold=2)
    assert tr["date"].iloc[0] == pd.Timestamp(dates[29])                # the close of the day after
    assert tr["ret"].iloc[0] == pytest.approx(0.20)                      # day 29 close 50 -> day 31 close 60


def test_a_real_edge_passes_and_noise_does_not():
    rng = np.random.default_rng(0)
    dates = pd.Series(pd.bdate_range("2008-01-01", "2024-12-31")).sample(4000, replace=True, random_state=1)
    edge = swing.evaluate(pd.Series(rng.normal(0.01, 0.03, 4000)), dates.reset_index(drop=True), 0.001)
    assert swing.verdict(edge).startswith("PASSES")
    noise = swing.evaluate(pd.Series(rng.normal(0.0, 0.03, 4000)), dates.reset_index(drop=True), 0.001)
    assert not swing.verdict(noise).startswith("PASSES")


def test_the_whole_study_runs_and_says_what_passed(tmp_path):
    con, _ = make_db(tmp_path)
    out = swing.run(con, since="2000-01-01", log=lambda *_: None)
    assert "BIG MOVERS" in out and "Opened 10%-20% up" in out and "Passed:" in out
    assert "no company financials yet" in out and "BIG NEWS DAYS" in out


def fade_frame(days):
    """One stock, liquid, normal volume; `days`: list of (open, high, close, volume_x) from the gap day on."""
    rows = [("X", pd.Timestamp("2020-01-01") + pd.Timedelta(days=i), 100.0, 100.0, 100.0, 100.0, True, 50e6, 1.0)
            for i in range(3)]
    for i, (o, h, c, vx) in enumerate(days):
        rows.append(("X", pd.Timestamp("2020-01-04") + pd.Timedelta(days=i), o, h, c, None, True, 50e6, vx))
    df = pd.DataFrame(rows, columns=["ticker", "date", "o", "h", "c", "prev", "liquid", "dvol", "vol_x"])
    df["prev"] = df["c"].shift(1)
    return df


def test_shorting_a_gap_up_that_fades_wins():
    df = fade_frame([(120, 121, 110, 3.0)])                          # opened +20%, closed 110
    tr = swing.fade_trades(df, 0.10, 1, 0.10, 5e6, 0.30)
    assert len(tr) == 1 and tr["ret"].iloc[0] == pytest.approx(-(110 / 120 - 1))


def test_a_squeeze_hits_the_stop_and_an_overnight_gap_past_it_costs_more():
    df = fade_frame([(120, 140, 135, 3.0)])                          # ran to 140: stopped at 132
    tr = swing.fade_trades(df, 0.10, 1, 0.10, 5e6, 0.30)
    assert tr["ret"].iloc[0] == pytest.approx(-0.10 - swing.SHORT_SLIPPAGE)
    df = fade_frame([(120, 125, 125, 3.0), (150, 150, 150, 2.0), (150, 150, 150, 1.0)])   # opened at 150 next day
    tr = swing.fade_trades(df, 0.10, 3, 0.15, 5e6, 0.30)
    assert tr["ret"].iloc[0] == pytest.approx(-(150 / 120 - 1) - swing.SHORT_SLIPPAGE - 0.30 / 252 * 2)


def test_a_gap_on_normal_volume_is_treated_as_a_price_error():
    df = fade_frame([(120, 121, 110, 1.0)])                          # no extra volume: left out
    assert swing.fade_trades(df, 0.10, 1, 0.10, 5e6, 0.30).empty
    df = fade_frame([(120, 121, 110, 3.0)])
    assert swing.fade_trades(df, 0.10, 1, 0.10, 100e6, 0.30).empty   # not traded enough for this line


def test_after_a_big_news_day_it_buys_the_next_open_and_measures_vs_the_market():
    rows = [("X", pd.Timestamp("2020-01-01") + pd.Timedelta(days=i), 100.0, 100.0, 100.0, None, True, 50e6, 1.0)
            for i in range(3)]
    rows += [("X", pd.Timestamp("2020-01-04"), 101.0, 112.0, 110.0, None, True, 50e6, 4.0),   # +10% on 4x volume
             ("X", pd.Timestamp("2020-01-05"), 111.0, 115.0, 114.0, None, True, 50e6, 2.0),
             ("X", pd.Timestamp("2020-01-06"), 114.0, 120.0, 120.0, None, True, 50e6, 1.0)]
    df = pd.DataFrame(rows, columns=["ticker", "date", "o", "h", "c", "prev", "liquid", "dvol", "vol_x"])
    df["prev"] = df["c"].shift(1)
    spy = pd.DataFrame({"date": df["date"], "o": 400.0, "c": 400.0})
    spy.loc[spy["date"] == pd.Timestamp("2020-01-06"), "c"] = 404.0                 # the market +1% by then
    tr = swing.news_trades(df, spy, hold=2)
    assert len(tr) == 1 and tr["date"].iloc[0] == pd.Timestamp("2020-01-05")       # the next morning
    assert tr["ret"].iloc[0] == pytest.approx(120 / 111 - 1 - 0.01)
    df.loc[3, "vol_x"] = 1.0                                                          # no extra volume: no news
    assert swing.news_trades(df, spy, hold=2).empty
