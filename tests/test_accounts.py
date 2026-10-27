import datetime as dt

import pandas as pd

from trend_bot import accounts, db


class FakeAccount:
    def __init__(self, values, start="2026-10-19"):
        days = pd.bdate_range(start, periods=len(values))
        self.body = {"timestamp": [int(d.timestamp()) for d in days], "equity": values}

    def _call(self, method, path, params=None):
        assert path == "/account/portfolio/history" and params["timeframe"] == "1D"
        return self.body


def test_history_chart_and_weekly_summary(tmp_path):
    con = db.connect(tmp_path / "m.db")
    con.executemany("INSERT INTO prices(ticker, date, close, adj_close) VALUES ('SPY', ?, ?, ?)",
                    [(f"2026-10-{d}", 500 + d, 500 + d) for d in (19, 20, 21, 22, 23)])
    monthly = FakeAccount([0, 100_000, 101_000, 100_500, 102_000])       # 0: before the account was funded
    day = FakeAccount([100_000, 99_500, 100_200, 100_900, 101_100])
    hist = accounts.collect(con, monthly, day, start="2026-10-19")
    assert len(hist["monthly"]["values"]) == 4 and hist["day"]["values"][-1] == 101_100
    assert accounts.load(con) == hist
    g = accounts.growth(hist)
    assert round(g["monthly"].dropna().iloc[-1], 4) == 0.02 and round(g["spy"].iloc[-1], 4) == round(523 / 519 - 1, 4)
    assert accounts.chart_png(hist)[:4] == b"\x89PNG"
    svg = accounts.chart_svg(hist)
    assert "<svg" in svg and "Monthly portfolio (demo): +2.0%" in svg
    state = {"days": {"2026-10-20": {"breakout": {"pnl": -500, "trades": 3}},
                      "2026-10-23": {"breakout": {"pnl": 200, "trades": 2}, "gap": {"pnl": 50, "trades": 1}},
                      "2026-10-16": {"gap": {"pnl": 999, "trades": 9}}}}            # last week: not counted
    e = accounts.weekly(hist, state, dt.date(2026, 10, 23), live="📈 live line")
    d = e["description"]
    assert "Day trading (demo)**: this week **+1.10%**" in d and "breakout: $-300 over 5 trades" in d
    assert "gap: $+50 over 1 trades" in d and "999" not in d and "live line" in d
    assert e["image"]["url"] == "attachment://accounts.png"


def test_one_account_failing_doesnt_stop_the_other(tmp_path):
    class Broken:
        def _call(self, *a, **k):
            raise RuntimeError("401")
    con = db.connect(tmp_path / "m.db")
    hist = accounts.collect(con, Broken(), FakeAccount([100_000, 100_100]), start="2026-10-19")
    assert "monthly" not in hist and "day" in hist
    assert accounts.chart_svg({}) == "" and accounts.chart_png({}) is None


def test_the_100_dollar_account_is_in_the_history_and_the_week(tmp_path):
    con = db.connect(tmp_path / "m.db")
    small = FakeAccount([0, 0, 0, 0, 100.0, 101.5], start="2026-10-20")      # funded on the Monday
    hist = accounts.collect(con, None, None, start="2026-10-19", small=small)
    assert hist["small"]["values"] == [100.0, 101.5]
    assert "$100 account (demo): +1.5%" in accounts.chart_svg(hist)
    assert "$100 account (demo)**: this week **+1.50%**" in accounts.weekly(hist, {}, dt.date(2026, 10, 27))["description"]
