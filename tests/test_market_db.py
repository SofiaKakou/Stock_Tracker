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
    assert db.get_meta(con, "insider_daily_through") == "2026-04-07"
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
    assert "Insider cluster buys" in out and "survivorship" in out
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
    keep = ["AAPL", "BRK-B", "BF-A", "GOOGL", "SNOW", "ASMLF"]
    drop = ["AACPW", "AAC-WT", "AACOW", "SPACU", "ABCDR", "BAC-PL", "PSA-P", "XYZ-UN", "XYZ-RT", "XYZ-WS"]
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
