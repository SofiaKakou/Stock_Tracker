import json

import numpy as np
import pandas as pd
import pytest

from trend_bot import alerts, db, market_data, model, sec_bulk
from trend_bot.cli import main

N_STOCKS, N_DAYS = 80, 800
DATES = pd.bdate_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=1), periods=N_DAYS)


def spy_path():
    # Rises, crashes for ~6 months in the middle, recovers.
    r = np.full(N_DAYS, 0.0006)
    r[350:480] = -0.004
    return 300 * np.exp(np.cumsum(r))


@pytest.fixture
def model_db(tmp_path):
    con = db.connect(tmp_path / "market.db")
    tick = [f"S{i:02d}" for i in range(N_STOCKS)]
    con.executemany("INSERT INTO tickers(ticker, cik, name, exchange) VALUES (?, ?, ?, ?)",
                    [(t, i + 1, f"{t} Corp", "NYSE") for i, t in enumerate(tick)] + [("SPY", None, "SPDR", "ETF")])
    rng = np.random.default_rng(3)
    drift = np.linspace(-0.001, 0.0015, N_STOCKS)  # S79 is the steadiest riser, S00 the worst

    def dl(tickers, start=None, period=None):
        out = {}
        for t in tickers:
            if t == "SPY":
                c = spy_path()
            else:
                i = int(t[1:])
                c = 50 * np.exp(np.cumsum(rng.normal(drift[i], 0.01 + 0.0002 * (N_STOCKS - i), N_DAYS)))
            out[t] = pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Adj Close": c,
                                   "Volume": 1e6}, index=DATES)
        return out

    market_data.update_prices(con, downloader=dl, pause=0, log=lambda *_: None)
    yield con
    con.close()


def test_rebalance_keeps_holdings_inside_the_buffer():
    ranked = pd.Series(range(100, 0, -1), index=[f"T{i}" for i in range(100)])
    new, buys, sells = model.rebalance(ranked, ["T5", "T30", "T60"], hold=5, buffer=40)
    assert new[:2] == ["T5", "T30"] and "T60" in sells  # T30 is ranked 31st, still inside the buffer
    assert buys == ["T0", "T1", "T2"] and len(new) == 5


def test_features_use_only_the_past():
    c = pd.Series(np.linspace(10, 20, 300), index=pd.bdate_range("2020-01-01", periods=300))
    f = model.daily_features(pd.DataFrame({"Close": c, "Volume": 1.0}))
    changed = c.copy()
    changed.iloc[-1] = 999  # changing today's price can't change yesterday's features
    g = model.daily_features(pd.DataFrame({"Close": changed, "Volume": 1.0}))
    pd.testing.assert_frame_equal(f.iloc[:-1], g.iloc[:-1])
    assert f["trend"].iloc[-1] == 1 and f["high52"].iloc[-1] == 1


def test_score_parts_and_insiders():
    feat = pd.DataFrame({"trend": [1, 0], "mom12": [0.5, -0.2], "mom6": [0.3, -0.1], "high52": [1, 0.6],
                         "vol": [0.01, 0.03]}, index=["GOOD", "BAD"])
    s = model.score(feat, pd.Series({"GOOD": 3}), pd.Series({"BAD": 4}))
    assert list(s.index) == ["GOOD", "BAD"] and s.loc["GOOD", "score"] == pytest.approx(100)
    assert s.loc["BAD", "insiders"] == 0 and s.loc["GOOD", "insiders"] == 1


def test_backtest_and_weather(model_db):
    monthly = model.backtest(model_db, hold=10, cash_rate=0)
    assert {"model", "model_no_weather", "SPY", "SPY_weather", "all_eligible"} <= set(monthly.columns)
    assert not monthly["invested"].all() and monthly["invested"].any()  # the crash turns the weather off
    cash = monthly[~monthly["invested"]]
    assert (cash["model"] == 0).all()
    # Scoring favours the steady risers, so the model beats the average eligible stock.
    s = model.summarize(monthly, ["model_no_weather", "all_eligible"])
    assert s.loc["model_no_weather", "cagr"] > s.loc["all_eligible", "cagr"]


def test_live_portfolio_rebalances_monthly(model_db):
    res = model.update_portfolio(model_db, hold=10)
    assert res["rebalanced"] and len(res["holdings"]) == 10 and len(res["buys"]) == 10 and not res["sells"]
    assert res["weather"] == "invest" and not res["weather_changed"]
    again = model.update_portfolio(model_db, hold=10)
    assert not again["rebalanced"] and list(again["holdings"].index) == list(res["holdings"].index)
    forced = model.update_portfolio(model_db, hold=10, force=True)
    assert forced["rebalanced"] and forced["buys"] == []  # same scores -> nothing to change
    e = alerts.model_embed(res)
    assert e["title"].startswith("📈") and "Buy (10)" in e["description"]
    assert alerts.model_embed(again) is None


def test_cli_model_study_alert_report(model_db, tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "market.db")
    assert main(["model", "--db", path, "--hold", "10"]) == 0
    out = capsys.readouterr().out
    assert "Market weather" in out and "Model portfolio (10 stocks" in out
    assert model_db.execute("SELECT COUNT(*) FROM picks WHERE signal = 'model_buy'").fetchone()[0] == 10

    assert main(["study", "model", "--db", path, "--hold", "10"]) == 0
    out = capsys.readouterr().out
    assert "Model + weather filter" in out and "2016-now (check 2)" in out

    assert main(["report", "--db", path, "--out", str(tmp_path / "r")]) == 0
    page = (tmp_path / "r" / "latest.html").read_text(encoding="utf-8")
    held = [r[0] for r in model_db.execute("SELECT ticker FROM model_holdings")]
    assert "Trend Score model portfolio" in page and all(f"<b>{t}</b>" in page for t in held)

    assert main(["track", "--db", path]) == 0
    assert "Model buy" in capsys.readouterr().out
