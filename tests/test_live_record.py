import numpy as np
import pytest

from test_model import DATES, model_db  # noqa: F401
from trend_bot import ideas
from trend_bot.db import load_prices


def test_live_record_chains_monthly_holdings_vs_spy(model_db):
    con = model_db
    assert ideas.live_record(con) is None and ideas.live_line(con) == ""
    d1, d2 = str(DATES[-60].date()), str(DATES[-30].date())
    first = [f"S{i}" for i in range(70, 75)]
    second = ["S70", "S71", "S72", "S78", "S79"]                     # 2 sold, 2 bought
    con.executemany("INSERT INTO ideas_periods VALUES (?, ?, ?)",
                    [(d1, t, 1) for t in first] + [(d2, t, int(t in ("S78", "S79"))) for t in second])
    con.commit()
    r = ideas.live_record(con)

    close = lambda t: load_prices(con, t)["Close"]
    ret = lambda t, a, b=None: close(t).loc[:b].iloc[-1] / close(t).loc[a:].iloc[0] - 1
    p1 = np.mean([ret(t, d1, d2) for t in first])
    p2 = np.mean([ret(t, d2) for t in second])
    expected = (1 + p1) * (1 - 0.001 * 1.0) * (1 + p2) * (1 - 0.002 * 0.4) - 1
    assert r["portfolio"] == pytest.approx(expected)
    assert r["spy"] == pytest.approx(ret("SPY", d1))
    assert r["start"] == d1 and r["updates"] == 2 and r["days"] == (DATES[-1] - DATES[-60]).days
    line = ideas.live_line(con)
    assert f"Since {d1}" in line and "vs S&P 500" in line and "after trading costs" in line


def test_update_portfolio_records_each_month(tmp_path):
    from test_ideas import ranking
    from trend_bot import db

    con = db.connect(tmp_path / "m.db")
    names = [f"S{i:02d}" for i in range(60)]
    ideas.update_portfolio(con, ranking(names), "2026-09-30", {"invest": True}, hold=5, buffer=10)
    rows = con.execute("SELECT start, ticker, bought FROM ideas_periods ORDER BY ticker").fetchall()
    assert rows == [("2026-09-30", t, 1) for t in names[:5]]
