import datetime as dt
import json

from test_paper import FakeBroker, setup
from trend_bot import db, health, paper, safety

TODAY = dt.date(2026, 11, 10)

def portfolio_db(tmp_path):
    con = setup(tmp_path)    # holds AAA, BRK-B, CCC in the portfolio to follow (see test_paper)
    con.executemany("INSERT INTO tickers(ticker, cik, last_date) VALUES (?, ?, '2026-11-09')",
                    [("AAA", 1), ("BRK-B", 2), ("CCC", 3)])
    con.executemany("INSERT INTO ideas_holdings(ticker, rank, since, entry_price) VALUES (?, ?, '2026-11-02', ?)",
                    [("AAA", 1, 100.0), ("BRK-B", 2, 400.0), ("CCC", 3, 80.0)])
    return con

def test_serious_events_mean_an_exit_and_the_rest_a_warning_once_each(tmp_path):
    con = portfolio_db(tmp_path)
    con.executemany("INSERT INTO events VALUES (?, ?, '8-K', ?, ?)",
                    [("a1", 1, "2026-11-09", "1.03"),          # AAA: bankruptcy -> exit
                     ("a2", 2, "2026-11-09", "4.01"),          # BRK-B: auditor change -> warning
                     ("a3", 3, "2026-10-01", "4.02")])         # too old to be new
    found = safety.check(con, TODAY)
    assert found["exit"] == [("AAA", "bankruptcy filing (filed 2026-11-09)")]
    assert [t for t, _ in found["warn"]] == ["BRK-B", "CCC"]   # CCC: down 38% since it was bought
    assert safety.excluded(con, TODAY) == {"AAA": "bankruptcy filing (filed 2026-11-09)"}
    assert safety.check(con, TODAY) == {"exit": [], "warn": []}  # each reported once
    assert safety.excluded(con, TODAY + dt.timedelta(days=safety.EXCLUDE_DAYS + 1)) == {}
    e = safety.embed(found, [])
    assert "Emergency exit" in e["description"] and "Warnings" in e["description"]

def test_big_falls_are_warnings(tmp_path):
    con = portfolio_db(tmp_path)
    # CCC: bought at 80, the stored closes are 50 (test_paper's setup): down 37%.
    found = safety.check(con, TODAY)
    assert ("CCC", "down -38% since it was bought on 2026-11-02") in found["warn"]
    con.executemany("INSERT INTO prices(ticker, date, close, adj_close) VALUES ('AAA', ?, ?, ?)",
                    [("2026-11-08", 100.0, 100.0), ("2026-11-09", 80.0, 80.0)])
    found = safety.check(con, TODAY)
    assert ("AAA", "fell -20% in one day") in found["warn"]

def test_the_demo_account_sells_an_emergency_exit_now_and_keeps_it_out(tmp_path):
    con = portfolio_db(tmp_path)
    con.execute("INSERT INTO events VALUES ('a1', 1, '8-K', '2026-11-09', '3.01')")
    safety.check(con, TODAY)
    db.set_meta(con, "paper_synced", "2026-11-02")             # already following this month's portfolio
    broker = FakeBroker(positions={"AAA": 48, "BRK.B": 12, "CCC": 98})
    res = paper.sync(con, broker, today=TODAY, log=lambda *_: None)
    assert ("AAA", "close", 48) in broker.sent and res["skipped"].startswith("already matches")
    db.set_meta(con, "paper_synced", "")                       # next month: it isn't bought back
    broker = FakeBroker(positions={"BRK.B": 12, "CCC": 98})
    paper.sync(con, broker, today=TODAY, log=lambda *_: None)
    assert not any(s == "AAA" for s, _, _ in broker.sent)

def test_drawdown_alarms_once_until_recovered(tmp_path):
    con = portfolio_db(tmp_path)
    db.set_meta(con, "paper_status", json.dumps({"start_date": "2026-10-30", "start_equity": 100_000,
                                                 "date": "2026-11-09", "equity": 100_000, "peak": 105_000}))
    assert safety.drawdown(con) == []                           # 4.8% below the peak: fine
    status = json.loads(db.get_meta(con, "paper_status"))
    db.set_meta(con, "paper_status", json.dumps({**status, "equity": 90_000}))   # -10% vs the S&P 500's +2%
    alarms = safety.drawdown(con)
    assert any("below its peak" in a for a in alarms) and any("behind the S&P 500" in a for a in alarms)
    assert safety.drawdown(con) == []                           # not repeated

def test_health_warns_about_a_stale_demo_account_and_recent_exits(tmp_path):
    from test_health import TODAY as HDAY, healthy

    con = healthy(tmp_path)
    db.set_meta(con, "paper_status", json.dumps({"date": "2026-10-01", "equity": 1}))
    db.set_meta(con, "paper_excluded", json.dumps({"AAA": {"why": "bankruptcy", "date": "2026-10-06"}}))
    text = "\n".join(health.check(con, today=HDAY))
    assert "Demo account: not checked since 2026-10-01" in text and "Emergency exit in the last days: AAA" in text
