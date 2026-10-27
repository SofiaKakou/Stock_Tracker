import numpy as np
import pandas as pd
import pytest

from trend_bot import crypto


def coin(sym, closes, start="2023-06-30T20:00:00Z"):
    t = pd.date_range(start, periods=len(closes), freq="15min")
    closes = np.asarray(closes, float)
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({"symbol": sym, "t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": opens,
                         "high": np.maximum(opens, closes), "low": np.minimum(opens, closes), "close": closes,
                         "volume": 1.0})


def test_a_dip_is_bought_at_the_next_open_and_sold_later():
    closes = [100.0] * 5 + [97.0] + [97.0, 98.0, 99.0, 100.0, 100.0, 100.0]    # -3% in an hour at bar 5
    g = coin("X/USD", closes)
    sig = crypto.signals(g)
    assert sig["dip"][5] and not sig["big dip"][5] and not sig["momentum"][:9].any()   # the bounce back is +3%: momentum later
    tr = crypto.trades(g, sig["dip"], hold=4)
    assert len(tr) == 1 and tr["ret"].iloc[0] == pytest.approx(g["open"][10] / g["open"][6] - 1)


def test_one_position_at_a_time():
    closes = [100.0] * 5 + [97.0, 94.0, 91.0, 88.0] + [88.0] * 10              # signals on several bars in a row
    g = coin("X/USD", closes)
    tr = crypto.trades(g, crypto.signals(g)["dip"], hold=4)
    assert len(tr) == 2                                                          # the next one only after the exit


def test_a_breakout_needs_a_new_24_hour_high():
    closes = [100.0] * 100 + [101.0]
    g = coin("X/USD", closes)
    assert crypto.signals(g)["breakout"][100] and not crypto.signals(g)["breakout"][99]


def test_a_planted_bounce_passes_and_the_report_reads_like_the_others():
    rng = np.random.default_rng(0)
    frames = []
    for k, sym in enumerate(["AAA/USD", "BBB/USD"]):
        r = rng.normal(0, 0.004, 40_000)
        r[200::150] = -0.03                       # a sharp drop every so often ...
        r[201::150] = 0.025                       # ... that bounces straight back
        frames.append(coin(sym, 100 * np.exp(np.cumsum(r)), start="2022-12-01T00:00:00Z"))
    res, hold = crypto.study(pd.concat(frames, ignore_index=True))
    assert crypto.passes(res["dip, held 15 minutes"]) and not crypto.passes(res["momentum, held 1 day"])
    txt = crypto.text(res, hold, "2022-01-01", "2023-02-21")
    assert "Fell 2%+ in the last hour" in txt and "Passed:" in txt and "Buy and hold" in txt
