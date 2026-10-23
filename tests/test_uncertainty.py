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
