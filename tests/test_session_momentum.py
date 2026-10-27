import numpy as np
import pandas as pd
import pytest

from trend_bot import session_momentum as sm


def day(date, path, sym="QQQ"):
    """78 five-minute bars; path: 78 closes. Each bar opens at the previous close, high/low 0.05 around."""
    rows, prev = [], path[0]
    for slot, c in enumerate(path):
        o = prev if slot else path[0] - (0.5 if path[0] > 100 else -0.5)
        rows.append((sym, date, slot, o, max(o, c) + 0.05, min(o, c) - 0.05, c))
        prev = c
    return rows


def frame(*days):
    rows = [r for d in days for r in d]
    return pd.DataFrame(rows, columns=["symbol", "date", "slot", "open", "high", "low", "close"])


FLAT = [100.0] * 78


def test_a_strong_first_bar_buys_and_a_trend_runs_to_the_close():
    up = [101.0] + [101.0 + 0.1 * k for k in range(1, 78)]
    df = frame(day("2024-01-02", FLAT), day("2024-01-03", up))
    t = sm.trades(df, "as_shown").set_index("date").loc["2024-01-03"]
    assert t["side"] == 1 and t["ret"] == pytest.approx(up[-1] / up[0] - 1)    # in at 9:35 (101), out at the close


def test_the_ema_exit_takes_the_next_open_after_a_close_below_it():
    path = [101.0, 101.5, 102.0, 102.5, 99.0] + [99.0] * 73          # rises, then one bar closes far below the EMA
    df = frame(day("2024-01-02", FLAT), day("2024-01-03", path))
    em = sm.trades(df, "ema_only").set_index("date").loc["2024-01-03"]
    assert em["ret"] == pytest.approx(99.0 / 101.0 - 1)               # out at the next bar's open (99)
    held = sm.trades(df, "to_close").set_index("date").loc["2024-01-03"]
    assert held["ret"] == pytest.approx(99.0 / 101.0 - 1)


def test_the_first_bars_stop_is_hit_at_its_price():
    path = [101.0, 101.2, 100.0] + [100.0] * 75                       # first bar's low is 100.45: hit in bar 2
    df = frame(day("2024-01-02", FLAT), day("2024-01-03", path))
    t = sm.trades(df, "as_shown").set_index("date").loc["2024-01-03"]
    assert t["ret"] == pytest.approx(100.45 / 101.0 - 1)


def test_a_weak_first_bar_sells_short_and_half_days_are_skipped():
    down = [99.0] + [99.0 - 0.1 * k for k in range(1, 78)]
    df = frame(day("2024-01-02", FLAT), day("2024-01-03", down), day("2024-01-04", [98.0] * 40))
    t = sm.trades(df, "to_close")
    assert list(t["date"]) == ["2024-01-03"] and t["side"].iloc[0] == -1 and t["ret"].iloc[0] > 0


def test_the_study_reads_like_the_others():
    rng = np.random.default_rng(0)
    days = [day(str(d.date()), list(100 + np.cumsum(rng.normal(0, 0.1, 78))))
            for d in pd.bdate_range("2019-01-01", periods=60)]
    res = sm.study(frame(*days), measured_cost=0.0034)
    txt = sm.text(res, "2019-01-01", "2019-03-25", 0.0034)
    assert "As shown" in txt and "at our measured cost" in txt and "Passed:" in txt and "+508%" in txt
