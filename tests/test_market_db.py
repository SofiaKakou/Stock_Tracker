import datetime as dt
import io
import json
import zipfile

import numpy as np
import pandas as pd
import pytest

from conftest import make_prices
from test_insiders import form4
from trend_bot import db, insiders, market_data, sec_bulk
from trend_bot.cli import main
from trend_bot.screen import screen
from trend_bot.strategy import MACrossover
from trend_bot import study

N = 700
DATES = pd.bdate_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=1), periods=N, name="Date")


def raw(closes, dividend_factor=1.0):
    """Yahoo-style raw bars; Adj Close = Close * factor (dividend adjustment)."""
    p = make_prices(closes)
    p.index = DATES[-len(closes):]
    p["Adj Close"] = p["Close"] * dividend_factor
    p["Volume"] = 1_000_000
    return p


SERIES = {
    # Falls then turns up at the very end -> fresh BUY flip on the last bar.
    "FLIP": np.concatenate([np.linspace(100, 50, N - 1), [140]]),
    "UPPY": np.linspace(20, 80, N),
    "PENY": np.linspace(1, 2, N),       # penny stock, filtered out of screens
    "SPY": np.linspace(300, 400, N),
    "QQQ": np.linspace(300, 400, N),
    "IWM": np.linspace(150, 200, N),
}


class FakeYahoo:
    def __init__(self):
        self.calls = []
        self.factor = 1.0

    def __call__(self, tickers, start=None, period=None):
        self.calls.append((tuple(tickers), start, period))
        out = {}
        for t in tickers:
            if t in SERIES:
                df = raw(SERIES[t], self.factor)
                out[t] = df[df.index >= pd.Timestamp(start)] if start else df
        return out


UNIVERSE = {"fields": ["cik", "name", "ticker", "exchange"], "data": [
    [1, "Flip Corp", "FLIP", "Nasdaq"], [2, "Uppy Inc", "UPPY", "NYSE"], [3, "Penny Co", "PENY", "Nasdaq"],
    [4, "Ghost Ltd", "GHST", "NYSE"],            # Yahoo has no data
    [5, "Otc Thing", "OTCX", "OTC"],             # skipped unless include_otc
]}


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(insiders, "CACHE_DIR", tmp_path / "sec")
    monkeypatch.setattr(insiders, "_get", lambda url: json.dumps(UNIVERSE).encode())
    c = db.connect(tmp_path / "market.db")
    yield c
    c.close()


def test_universe_and_prices(con):
    assert market_data.update_universe(con) == 4 + 3  # 4 listed + SPY/QQQ/IWM
    yahoo = FakeYahoo()
    counts = market_data.update_prices(con, downloader=yahoo, pause=0, log=lambda *_: None)
    assert counts["new"] == 6 and counts["failed"] == 1
    assert db.status(con)["price_rows"] == 6 * N
    flip = db.load_prices(con, "FLIP")
    assert flip["Close"].iloc[-1] == pytest.approx(140)

    # Second run right away: nothing to do (resumable / max_age).
    assert market_data.update_prices(con, downloader=yahoo, pause=0, log=lambda *_: None)["updated"] == 0

    # Later run: incremental fetch from shortly before the last date.
    con.execute("UPDATE tickers SET updated_at = '2000-01-01'")
    yahoo.calls.clear()
    counts = market_data.update_prices(con, downloader=yahoo, pause=0, log=lambda *_: None)
    assert counts["updated"] == 6
    # Known tickers are fetched incrementally; only GHST (never had data) asks for full history.
    assert all(start or tickers == ("GHST",) for tickers, start, _ in yahoo.calls)

    # A dividend changes Yahoo's adjusted history -> full reload, adjusted prices follow.
    con.execute("UPDATE tickers SET updated_at = '2000-01-01'")
    yahoo.factor = 0.9
    counts = market_data.update_prices(con, downloader=yahoo, pause=0, log=lambda *_: None)
    assert counts["reloaded"] == 6
    assert db.load_prices(con, "FLIP")["Close"].iloc[-1] == pytest.approx(126)


def bulk_zip(rows):
    """A minimal SEC quarterly insider dataset zip."""
    sub = ["ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERCIK\tISSUERNAME\tISSUERTRADINGSYMBOL\tAFF10B5ONE"]
    own = ["ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP\tRPTOWNER_TITLE"]
    tr = ["ACCESSION_NUMBER\tNONDERIV_TRANS_SK\tTRANS_DATE\tTRANS_CODE\tTRANS_SHARES\tTRANS_PRICEPERSHARE\tSHRS_OWND_FOLWNG_TRANS"]
    for i, (acc, cik, name, code, date) in enumerate(rows):
        d = pd.Timestamp(date).strftime("%d-%b-%Y").upper()
        sub.append(f"{acc}\t{d}\t4\t{cik}\tCO\tSYM\t0")
        own.append(f"{acc}\t9{i}\t{name}\tDirector,Officer\tCFO")
        tr.append(f"{acc}\t{100 + i}\t{d}\t{code}\t1000\t10.5\t5000")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("2026q1/SUBMISSION.tsv", "\n".join(sub))
        z.writestr("2026q1/REPORTINGOWNER.tsv", "\n".join(own))
        z.writestr("2026q1/NONDERIV_TRANS.tsv", "\n".join(tr))
    return buf.getvalue()


def test_bulk_quarters_and_daily_feed(con):
    market_data.update_universe(con)
    zip_q1 = bulk_zip([("a-1", 1, "SMITH ANN", "P", "2026-02-10"), ("a-2", 1, "JONES BOB", "P", "2026-02-20"),
                       ("a-3", 2, "LEE CHO", "S", "2026-03-01")])
    idx = """Description: Daily Index of EDGAR Dissemination Feed by Form Type
Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
3           SOMEONE                                                       111         20260406    edgar/data/111/x.txt
4           FLIP CORP                                                     1           20260406    edgar/data/1/0000000001-26-000009.txt
4           DOE JANE                                                      222         20260406    edgar/data/1/0000000001-26-000009.txt
4/A         FLIP CORP                                                     1           20260406    edgar/data/1/y.txt
"""
    filing_txt = "<SEC-DOCUMENT>\n<XML>\n" + form4("DOE JANE", "P", "2026-04-03", 10, 99).replace(
        "<issuerCik>0001045810</issuerCik>", "<issuerCik>0000000001</issuerCik>").split("?>", 1)[1] + "\n</XML>\n"

    def fake_fetch(url):
        if "2026q1_form345" in url:
            return zip_q1
        if "form345" in url:
            import requests
            err = requests.HTTPError("404")
            err.response = type("R", (), {"status_code": 404})()
            raise err
        if "form.20260406.idx" in url:
            return idx.encode()
        if url.endswith("0000000001-26-000009.txt"):
            return filing_txt.encode()
        import requests
        err = requests.HTTPError("404")
        err.response = type("R", (), {"status_code": 404})()
        raise err

    today = dt.date(2026, 4, 8)
    assert sec_bulk.load_quarters(con, since_year=2026, today=today, fetch=fake_fetch, log=lambda *_: None) == 1
    rows = con.execute("SELECT ticker, insider, role, code, filed, price FROM insider_trades ORDER BY accession").fetchall()
    assert rows[0] == ("FLIP", "Smith Ann", "CFO, Director", "P", "2026-02-10", 10.5)
    assert rows[2][0] == "UPPY"

    n = sec_bulk.load_recent_days(con, max_days=30, today=today, fetch=fake_fetch, log=lambda *_: None)
    assert n == 1  # the Form 4 listed twice is fetched once; 3 and 4/A ignored
    daily = con.execute("SELECT ticker, insider, code, filed FROM insider_trades WHERE accession LIKE '0000000001-%'").fetchall()
    assert ("FLIP", "Doe Jane", "P", "2026-04-06") in daily
    assert "2026-04-07" in json.loads(db.get_meta(con, "insider_days_done"))
    # Running again doesn't refetch finished days.
    assert sec_bulk.load_recent_days(con, max_days=30, today=today, fetch=fake_fetch, log=lambda *_: None) == 0


@pytest.fixture
def full_db(con):
    market_data.update_universe(con)
    market_data.update_prices(con, downloader=FakeYahoo(), pause=0, log=lambda *_: None)
    recent = (DATES[-1] - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    older = DATES[300].strftime("%Y-%m-%d")
    later = DATES[310].strftime("%Y-%m-%d")
    trades = [("r1", 0, 1, "FLIP", "A", "Director", "P", recent, recent, 100, 50, None, 0),
              ("r2", 0, 1, "FLIP", "B", "Director", "P", recent, recent, 100, 50, None, 0),
              ("o1", 0, 2, "UPPY", "C", "Director", "P", older, older, 100, 40, None, 0),
              ("o2", 0, 2, "UPPY", "D", "Director", "P", later, later, 100, 40, None, 0),
              ("s1", 0, 2, "UPPY", "E", "Director", "S", later, later, 100, 40, None, 0)]
    sec_bulk._insert(con, trades)
    con.commit()
    return con


def test_screen_finds_flip_and_cluster(full_db):
    flips, clusters = screen(full_db, MACrossover(fast=10, slow=50))
    assert list(flips.index) == ["FLIP"] and flips.loc["FLIP", "signal"] == "BUY"
    assert bool(flips.loc["FLIP", "cluster"]) and flips.loc["FLIP", "buyers"] == 2
    assert "PENY" not in flips.index
    assert list(clusters.index) == ["FLIP"]


def test_study_events_and_returns(full_db):
    events = study.cluster_events(full_db, window_days=30, min_buyers=2, since="2000-01-01")
    assert sorted(events["ticker"]) == ["FLIP", "UPPY"]
    ev = study.add_returns(full_db, events, strategy=MACrossover(fast=10, slow=50))
    uppy = ev[ev["ticker"] == "UPPY"].iloc[0]
    assert uppy["entry_date"] > uppy["date"] and uppy["trend"] == "UP"
    assert uppy["1m"] > 0
    # UPPY rises 20->80 (x4) while SPY rises 300->400, so it beats SPY.
    assert uppy["3m_excess"] > 0
    table = study.summarize(ev)
    assert table.loc["1m", "events"] == 1  # FLIP's event is too recent for a 1m return

    flips = study.trend_flip_events(full_db, MACrossover(fast=10, slow=50), ["FLIP", "UPPY"], since="2000-01-01")
    assert set(flips["ticker"]) == {"FLIP", "UPPY"}


def test_cli_db_screen_study(full_db, tmp_path, capsys):
    path = str(tmp_path / "market.db")
    full_db.commit()
    assert main(["db", "status", "--db", path]) == 0
    assert main(["screen", "--db", path, "--fast", "10", "--slow", "50"]) == 0
    out = capsys.readouterr().out
    assert "FLIP" in out and "YES" in out and "Trend flips" in out
    assert main(["study", "both", "--db", path, "--fast", "10", "--slow", "50", "--since", "2000-01-01"]) == 0
    out = capsys.readouterr().out
    assert "Insider cluster buys" in out and "bankruptcy or buyout" in out and "median_vs_bench" in out
    assert main(["study", "insiders", "--db", path, "--since", "2000-01-01", "--until", "2001-01-01"]) == 0
    assert "0 events" in capsys.readouterr().out
    assert main(["backtest", "UPPY", "--from-db", "--db", path, "--fast", "10", "--slow", "50", "--period", "max"]) == 0
    assert "Buy & hold" in capsys.readouterr().out


def test_alert_market_embed(full_db, tmp_path, monkeypatch):
    from trend_bot import alerts

    path = str(tmp_path / "market.db")
    sent = []
    monkeypatch.setattr(alerts, "send", lambda url, payload: sent.append(payload))
    args = ["alert", "UPPY", "--from-db", "--db", path, "--fast", "10", "--slow", "50", "--webhook", "https://h",
            "--state", str(tmp_path / "s.json"), "--no-news", "--no-insiders", "--market"]
    assert main(args) == 0
    market = [e for p in sent for e in p["embeds"] if e["title"] == "🌎 Market screen"][0]
    assert "FLIP" in market["description"] and "🔔" in market["description"]


def test_non_stocks_filtered():
    keep = ["AAPL", "BRK-B", "BF-A", "GOOGL", "SNOW", "ASMLF", "BABAY"]
    drop = ["AACPW", "AAC-WT", "AACOW", "SPACU", "ABCDR", "BAC-PL", "PSA-P", "XYZ-UN", "XYZ-RT", "XYZ-WS", "BCAT-RW"]
    assert all(market_data.is_common_stock(t) for t in keep)
    assert not any(market_data.is_common_stock(t) for t in drop)


def test_missing_old_quarter_is_skipped(con):
    import requests

    loaded = []

    def fetch(url):
        if "2025q2" in url or "2026q3" in url:
            err = requests.HTTPError("404")
            err.response = type("R", (), {"status_code": 404})()
            raise err
        loaded.append(url)
        return bulk_zip([("x-" + url[-16:-12], 1, "A", "P", "2025-01-10")])

    logs = []
    n = sec_bulk.load_quarters(con, since_year=2025, today=dt.date(2026, 10, 20), fetch=fetch, log=logs.append)
    assert n == 5  # 2025 q1, q3, q4, 2026 q1, q2 - q2 2025 skipped, 2026q3 not out yet
    assert any("2025q2 not available" in m for m in logs) and any("2026q3 not published" in m for m in logs)


def test_bulk_links_from_listing_page(con):
    html = """<table>
      <a href="/files/structureddata/data/insider-transactions-data-sets/2026q1_form345.zip">2026 Q1</a>
      <a href='https://www.sec.gov/files/new-place/2026-q2-form345.zip'>2026 Q2</a>
      <a href="/about.html">About</a></table>"""
    links = sec_bulk.bulk_links(html)
    assert links[(2026, 1)].startswith("https://www.sec.gov/files/structureddata/")
    assert links[(2026, 2)] == "https://www.sec.gov/files/new-place/2026-q2-form345.zip"

    fetched = []

    def fetch(url):
        fetched.append(url)
        if url == sec_bulk.BULK_INDEX_URL:
            return html.encode()
        if url.endswith(".zip") and ("2026q1" in url or "2026-q2" in url):
            return bulk_zip([("z-" + str(len(fetched)), 1, "A", "P", "2026-02-10")])
        import requests
        err = requests.HTTPError("404")
        err.response = type("R", (), {"status_code": 404})()
        raise err

    n = sec_bulk.load_quarters(con, since_year=2026, today=dt.date(2026, 9, 29), fetch=fetch, log=lambda *_: None)
    assert n == 2 and "https://www.sec.gov/files/new-place/2026-q2-form345.zip" in fetched


def test_daily_feed_fills_gaps_left_by_short_runs(con):
    fetched = []

    def fetch(url):
        fetched.append(url)
        return b""  # empty index: no filings that day

    today = dt.date(2026, 9, 30)  # a Wednesday
    # A quick test run only covered the last few days...
    sec_bulk.load_recent_days(con, max_days=3, today=today, fetch=fetch, log=lambda *_: None)
    assert [u[-12:-4] for u in fetched] == ["20260928", "20260929"]
    # ...a later full run must still read the older days, but not those two again.
    fetched.clear()
    sec_bulk.load_recent_days(con, max_days=10, today=today, fetch=fetch, log=lambda *_: None)
    days = [u[-12:-4] for u in fetched]
    assert days == ["20260921", "20260922", "20260923", "20260924", "20260925"]


def test_cluster_filters(full_db):
    sec_bulk._insert(full_db, [
        ("x1", 0, 2, "UPPY", "Big Ceo", "Chief Executive Officer", "P", DATES[500].strftime("%Y-%m-%d"),
         DATES[500].strftime("%Y-%m-%d"), 100_000, 50, None, 0),
        ("x2", 0, 2, "UPPY", "Big Cfo", "CFO, Director", "P", DATES[505].strftime("%Y-%m-%d"),
         DATES[505].strftime("%Y-%m-%d"), 100_000, 50, None, 0),
    ])
    all_ev = study.cluster_events(full_db, since="2000-01-01")
    big = study.cluster_events(full_db, since="2000-01-01", min_value=1_000_000)
    execs = study.cluster_events(full_db, since="2000-01-01", officers_only=True)
    assert len(all_ev) == 3
    assert list(big["value"]) == [10_000_000.0]
    assert len(execs) == 1 and execs.iloc[0]["buyers"] == 2  # only the CEO + CFO cluster


def test_daily_feed_skips_unscreenable_companies(full_db):
    idx = """Form Type   Company Name      CIK         Date Filed  File Name
-----------------------------------------------------------------------------------
4           FLIP CORP         1           20260928    edgar/data/1/aaa.txt
4           SOME INSIDER      777         20260928    edgar/data/1/aaa.txt
4           PENNY CO          3           20260928    edgar/data/3/bbb.txt
4           OTHER INSIDER     888         20260928    edgar/data/3/bbb.txt
"""
    listed = sec_bulk.parse_daily_index(idx)
    assert [(p, c) for p, _, c in listed] == [("edgar/data/1/aaa.txt", {1, 777}), ("edgar/data/3/bbb.txt", {3, 888})]

    fetched = []

    def fetch(url):
        fetched.append(url)
        return idx.encode() if url.endswith(".idx") else b"<XML></XML>"

    today = dt.date(2026, 9, 29)
    sec_bulk.load_recent_days(full_db, max_days=1, today=today, fetch=fetch, log=lambda *_: None)
    assert [u for u in fetched if u.endswith(".txt")] == ["https://www.sec.gov/Archives/edgar/data/1/aaa.txt"]
    fetched.clear()
    full_db.execute("DELETE FROM meta WHERE key = 'insider_days_done'")
    sec_bulk.load_recent_days(full_db, max_days=1, today=today, fetch=fetch, log=lambda *_: None, all_companies=True)
    assert len([u for u in fetched if u.endswith(".txt")]) == 2


def submissions(*filings):
    forms, dates, items = zip(*filings) if filings else ((), (), ())
    return {"name": "Gone Inc", "filings": {"recent": {"form": list(forms), "filingDate": list(dates),
                                                       "items": list(items)}}}


def test_classify_company_fate():
    from trend_bot.fates import classify

    assert classify(submissions(("10-Q", "2015-05-01", ""), ("8-K", "2015-09-01", "1.03,7.01"),
                                ("15-12G", "2016-01-10", ""))) == ("bankrupt", "2015-09-01")
    assert classify(submissions(("DEFM14A", "2019-02-01", ""), ("8-K", "2019-04-01", "2.01,3.01,5.01"),
                                ("25-NSE", "2019-04-01", ""), ("15-12B", "2019-04-12", ""))) == ("acquired", "2019-04-01")
    assert classify(submissions(("10-K", "2012-03-01", ""), ("15-12G", "2012-06-01", ""))) == ("delisted", "2012-06-01")
    assert classify(submissions(("10-K", "2026-03-01", ""))) == ("unknown", None)
    # Merger paperwork years before a later deregistration doesn't count.
    assert classify(submissions(("DEFM14A", "2010-01-01", ""), ("15-12G", "2014-01-01", "")))[0] == "delisted"


def test_fates_fill_in_delisted_companies(full_db):
    from trend_bot import fates

    # GONE (CIK 77) had a 3-insider cluster, no prices, and went bankrupt 2 months later.
    day = DATES[400]
    trades = [(f"g{i}", 0, 77, "GONE", name, "Director", "P", day.strftime("%Y-%m-%d"), day.strftime("%Y-%m-%d"),
               1000, 12.0, None, 0) for i, name in enumerate(["A", "B", "C"])]
    # BOUGHT (CIK 88) was acquired within a month; OLDCO (CIK 99) is unclear.
    trades += [(f"b{i}", 0, 88, "BOUGHT", name, "Director", "P", day.strftime("%Y-%m-%d"),
                day.strftime("%Y-%m-%d"), 1000, 20.0, None, 0) for i, name in enumerate(["A", "B"])]
    trades += [(f"o{i}", 0, 99, "OLDCO", name, "Director", "P", day.strftime("%Y-%m-%d"),
                day.strftime("%Y-%m-%d"), 1000, 20.0, None, 0) for i, name in enumerate(["A", "B"])]
    sec_bulk._insert(full_db, trades)
    bankrupt_on = (day + pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    bought_on = (day + pd.Timedelta(days=20)).strftime("%Y-%m-%d")
    docs = {77: submissions(("8-K", bankrupt_on, "1.03")),
            88: submissions(("SC 14D9", bought_on, ""), ("15-12B", bought_on, "")),
            99: submissions(("10-K", "2020-01-01", ""))}
    asked = []

    def fetch(url):
        cik = int(url.split("CIK")[1][:10])
        asked.append(cik)
        return json.dumps(docs[cik]).encode()

    assert fates.companies_to_check(full_db) == [77, 88, 99]  # FLIP/UPPY have prices
    counts = fates.update_fates(full_db, fetch=fetch, log=lambda *_: None)
    assert counts == {"bankrupt": 1, "acquired": 1, "unknown": 1}
    assert fates.companies_to_check(full_db) == []  # nothing re-checked right away

    events = study.cluster_events(full_db, since="2000-01-01")
    priced = study.add_returns(full_db, events, strategy=MACrossover(fast=10, slow=50), benchmark="SPY")
    gone = study.add_fates(full_db, events, priced, benchmark="SPY").set_index("ticker")
    assert gone.loc["GONE", "1m"] != gone.loc["GONE", "1m"]  # bankrupt after 1 month: unknown then
    assert gone.loc["GONE", "3m"] == -1.0 and gone.loc["GONE", "12m"] == -1.0
    assert gone.loc["BOUGHT", "1m_excess"] == 0.0
    assert gone.loc["OLDCO", "status"] == "unknown" and gone.loc["OLDCO", "12m"] != gone.loc["OLDCO", "12m"]
    assert gone.loc["GONE", "entry_price"] == 12.0


def test_fund_detection_and_company_info(full_db):
    from trend_bot import company_info
    from trend_bot.screen import liquid_tickers

    assert company_info.is_fund("Nuveen Select Tax Free Income Portfolio", None, "NYSE")
    assert company_info.is_fund("Some Trust", 6726, "NYSE")
    assert company_info.is_fund("iShares Russell 2000", None, "ETF")
    assert not company_info.is_fund("Realty Income Corp", 6798, "NYSE")
    assert not company_info.is_fund("Flip Corp", None, "Nasdaq")
    # Once the SEC code says "company", a fund-sounding name doesn't matter.
    assert not company_info.is_fund("Mutual Fund Services Inc", 7374, "NYSE")

    docs = {1: {"sic": "3674", "sicDescription": "Semiconductors"}, 2: {"sic": "6726", "sicDescription": "Funds"},
            3: {"sic": "", "sicDescription": ""}, 4: {"sic": "1000", "sicDescription": "Mining"}}
    n = company_info.update_company_info(full_db, fetch=lambda u: json.dumps(docs[int(u.split("CIK")[1][:10])]).encode(),
                                         log=lambda *_: None)
    assert n == 4
    assert full_db.execute("SELECT sic FROM tickers WHERE ticker = 'FLIP'").fetchone()[0] == 3674
    assert "UPPY" in company_info.fund_tickers(full_db)  # SIC 6726
    assert "UPPY" not in liquid_tickers(full_db) and "UPPY" in liquid_tickers(full_db, include_funds=True)
    assert company_info.update_company_info(full_db, fetch=lambda u: 1 / 0, log=lambda *_: None) == 0  # done once


def test_strong_rule_tracking_and_alert(full_db, tmp_path, monkeypatch, capsys):
    from trend_bot import alerts, track

    # A third FLIP insider: 3 buyers, $15k... too small for the $250k rule at first.
    recent = (DATES[-1] - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    sec_bulk._insert(full_db, [("r3", 0, 1, "FLIP", "C", "CEO", "P", recent, recent, 100, 50, None, 0)])
    flips, clusters = screen(full_db, MACrossover(fast=10, slow=50))
    assert clusters.loc["FLIP", "buyers"] == 3 and not clusters.loc["FLIP", "strong"]
    flips, clusters = screen(full_db, MACrossover(fast=10, slow=50), strong_min_value=10_000)
    assert clusters.loc["FLIP", "strong"]

    new = track.record_picks(full_db, flips, clusters)
    assert new == {("FLIP", "uptrend"), ("FLIP", "insider_cluster"), ("FLIP", "strong_insider")}
    # The same cluster the next days isn't a new pick; the same-day rerun changes nothing either.
    assert track.record_picks(full_db, flips, clusters) == set()
    later = clusters.copy()
    later["date"] = (DATES[-1] + pd.Timedelta(days=5)).date()
    assert track.record_picks(full_db, flips.iloc[0:0], later) == set()

    perf = track.performance(full_db, benchmark="SPY")
    assert set(perf["signal"]) == {"uptrend", "insider_cluster", "strong_insider"}
    assert (perf["return"] == 0).all()  # recorded on the latest day
    assert list(track.summary(perf).index)[0].startswith("🔔")

    embed = alerts.market_embed(flips, clusters, "rule", new=new)
    assert embed["description"].index("Strong insider") < embed["description"].index("New uptrends")
    assert "**NEW**" in embed["description"] and embed["color"] == alerts.GOLD

    assert main(["track", "--db", str(tmp_path / "market.db")]) == 0
    out = capsys.readouterr().out
    assert "Track record of 3 picks" in out and "FLIP" in out
