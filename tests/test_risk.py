import numpy as np
import pandas as pd

from trend_bot import risk


def m(values):
    return pd.Series(values, index=pd.date_range("2010-01-31", periods=len(values), freq="ME"))


def test_underwater_counts_the_longest_stretch_including_an_open_slump():
    r = m([0.1, -0.2, 0.05, 0.05, 0.2, 0.01, -0.1, -0.05])        # under for 3 months, then 2 at the end
    u = risk.underwater(r)
    assert u["months"] == 3 and not u["still_under"] and u["now"] == 2
    still = risk.underwater(m([0.1, -0.3, 0.01, 0.01, 0.01, 0.01]))
    assert still == {"months": 5, "still_under": True, "now": 5}


def test_worst_months_and_without_best():
    r = m(list(np.linspace(-0.10, 0.10, 40)))
    assert risk.worst_months(r) == np.mean(sorted(r)[:2])            # 5% of 40 = 2 months
    assert risk.without_best(r) < risk.summary(r)["yearly"]
    lucky = m([0.0] * 55 + [0.5] * 5)                               # all the growth in 5 months
    assert abs(risk.without_best(lucky)) < 1e-12


def test_words_are_plain():
    s = risk.summary(m([0.1, -0.3, 0.01, 0.01, 0.01, 0.01] + [0.02] * 30))
    text = " ".join(risk.words(s, "The portfolio"))
    assert "The portfolio once spent" in text and "worst months" in text and "best months" in text
    assert risk.span(27) == "2 years 3 months" and risk.span(12) == "1 year" and risk.span(1) == "1 month"
