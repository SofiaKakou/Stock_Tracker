import json

import numpy as np
import pandas as pd
import pytest

from test_fundamentals import bulk_zip  # noqa: F401
from test_model import DATES, N_STOCKS, model_db  # noqa: F401
from trend_bot import earnings, factors, fundamentals, market_data


def facts_frame(rows):
    df = pd.DataFrame(rows, columns=["cik", "item", "priority", "start", "end", "filed", "val"])
    for c in ("start", "end", "filed"):
        df[c] = pd.to_datetime(df[c])
    df["days"] = (df["end"] - df["start"]).dt.days
    df["val"] = df["val"].astype(float)
    return df


def year_rows(cik, y, quarters, item="net_income", fy_filed=None):
    """Three 10-Q quarters, the 9-month YTD, and the 10-K year for fiscal year y."""
    q_ends = [f"{y}-03-31", f"{y}-06-30", f"{y}-09-30"]
    q_starts = [f"{y}-01-01", f"{y}-04-01", f"{y}-07-01"]
    rows = [(cik, item, 0, s, e, f"{e[:4]}-{int(e[5:7]) + 1:02d}-10", v)
            for s, e, v in zip(q_starts, q_ends, quarters[:3])]
    rows.append((cik, item, 0, f"{y}-01-01", f"{y}-09-30", f"{y}-10-10", sum(quarters[:3])))
    rows.append((cik, item, 0, f"{y}-01-01", f"{y}-12-31", fy_filed or f"{y + 1}-02-15", sum(quarters)))
    return rows


def test_fourth_quarter_is_year_minus_nine_months():
    f = facts_frame(year_rows(1, 2020, [10, 20, 30, 45]))
    q = earnings.quarterly_series(f, "net_income")
    assert list(q["val"]) == [10, 20, 30, 45]
    assert q["filed"].iloc[-1] == pd.Timestamp("2021-02-15")  # Q4 becomes known with the 10-K


def test_first_reported_value_is_used():
    rows = year_rows(1, 2020, [10, 20, 30, 40])
    rows.append((1, "net_income", 0, "2020-01-01", "2020-03-31", "2021-05-10", 999))  # later restatement
    q = earnings.quarterly_series(facts_frame(rows), "net_income")
    assert q["val"].iloc[0] == 10


def test_surprise_spikes_on_an_unusual_quarter():
    rows = []
    for i, y in enumerate(range(2015, 2021)):
        base = 100 + 4 * i
        quarters = [base, base + 1, base + 2, base + 3]
        if y == 2020:
            quarters[2] = base + 60  # a big Q3 beat
        rows += year_rows(1, y, quarters)
    rng = np.random.default_rng(0)
    f = facts_frame(rows)
    f.loc[f["days"].between(80, 100), "val"] += rng.normal(0, 0.5, f["days"].between(80, 100).sum())
    s = earnings.surprises(earnings.quarterly_series(f, "net_income"))
    beat = s["end"] == pd.Timestamp("2020-09-30")
    q3 = s.loc[beat, "sue"].iloc[0]
    assert q3 > 5 and s.loc[~beat, "sue"].abs().max() < q3
    assert s["end"].min() > pd.Timestamp("2016-12-31")  # needs a year-earlier quarter and 4 past surprises


def test_announcement_return_vs_benchmark(model_db):
    tickers = {i + 1: f"S{i:02d}" for i in range(N_STOCKS)}
    ev = pd.DataFrame({"cik": [80, 1], "filed": [DATES[400], DATES[400]]})
    ear = earnings.announcement_returns(model_db, ev, tickers)
    s79 = model_db.execute("SELECT close FROM prices WHERE ticker='S79' ORDER BY date").fetchall()
    spy = model_db.execute("SELECT close FROM prices WHERE ticker='SPY' ORDER BY date").fetchall()
    expected = (s79[401][0] / s79[399][0] - 1) - (spy[401][0] / spy[399][0] - 1)
    assert ear.iloc[0] == pytest.approx(expected)


def test_factor_study_includes_earnings(model_db, tmp_path, monkeypatch, capsys):
    from trend_bot.cli import main

    years = list(range(DATES[0].year - 1, DATES[-1].year))
    data = bulk_zip(years)
    monkeypatch.setattr(fundamentals.insiders, "CACHE_DIR", tmp_path / "sec")
    fundamentals.update_fundamentals(model_db, download=lambda p: (p.parent.mkdir(parents=True, exist_ok=True),
                                                                   p.write_bytes(data), p)[-1],
                                     log=lambda *_: None)
    df = factors.monthly_factors(model_db, since=str(DATES[260].date()), log=lambda *_: None)
    assert {"sue", "revenue_sue", "ear"} <= set(df.columns)
    assert df["ear"].notna().any()  # every 10-K filing is an event with a measurable reaction
    res = factors.study(df, split_year=int(df["month"].dt.year.max()))
    assert "earnings_combo" in res["all"].index
    assert main(["study", "factors", "--db", str(tmp_path / "market.db"), "--since", str(DATES[260].date())]) == 0
    assert "earnings_combo" in capsys.readouterr().out
