import numpy as np
import pandas as pd

from trend_bot import crypto_fast as cf


def coin(n=2000, falls=(), seed=0):
    """5-minute bars, flat at 100 with a little noise; at each bar in `falls` the price drops 4% over 15
    minutes and comes back over the next day."""
    t = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    c = 100 + np.random.default_rng(seed).normal(0, 0.01, n)
    for i in falls:
        c[i - 2:i + 1] = [99.0, 97.5, 96.0]
        c[i + 1:i + 289] = np.linspace(96.0, 101.0, len(c[i + 1:i + 289]))
    return pd.DataFrame({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": c, "high": c, "low": c, "close": c, "volume": 1.0})


def test_a_fast_fall_is_caught_every_5_minutes_but_not_by_the_hourly_quarter_check():
    g = coin(falls=[500])
    t = pd.to_datetime(g["t"], utc=True).dt.tz_localize(None).to_numpy("datetime64[ns]")
    c = g["close"].to_numpy()
    fast = cf.signal(t, c, cf.RULES["3% in 15 min, every 5 min"])
    assert fast[500]
    quarter = cf.signal(t, c, ([(3, 0.03)], True))
    assert quarter.sum() <= fast.sum()
    assert not (quarter & ~(pd.DatetimeIndex(t + np.timedelta64(5, "m")).minute % 15 == 0)).any()


def test_a_gap_in_the_bars_is_not_a_fall():
    g = coin().drop(index=range(100, 120)).reset_index(drop=True)
    g.loc[100:, ["open", "close"]] = 90.0
    t = pd.to_datetime(g["t"], utc=True).dt.tz_localize(None).to_numpy("datetime64[ns]")
    assert not cf.signal(t, g["close"].to_numpy(), cf.RULES["3% in 15 min, every 5 min"])[100:103].any()


def test_an_account_takes_only_as_many_coins_as_it_can_hold():
    ins = np.array(["2026-01-01T00:00", "2026-01-01T00:05", "2026-01-01T00:10", "2026-01-02T01:00"], "datetime64[ns]")
    outs = ins + np.timedelta64(1, "D")
    took = cf.capacity(ins, outs, np.zeros(4), slots=2)
    assert took.tolist() == [True, True, False, True]


def test_the_study_counts_trades_and_reads_well():
    frames = [("AAA/USD", coin(4000, falls=[500, 1500, 2500])), ("BBB/USD", coin(4000, falls=[800], seed=1))]
    res, first, last, n = cf.study(frames, log=lambda *_: None)
    r = res[("3% in 15 min, every 5 min", "1 day")]
    assert n == 2 and r["trades"] == 4 and r["gross"] > 0.04
    assert r["accounts"]["$100 (5 coins at once, $16 each: now)"]["per_day"] > 0
    out = cf.text(res, first, last, n)
    assert "sold 1 day later" in out and "$500 (30 coins at once" in out and out.splitlines()[-1].startswith("Passed:")


def test_a_waiting_buy_fills_only_if_the_price_comes_back_to_it():
    g = coin(1500, falls=[500])
    t = pd.to_datetime(g["t"], utc=True).dt.tz_localize(None).to_numpy("datetime64[ns]")
    o, h, lo, c = (g[k].to_numpy() for k in ("open", "high", "low", "close"))
    sig = np.zeros(len(c), bool)
    sig[500] = True
    lo2 = lo.copy()
    lo2[501:504] = 95.0                              # it dipped a little further: the buy at 96 fills
    ins, net, fired, filled, missed = cf.maker_trades(t, o, h, lo2, c, sig, 288, 0.0, 3)
    assert (fired, filled) == (1, 1) and net[0] > 0.03
    ins, net, fired, filled, missed = cf.maker_trades(t, o, h, lo, c, sig, 288, 0.0, 3)
    assert (fired, filled) == (1, 0) and len(missed) == 1     # it bounced straight away: missed


def test_the_waiting_order_study_reads_well():
    frames = [("AAA/USD", coin(4000, falls=[500, 1500, 2500]))]
    res, first, last, n = cf.maker_study(frames, log=lambda *_: None)
    out = cf.maker_text(res, first, last, n)
    assert "taking the price now (today)" in out and "waiting 30 min at 0.50% below" in out
    assert out.splitlines()[-1].startswith("Passed")
