"""Does the money add up? Bookkeeping checks on the portfolio and the headline numbers.

Ideas from passivbot (independent reference implementations, parity tests) and from what
OpenTrader lacks (costs, mark-to-market, a backtest that matches live).
"""

import math

import numpy as np
import pandas as pd

from test_ideas import panel_with_edge
from trend_bot import db, ideas, model, risk


# --- Slow, simple reference versions, written separately from the real code ---------------------

def ref_yearly(returns: list[float]) -> float:
    value = 1.0
    for r in returns:
        value *= 1.0 + r
    return value ** (12.0 / len(returns)) - 1.0


def ref_worst_drop(returns: list[float]) -> float:
    value, peak, worst = 1.0, 1.0, 0.0
    for r in returns:
        value *= 1.0 + r
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def ref_longest_under(returns: list[float]) -> int:
    value, peak, run, longest = 1.0, 1.0, 0, 0
    for r in returns:
        value *= 1.0 + r
        if value >= peak:
            peak, run = value, 0
        else:
            run += 1
            longest = max(longest, run)
    return longest


def ref_worst_months(returns: list[float], share: float = 0.05) -> float:
    xs = sorted(returns)
    k = max(1, int(round(len(xs) * share)))
    return sum(xs[:k]) / k


def series(seed: int, n: int = 150) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(0.009, 0.05, n), index=pd.date_range("2010-01-31", periods=n, freq="ME"))


def test_headline_numbers_match_the_slow_reference_versions():
    for seed in range(20):
        r = series(seed)
        xs = [float(v) for v in r]
        s = model.summarize(r.to_frame("p"), ["p"]).loc["p"]
        assert math.isclose(s["cagr"], ref_yearly(xs), rel_tol=1e-9, abs_tol=1e-12)
        assert math.isclose(s["max_drawdown"], ref_worst_drop(xs), rel_tol=1e-9, abs_tol=1e-12)
        assert risk.underwater(r)["months"] == ref_longest_under(xs)
        assert math.isclose(risk.worst_months(r), ref_worst_months(xs), rel_tol=1e-12)


# --- The portfolio you'd follow is the portfolio that was tested --------------------------------

def test_paper_portfolio_matches_the_historical_test_month_by_month(tmp_path, monkeypatch):
    monkeypatch.setattr(ideas, "ALWAYS_CAP", False)   # the tested (uncapped) rule; the cap is tested in test_ideas
    df = panel_with_edge(months=14, n=150)
    record = []
    ideas._hold_returns(df.dropna(subset=["ret"]).reset_index(drop=True), ideas.scores(df.dropna(subset=["ret"])
                        .reset_index(drop=True)), 20, 40, 0.002, record=record)
    tested = {r["month"]: set(r["held"]) for r in record}

    con = db.connect(tmp_path / "m.db")
    live = {}
    for month in sorted(df["month"].unique()):
        ranked = ideas.rank_month(df[df["month"] <= month])
        port = ideas.update_portfolio(con, ranked, f"{pd.Timestamp(month):%Y-%m-%d}", {"invest": True},
                                      hold=20, buffer=40)
        live[pd.Timestamp(month)] = {p["ticker"] for p in port["buys"] + port["holds"]}
    common = sorted(set(tested) & set(live))
    assert len(common) >= 12
    for m in common:
        assert tested[m] == live[m], f"{m:%Y-%m}: the followed portfolio differs from the tested one"


def test_every_trade_pays_its_cost():
    df = panel_with_edge(months=12, n=150).dropna(subset=["ret"]).reset_index(drop=True)
    score = ideas.scores(df)
    record = []
    free = ideas._hold_returns(df, score, 20, 40, 0.0, record=record)
    cost = 0.002
    paid = ideas._hold_returns(df, score, 20, 40, cost)
    buys = pd.Series({r["month"] + pd.offsets.MonthEnd(1): len(r["buys"]) for r in record})
    assert buys.iloc[0] == 20                                         # buying in: every stock pays
    assert np.allclose((free - paid) * 20 / cost, buys.reindex(free.index))
    assert (free - paid >= 0).all()                                   # costs never help


def test_a_ranking_with_no_information_does_not_beat_the_average_stock_after_costs():
    df = panel_with_edge(months=24, n=150, seed=5).dropna(subset=["ret"]).reset_index(drop=True)
    df["ret"] = np.random.default_rng(9).normal(0.01, 0.05, len(df))  # returns unrelated to every signal
    noise = pd.Series(np.random.default_rng(3).random(len(df)), index=df.index)
    r = ideas._hold_returns(df, noise, 20, 40, 0.002)
    average = df.groupby("month")["ret"].mean()
    average.index = average.index + pd.offsets.MonthEnd(1)
    gap = (r - average.reindex(r.index)).mean()
    assert gap < 0.01                                                 # no free lunch from bookkeeping
