import numpy as np
import pandas as pd

from trend_bot import uncertainty


def months(n=180):
    return pd.date_range("2010-01-31", periods=n, freq="ME")


def test_ranges_cover_the_value_and_widen_with_noise():
    rng = np.random.default_rng(0)
    calm = pd.Series(0.008 + rng.normal(0, 0.01, 180), index=months())
    wild = pd.Series(0.008 + rng.normal(0, 0.08, 180), index=months())
    c, w = uncertainty.ranges(calm), uncertainty.ranges(wild)
    assert c["lo"] < c["value"] < c["hi"] and w["lo"] < w["value"] < w["hi"]
    assert (w["hi"] - w["lo"]) > 3 * (c["hi"] - c["lo"])


def test_gap_words_tell_clear_from_unclear():
    rng = np.random.default_rng(1)
    market = pd.Series(rng.normal(0.008, 0.04, 180), index=months())
    clearly_better = market + 0.01
    wiggle = rng.normal(0, 0.01, 180)
    about_same = market + (wiggle - wiggle.mean())                     # same on average, a bit different each month
    r = uncertainty.ranges(clearly_better, market)
    assert r["gap_lo"] > 0 and r["gap_positive"] == 1.0
    assert "probably really better" in uncertainty.gap_words(r)
    r = uncertainty.ranges(about_same, market)
    assert r["gap_lo"] < 0 < r["gap_hi"] and "can't tell" in uncertainty.gap_words(r)
    assert "probably really worse" in uncertainty.gap_words(uncertainty.ranges(market - 0.01, market))
    assert uncertainty.ranges(market.head(10)) == {}


def test_q_values_raise_the_bar_when_many_ideas_are_tested():
    p = {"a": 0.001, "b": 0.02, "c": 0.04, "d": 0.5}
    q = uncertainty.q_values(p)
    assert q["a"] == 0.004 and abs(q["b"] - 0.04) < 1e-12 and abs(q["c"] - 0.04 * 4 / 3) < 1e-12 and q["d"] == 0.5
    assert all(q[k] >= p[k] for k in p)                       # never easier than the raw chance
    assert uncertainty.p_from_t(3.0) < 0.002 and abs(uncertainty.p_from_t(0.0) - 0.5) < 1e-12
    assert uncertainty.q_values({}) == {}


def test_beats_t_compares_two_rankings_month_by_month():
    rng = np.random.default_rng(0)
    rows = []
    for m in months(48):
        ret = rng.normal(0, 0.05, 200)
        rows.append(pd.DataFrame({"month": m, "ret": ret, "good": ret + rng.normal(0, 0.05, 200),
                                  "meh": ret + rng.normal(0, 0.5, 200)}))
    df = pd.concat(rows, ignore_index=True)
    assert uncertainty.beats_t(df, "good", "meh") > 5
    assert uncertainty.beats_t(df, "meh", "good") < -5
