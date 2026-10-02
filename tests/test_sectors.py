import json

import numpy as np
import pandas as pd
import pytest

from trend_bot import db, factors, ideas, sectors


@pytest.mark.parametrize("sic,name", [(2834, "Health care"), (7372, "Technology"), (6022, "Financials"),
                                      (6798, "Real estate"), (1311, "Energy"), (4911, "Utilities"),
                                      (5331, "Retail"), (3714, "Consumer goods"), (3560, "Industrials"),
                                      (None, None), (9999, "Other")])
def test_sic_to_industry(sic, name):
    assert sectors.sector(sic) == name


def two_industries(n=40):
    """Banks all have high book-to-market, tech all low: across the market every bank looks 'cheap'."""
    rows = []
    for i in range(n):
        rows.append({"month": pd.Timestamp("2026-09-30"), "ticker": f"B{i}", "sector": "Financials",
                     "book_to_market": 2.0 + i / 100})
        rows.append({"month": pd.Timestamp("2026-09-30"), "ticker": f"T{i}", "sector": "Technology",
                     "book_to_market": 0.1 + i / 1000})
    rows.append({"month": pd.Timestamp("2026-09-30"), "ticker": "X", "sector": None, "book_to_market": 1.0})
    return pd.DataFrame(rows)


def test_group_rank_compares_with_peers_and_falls_back_to_the_market():
    df = two_industries()
    market = factors.group_rank(df, "book_to_market")
    within = factors.group_rank(df, "book_to_market", by_sector=True)
    t39, b0 = df.index[df["ticker"] == "T39"][0], df.index[df["ticker"] == "B0"][0]
    assert market[t39] < market[b0]              # vs the market, every bank is cheaper than any tech stock
    assert within[t39] == pytest.approx(1.0)     # but T39 is the cheapest tech stock
    assert within[b0] == pytest.approx(1 / 40)   # and B0 the most expensive bank
    x = df.index[df["ticker"] == "X"][0]
    assert within[x] == market[x]                # unknown industry: ranked against the whole market
    small = df[df["ticker"].isin([f"B{i}" for i in range(5)]) | df["ticker"].str.startswith("T")]
    w = factors.group_rank(small, "book_to_market", by_sector=True)
    m = factors.group_rank(small, "book_to_market")
    banks = small["sector"] == "Financials"
    assert (w[banks] == m[banks]).all()          # only 5 banks: too few peers, market rank used


def card(simple, industry):
    """A study-ml-like scorecard with mean ICs per half: (before, after)."""
    mk = lambda i: pd.DataFrame({"mean_ic": [simple[i], industry[i]], "months": [60, 60], "ic_t": [3.0, 3.0],
                                 "ic_positive": [0.6, 0.6], "top10_per_year": [0.15, 0.16],
                                 "average_stock_per_year": [0.12, 0.12]}, index=["simple_mix", "industry_mix"])
    return {"all": mk(2), "before 2018": mk(0), "2018 on": mk(1)}


def test_industry_version_only_used_if_better_in_both_halves(tmp_path):
    con = db.connect(tmp_path / "m.db")
    preds = pd.DataFrame({"month": pd.to_datetime(["2011-01-31", "2026-08-31"])})
    ideas.save_scorecard(con, card((0.02, 0.03, 0.025), (0.01, 0.05, 0.03)), preds)
    assert ideas.method(con) == "simple_mix"     # better overall, but worse in the first half
    ideas.save_scorecard(con, card((0.02, 0.03, 0.025), (0.03, 0.04, 0.035)), preds)
    assert ideas.method(con) == "industry_mix"
    assert "within each industry" in ideas.reliability_note(con)
    assert json.loads(db.get_meta(con, "mix_scorecard"))["other_ic"] == pytest.approx(0.025)


def test_factor_study_shows_the_industry_comparison():
    rng = np.random.default_rng(0)
    rows = []
    for m in pd.date_range("2020-01-31", periods=12, freq="ME"):
        for i in range(120):
            sec = ["Financials", "Technology", "Energy"][i % 3]
            bm = rng.normal() + (3 if sec == "Financials" else 0)
            rows.append({"month": m, "ticker": f"S{i}", "sector": sec, "book_to_market": bm,
                         "ret": 0.01 * (bm - (3 if sec == "Financials" else 0)) + rng.normal(0, 0.02)})
    df = pd.DataFrame(rows)
    out = factors.study(df, split_year=2020)
    t = out[factors.INDUSTRY_TABLE]
    # Returns follow cheapness *within* the industry, so the within-industry ranking does better.
    assert t.loc["book_to_market", "ic_within_industry"] > t.loc["book_to_market", "ic_whole_market"] > 0


def card4(rows):
    """{method: (before, after, all)} -> a study-ml-like scorecard."""
    mk = lambda i: pd.DataFrame({"mean_ic": [v[i] for v in rows.values()], "months": 60, "ic_t": 3.0,
                                 "ic_positive": 0.6, "top10_per_year": 0.15, "average_stock_per_year": 0.12},
                                index=list(rows))
    return {"all": mk(2), "before 2018": mk(0), "2018 on": mk(1)}


def test_extra_signals_kept_only_if_better_in_both_halves(tmp_path):
    con = db.connect(tmp_path / "m.db")
    preds = pd.DataFrame({"month": pd.to_datetime(["2011-01-31", "2026-08-31"])})
    base = {"simple_mix": (0.02, 0.03, 0.025), "industry_mix": (0.01, 0.02, 0.015)}
    ideas.save_scorecard(con, card4(base | {"simple_mix_plus": (0.03, 0.025, 0.03),
                                            "industry_mix_plus": (0.0, 0.0, 0.0)}), preds)
    assert ideas.method(con) == "simple_mix" and not ideas.extra(con)   # worse in the second half
    ideas.save_scorecard(con, card4(base | {"simple_mix_plus": (0.03, 0.04, 0.035),
                                            "industry_mix_plus": (0.0, 0.0, 0.0)}), preds)
    assert ideas.extra(con) and "52-week high" in ideas.reliability_note(con)
    assert json.loads(db.get_meta(con, "mix_scorecard"))["mean_ic"] == pytest.approx(0.035)  # the version used


def test_mix_plus_rewards_calm_stocks_near_their_high():
    from trend_bot import ml

    rng = np.random.default_rng(0)
    df = pd.DataFrame(rng.normal(size=(100, len(ml.FEATURES))), columns=ml.FEATURES)
    df["month"] = pd.Timestamp("2026-09-30")
    df.loc[0, "vol"], df.loc[0, "high52"] = -9, 9      # calmest, closest to its high
    df.loc[1, "vol"], df.loc[1, "high52"] = 9, -9
    plain, plus = ml.simple_mix(df), ml.simple_mix(df, extra=True)
    assert plus[0] - plain[0] > 0 > plus[1] - plain[1]
    r = ideas.oriented_ranks(df, extra=True)
    assert r.loc[0, "vol"] == r["vol"].max() and r.loc[0, "high52"] == 1.0
    assert ideas.reasons(pd.Series({"vol": 0.95, "high52": 0.9})) == "calm stock, near its 52-week high"
