import sqlite3

import numpy as np
import pandas as pd

from trend_bot import scalp


def minute_bars(sym="RUN", closes=None, date="2026-03-02"):
    """Regular-hours 1-minute bars (New York) with the given closes; high/low 0.1% around them."""
    t = pd.date_range(f"{date} 09:30", periods=390, freq="1min", tz="America/New_York")
    c = np.full(390, 10.0) if closes is None else np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    df = pd.DataFrame({"symbol": sym, "t": t.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), "open": o,
                       "high": np.maximum(o, c) * 1.001, "low": np.minimum(o, c) * 0.999, "close": c, "volume": 1000.0})
    return df


def test_candidates_are_big_gap_ups_in_tradable_stocks_known_at_the_open():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE prices (ticker TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, adj_close REAL, volume REAL)")
    days = pd.bdate_range("2026-01-01", periods=30).strftime("%Y-%m-%d")
    rows = []
    for tk, gap, px, vol in [("BIG", 0.25, 10.0, 1e6), ("SMALLGAP", 0.05, 10.0, 1e6), ("PENNY", 0.5, 1.0, 1e7),
                             ("THIN", 0.3, 10.0, 1e3), ("SPLIT", 2.0, 10.0, 1e6)]:
        for i, d in enumerate(days):
            o = px * (1 + gap) if i == len(days) - 1 else px
            rows.append((tk, d, o, o, o, px, px, vol))
    con.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?,?)", rows)
    c = scalp.candidates(con, start=days[-1])
    assert c["ticker"].tolist() == ["BIG"] and c["gap"].iloc[0] == 0.25


def test_a_breakout_above_vwap_hits_its_target_and_a_fade_hits_its_stop():
    closes = 10.0 * 1.01 ** (np.arange(390) // 10)   # a 1% jump every 10 minutes: breakouts above VWAP
    g = scalp.day_bars(minute_bars(closes=closes))["RUN"]
    times, rets, _ = scalp.day_trades(g, (0.01, 0.005, 30))
    assert len(rets) >= 1 and rets.max() > 0
    falling = np.full(390, 10.0)
    falling[10] = 10.2                                 # one jump, then straight down
    falling[11:] = np.linspace(10.1, 9.0, 379)
    g = scalp.day_bars(minute_bars(closes=falling))["RUN"]
    times, rets, _ = scalp.day_trades(g, (0.01, 0.005, 30))
    assert len(rets) == 1 and rets[0] == -0.005


def test_nothing_is_held_past_1555():
    closes = np.full(390, 10.0)
    closes[350:] = np.linspace(10.0, 10.02, 40)       # a slow climb late in the day: the time limits end it
    g = scalp.day_bars(minute_bars(closes=closes))["RUN"]
    times, rets, _ = scalp.day_trades(g, (0.05, 0.05, 60))
    assert all(t.time() <= scalp.LAST_ENTRY.replace(minute=31) for t in times)


def test_the_study_reads_well():
    closes = 10.0 * 1.005 ** (np.arange(390) // 10)
    days = [(d, scalp.day_bars(minute_bars(closes=closes, date=d))) for d in ["2026-03-02", "2026-03-03", "2026-03-04", "2026-04-01"]]
    res, first, last, n = scalp.study(days, log=lambda *_: None)
    out = scalp.text(res, first, last, n, 4)
    assert n == 4 and "+1% target, -0.5% stop, 30 min" in out and out.splitlines()[-1].startswith("Passed")
