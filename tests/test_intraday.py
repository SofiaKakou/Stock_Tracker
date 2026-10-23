import datetime as dt

import numpy as np
import pandas as pd
import pytest

from trend_bot import intraday


def bars_frame(days, symbols, rng, momentum=0.0, noise=0.002):
    """Half-hour regular-hours bars ([symbol, date, slot, open, high, low, close, volume]).
    momentum: the last half hour of each day moves this much in the direction of the first."""
    rows = []
    for sym in symbols:
        price = 100.0
        for day in days:
            first_dir = 0.0
            for slot in range(13):
                r = rng.normal(0, noise)
                if slot == 0:
                    first_dir = np.sign(r) if r else 1.0
                if slot == 12:
                    r = momentum * first_dir + rng.normal(0, noise / 4)
                o, c = price, price * (1 + r)
                rows.append((sym, day, slot, o, max(o, c) * 1.0005, min(o, c) * 0.9995, c, 1e5))
                price = c
    return pd.DataFrame(rows, columns=["symbol", "date", "slot", "open", "high", "low", "close", "volume"])


DAYS = [str(d.date()) for d in pd.bdate_range("2019-01-01", "2023-12-31")]


def test_a_planted_pattern_passes_and_noise_does_not():
    rng = np.random.default_rng(1)
    planted = bars_frame(DAYS, ["SPY"], rng, momentum=0.002)
    r = intraday.evaluate(intraday.intraday_momentum(planted), draws=100)
    assert r["early"] > 0 and r["late"] > 0 and r["luck"] < 0.05
    assert intraday.verdict(r).startswith("PASSES")

    noise = bars_frame(DAYS, ["SPY"], np.random.default_rng(2), momentum=0.0)
    r = intraday.evaluate(intraday.intraday_momentum(noise), draws=100)
    assert not intraday.verdict(r).startswith("PASSES") and r["per_trade"] < 0   # costs eat it


def test_opening_range_and_gap_fade_on_a_made_up_day():
    # Day 2: opens 3% up (a gap), first half hour 100-101, breaks above at slot 2, ends at 104.
    rows = []
    for slot in range(13):
        rows.append(("AAA", "2024-01-02", slot, 97.0, 97.0, 97.0, 97.0, 1))
    path = [100.5] + [100.5, 101.5] + [102.0] * 9 + [104.0]
    for slot, c in enumerate(path):
        o = 100.0 if slot == 0 else path[slot - 1]
        hi, lo = (101.0, 100.0) if slot == 0 else (max(o, c), min(o, c))
        rows.append(("AAA", "2024-01-03", slot, o, hi, lo, c, 1))
    df = pd.DataFrame(rows, columns=["symbol", "date", "slot", "open", "high", "low", "close", "volume"])
    orb = intraday.opening_range(df).set_index("date")
    # Closes above 101 at slot 2; in at slot 3's open (101.5), out at the close (104).
    assert orb.loc["2024-01-03", "ret"] == pytest.approx(104 / 101.5 - 1)
    fade = intraday.gap_fade(df).set_index("date")
    assert fade.loc["2024-01-03", "ret"] == pytest.approx(-(104 / 100 - 1))   # gapped up: short it
    assert "2024-01-02" not in fade.index


class FakeSession:
    def __init__(self, sip_allowed=False):
        self.headers, self.calls, self.sip_allowed = {}, [], sip_allowed

    def get(self, url, params, timeout):
        self.calls.append(dict(params))
        if params["feed"] == "sip" and not self.sip_allowed:
            return Resp(403, {})
        page = params.get("page_token")
        bars = {"SPY": [{"t": "2024-01-02T14:30:00Z" if not page else "2024-01-02T15:00:00Z",
                         "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]}
        return Resp(200, {"bars": bars, "next_page_token": None if page else "p2"})


class Resp:
    def __init__(self, code, body):
        self.status_code, self.body = code, body

    def json(self):
        return self.body

    def raise_for_status(self):
        assert self.status_code < 400


def test_download_follows_pages_and_falls_back_to_iex(tmp_path):
    data = intraday.Data("k", "s", session=FakeSession())
    df = data.bars(["SPY"], "2024-01-01T00:00:00Z", "2024-01-03T00:00:00Z", pause=0)
    assert len(df) == 2 and data.feed == "iex" and data.http.headers["APCA-API-KEY-ID"] == "k"
    con = intraday.connect(tmp_path / "i.db")
    added = intraday.update(con, intraday.Data("k", "s", session=FakeSession(sip_allowed=True)), ["SPY"],
                            today=dt.date(2016, 6, 1), log=lambda *_: None)
    assert added == 2 and con.execute("SELECT value FROM meta WHERE key='feed'").fetchone()[0] == "sip"
    s = intraday.sessions(con)
    assert list(s["slot"]) == [0, 1] and s["date"].iloc[0] == "2024-01-02"   # 9:30 and 10:00 New York


def test_text_names_what_passed():
    r = {"intraday_momentum": {"trades": 10, "days": 10, "per_trade": 0.001, "gross_per_trade": 0.0012,
                               "yearly": 0.2, "early": 0.1, "late": 0.1, "hit": 0.6, "t": 3.0, "luck": 0.0,
                               "worst_day": -0.01},
         "opening_range": {"days": 0}, "gap_fade": {"days": 0}}
    out = intraday.text(r, "iex", "2016-01-04", "2026-10-22")
    assert "PASSES" in out and "Passed: Market's last half hour" in out and "IEX only" in out


def test_new_patterns_and_filters_on_made_up_days():
    rng = np.random.default_rng(3)
    days = [str(d.date()) for d in pd.bdate_range("2019-01-01", "2021-12-31")]
    df = bars_frame(days, ["SPY", "AAA", "BBB"], rng, momentum=0.002, noise=0.01)
    drive, rev = intraday.opening_drive(df), intraday.eod_reversal(df, move=0.01)
    assert len(drive) > 100 and set(drive["symbol"]) <= {"AAA", "BBB"} and len(rev) > 10
    earnings = {("AAA", days[300]), ("BBB", days[301])}
    f = intraday.filters(df, earnings, draws=20)
    assert set(f) >= {"intraday_momentum", "opening_drive"}
    assert "earnings days" in f["opening_drive"] and "earnings days" not in f["intraday_momentum"]
    assert "nervous market" in f["intraday_momentum"]
    assert "Filters" in intraday.filters_text(f)


def test_earnings_dates_after_the_close_count_for_the_next_day(tmp_path):
    con = intraday.connect(tmp_path / "i.db")
    stamps = {"AAA": [pd.Timestamp("2024-01-25 16:05", tz="America/New_York")],      # after the close: Friday
              "BBB": [pd.Timestamp("2024-01-25 07:00", tz="America/New_York")]}      # before the open: same day
    got = intraday.earnings_days(con, ["SPY", "AAA", "BBB"], fetch=lambda s: stamps[s], log=lambda *_: None)
    assert got == {("AAA", "2024-01-26"), ("BBB", "2024-01-25")}
    assert intraday.earnings_days(con, ["AAA"], fetch=lambda s: 1 / 0) == got   # kept, not fetched again
