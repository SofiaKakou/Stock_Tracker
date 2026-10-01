import datetime as dt

import numpy as np
import pandas as pd
import pytest

from test_fundamentals import bulk_zip  # noqa: F401
from test_model import DATES, N_STOCKS, model_db  # noqa: F401
from trend_bot import db, factors, fundamentals, short_interest

SI_HEADER = ("accountingYearMonthNumber|symbolCode|issueName|issuerServicesGroupExchangeCode|marketClassCode|"
             "currentShortPositionQuantity|previousShortPositionQuantity|stockSplitFlag|averageDailyVolumeQuantity|"
             "daysToCoverQuantity|revisionFlag|changePercent|changePreviousNumber|settlementDate")


def si_file(settle: str, rows: dict[str, tuple[float, float]], quoted: bool = False) -> bytes:
    """A FINRA short interest file: {symbol: (short, avg_volume)}."""
    q = (lambda v: f'"{v}"') if quoted else str
    lines = [SI_HEADER if not quoted else "|".join(f'"{c}"' for c in SI_HEADER.split("|"))]
    for sym, (short, vol) in rows.items():
        vals = [settle.replace("-", ""), sym, f"{sym} Inc", "A", "NYSE", int(short), 0, None, int(vol), 1.0,
                None, 0.0, 0, settle]
        lines.append("|".join("" if v is None else q(v) for v in vals))
    return ("\n".join(lines) + "\n").encode()


def sv_file(day: dt.date, rows: dict[str, tuple[float, float]]) -> bytes:
    lines = ["Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market"]
    lines += [f"{day:%Y%m%d}|{s}|{a}|0|{b}|B,Q,N" for s, (a, b) in rows.items()]
    lines.append(str(len(rows)))  # FINRA ends each file with a record count
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "market.db")
    c.executemany("INSERT INTO tickers(ticker) VALUES (?)", [("AAA",), ("BRK-B",), ("ZZZ",)])
    yield c
    c.close()


def test_parse_both_file_styles_and_class_shares():
    keep = {"AAA", "BRK-B"}
    for quoted in (False, True):
        rows = short_interest.parse_short_interest(
            si_file("2026-09-15", {"AAA": (500, 100), "BRKB": (900, 300), "OTCX": (5, 1)}, quoted), keep)
        assert sorted(rows) == [("AAA", "2026-09-15", 500.0, 100.0), ("BRK-B", "2026-09-15", 900.0, 300.0)]


def test_report_dates_fall_on_weekdays():
    t = short_interest.report_targets(dt.date(2018, 1, 1), dt.date(2018, 3, 31))
    assert t == [dt.date(2018, 1, 15), dt.date(2018, 1, 31), dt.date(2018, 2, 15), dt.date(2018, 2, 28),
                 dt.date(2018, 3, 15), dt.date(2018, 3, 30)]  # Mar 31 2018 was a Saturday


def test_update_finds_holiday_dates_and_skips_missing_reports(con):
    asked = []

    def fetch(url):
        asked.append(url)
        if url.endswith("shrt20180112.csv"):   # Jan 15 2018 was a holiday
            return si_file("2018-01-12", {"AAA": (100, 10)})
        if url.endswith("shrt20180131.csv"):
            return si_file("2018-01-31", {"AAA": (150, 10), "BRKB": (7, 1)})
        return None

    n = short_interest.update_short_interest(con, fetch=fetch, today=dt.date(2018, 4, 10),
                                             start=dt.date(2018, 1, 1), pause=0, log=lambda *_: None)
    assert n == 2
    got = con.execute("SELECT ticker, settle, short FROM short_interest ORDER BY settle, ticker").fetchall()
    assert got == [("AAA", "2018-01-12", 100.0), ("AAA", "2018-01-31", 150.0), ("BRK-B", "2018-01-31", 7.0)]
    files = dict(con.execute("SELECT target, settle FROM short_interest_files"))
    assert files["2018-01-15"] == "2018-01-12"
    assert files["2018-02-15"] is None          # old and missing: not asked again
    assert "2018-03-30" not in files            # recent: might just not be out yet

    asked.clear()
    short_interest.update_short_interest(con, fetch=fetch, today=dt.date(2018, 4, 10),
                                         start=dt.date(2018, 1, 1), pause=0, log=lambda *_: None)
    assert not any("201801" in u or "201802" in u for u in asked)


def test_short_volume_adds_each_day_once(con):
    def fetch(url):
        day = dt.datetime.strptime(url[-12:-4], "%Y%m%d").date()
        return sv_file(day, {"AAA": (40, 100), "BRK.B": (10, 50), "ZZZ": (0, 0)})

    kw = dict(fetch=fetch, start=dt.date(2026, 9, 1), pause=0, log=lambda *_: None)
    assert short_interest.update_short_volume(con, today=dt.date(2026, 9, 5), **kw) == 4   # Sep 1-4
    assert short_interest.update_short_volume(con, today=dt.date(2026, 9, 5), **kw) == 0   # nothing new
    assert short_interest.update_short_volume(con, today=dt.date(2026, 9, 9), **kw) == 2   # Sep 7 (Sat/Sun skipped) - 8
    rows = dict((t, (s, v, d)) for t, s, v, d in
                con.execute("SELECT ticker, short, total, days FROM short_volume WHERE month = '2026-09'"))
    assert rows["AAA"] == (240.0, 600.0, 6) and rows["BRK-B"] == (60.0, 300.0, 6)


def test_short_volume_waits_for_files_not_out_yet(con):
    fetch = lambda url: None
    n = short_interest.update_short_volume(con, fetch=fetch, today=dt.date(2026, 9, 10), start=dt.date(2026, 9, 8),
                                           pause=0, log=lambda *_: None)
    assert n == 0 and db.get_meta(con, "short_volume_last") is None


def frame(rows):
    si = pd.DataFrame(rows, columns=["ticker", "settle", "short", "avg_volume"])
    si["settle"] = pd.to_datetime(si["settle"])
    si["available"] = si["settle"] + pd.Timedelta(days=short_interest.PUBLISH_LAG_DAYS)
    return si


def test_factor_values_wait_for_publication_and_undo_splits():
    si = frame([("AAA", "2024-01-12", 100.0, 50.0), ("AAA", "2023-12-15", 50.0, 50.0),
                ("BBB", "2024-01-12", 400.0, 0.0), ("BBB", "2023-12-15", 400.0, 100.0),
                ("AAA", "2024-01-31", 999.0, 1.0)])   # not public by Jan 31
    sv = pd.DataFrame({"ticker": ["AAA"], "month": ["2024-01"], "short": [30.0], "total": [120.0], "days": [21]})
    splits = pd.DataFrame({"ticker": ["BBB"], "date": ["2024-01-02"], "ratio": [2.0]})
    shares = pd.Series({"AAA": 1000.0, "BBB": 1000.0})
    when = pd.Timestamp("2024-01-31")
    sd = pd.Series({"AAA": pd.Timestamp("2023-09-30"), "BBB": pd.Timestamp("2023-09-30")})
    v = short_interest.factor_values(si, sv, when, shares, sd, splits)
    assert v.loc["AAA", "days_to_cover"] == pytest.approx(2.0)
    assert v.loc["AAA", "short_ratio"] == pytest.approx(0.1)
    assert v.loc["AAA", "short_change"] == pytest.approx(np.log(101 / 51))
    assert np.isnan(v.loc["BBB", "days_to_cover"])            # no volume: unknown, not infinite
    # BBB split 2-for-1 after its share count (1000 -> 2000 shares) but before the short report.
    assert v.loc["BBB", "short_ratio"] == pytest.approx(400 / 2000)
    assert v.loc["AAA", "short_volume_ratio"] == pytest.approx(0.25)
    early = short_interest.factor_values(si, sv, pd.Timestamp("2024-01-20"), shares, sd, splits)
    assert early.loc["AAA", "short_ratio"] == pytest.approx(0.05)   # the Jan 12 report isn't out until Jan 24


def test_factor_study_includes_short_selling(model_db, tmp_path, monkeypatch, capsys):
    from trend_bot.cli import main

    years = list(range(DATES[0].year - 1, DATES[-1].year))
    data = bulk_zip(years)
    monkeypatch.setattr(fundamentals.insiders, "CACHE_DIR", tmp_path / "sec")
    fundamentals.update_fundamentals(model_db, download=lambda p: (p.parent.mkdir(parents=True, exist_ok=True),
                                                                   p.write_bytes(data), p)[-1],
                                     log=lambda *_: None)
    # Simulated short interest: the weakest stocks (low numbers) are the most shorted.
    def fetch(url):
        name = url.rsplit("/", 1)[1]
        d = dt.datetime.strptime(name[-12:-4], "%Y%m%d").date()
        if name.startswith("shrt"):
            return si_file(d.isoformat(), {f"S{i:02d}": (1000 * (N_STOCKS - i) + d.day, 5000)
                                           for i in range(N_STOCKS)})
        return sv_file(d, {f"S{i:02d}": (N_STOCKS - i, 100) for i in range(N_STOCKS)})

    monkeypatch.setattr(short_interest, "download", fetch)
    monkeypatch.setattr(short_interest, "SI_FIRST", DATES[0].date())
    monkeypatch.setattr(short_interest, "SV_FIRST", DATES[-60].date())
    monkeypatch.setattr(short_interest.time, "sleep", lambda s: None)
    assert main(["db", "update", "--short-only", "--db", str(tmp_path / "market.db")]) == 0
    assert main(["db", "status", "--db", str(tmp_path / "market.db")]) == 0
    assert "Short interest:" in capsys.readouterr().out

    df = factors.monthly_factors(model_db, since=str(DATES[260].date()), log=lambda *_: None)
    assert {"short_ratio", "days_to_cover", "short_change", "short_volume_ratio"} <= set(df.columns)
    assert df["short_ratio"].notna().any()
    res = factors.study(df, split_year=int(df["month"].dt.year.max()))
    assert res["all"].loc["short_combo", "mean_ic"] > 0   # less shorted = stronger drift in the simulation
    assert main(["study", "factors", "--db", str(tmp_path / "market.db"), "--since", str(DATES[260].date())]) == 0
    assert "short_combo" in capsys.readouterr().out


def test_stray_quote_in_a_company_name_does_not_break_parsing():
    # Real 2018 files have names like: 20180215|XYZ|Some "Odd Co|... with an unbalanced quote.
    data = si_file("2018-02-15", {"AAA": (500, 100), "BRKB": (900, 300)}).replace(b"AAA Inc", b'"AAA Odd Inc')
    rows = short_interest.parse_short_interest(data, {"AAA", "BRK-B"})
    assert sorted(r[0] for r in rows) == ["AAA", "BRK-B"]
