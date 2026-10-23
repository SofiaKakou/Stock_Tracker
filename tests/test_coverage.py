import datetime as dt

import pandas as pd

from trend_bot import coverage, db

TODAY = dt.date.today()
RECENT = (TODAY - dt.timedelta(days=2)).isoformat()


def setup(tmp_path):
    con = db.connect(tmp_path / "m.db")
    rows = [("AAA", 1, "Alpha", RECENT, 50.0, 5e8, 0), ("AAB", 1, "Alpha B shares", RECENT, 50.0, 1e6, 0),
            ("CHP", 3, "Cheapo", RECENT, 2.0, 5e7, 0), ("OLD", 4, "Gone Inc", "2020-01-02", 10.0, 1e6, 0),
            ("FND", None, "Some Fund", RECENT, 100.0, 9e9, 1), ("NEW", 5, "Newco", RECENT, 30.0, 4e8, 0),
            ("TIN", 6, "Tiny Co", RECENT, 30.0, 1.0, 0), ("FEW", 7, "Few Data", RECENT, 30.0, 3e8, 0)]
    con.executemany("INSERT INTO tickers(ticker, cik, name, last_date, last_close, dollar_vol, is_etf) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.executemany("INSERT INTO prices(ticker, date, close, adj_close) VALUES (?, ?, 10, 10)",
                    [(t, "2015-01-02") for t in ("AAA", "CHP", "TIN", "FEW")] + [("NEW", RECENT)])
    ranked = pd.DataFrame({"score": [0.9], "pct": [1.0], "signals": [15]}, index=pd.Index(["AAA"], name="ticker"))
    ranked.attrs["too_few"] = {"FEW": 5}
    coverage.save(con, ranked, "2026-09-30")
    return con


def test_why_explains_every_kind_of_ticker(tmp_path):
    con = setup(tmp_path)
    say = lambda t: " ".join(coverage.why(con, t, universe=1))
    assert "ranked #1 of 1" in say("aaa") and "15 measures" in say("AAA")
    assert "isn't in the bot's list" in say("ZZZZ")
    assert "is a fund" in say("FND")
    assert "another share class" in say("AAB") and "AAA" in say("AAB")
    assert "only 5 of its measures" in say("FEW")
    assert "stopped trading" in say("OLD")
    assert "only since" in say("NEW")
    assert "under the $5 minimum" in say("CHP")
    assert "by trading volume" in say("TIN")


def test_audit_finds_planted_problems(tmp_path):
    con = db.connect(tmp_path / "m.db")
    future = (TODAY + dt.timedelta(days=30)).isoformat()
    con.executemany("INSERT INTO prices(ticker, date, close, adj_close) VALUES (?, ?, ?, ?)", [
        ("AAA", "2024-01-05", 10, 10), ("AAA", "2024-01-06", 10, 10),          # a Saturday
        ("AAA", "2024-07-04", 10, 10),                                          # a holiday (Thursday)
        ("AAA", "2024-07-08", 50, 50),                                          # a 5x jump, no split
        ("AAA", "2024-09-02", 50, 50),                                          # a 56-day gap
        ("AAA", future, 50, 50), ("BBB", "2024-01-05", 0, 0)])
    con.execute("INSERT INTO facts VALUES (1, 'revenue', 0, '2024-01-01', '2024-12-31', '2024-06-30', 5)")
    found = {r["check"]: r for r in coverage.audit(con)}
    for check in ("Prices dated on a weekend", "Prices dated on a market holiday", "Prices from the future",
                  "Impossible prices", "Unexplained one-day jumps", "Long gaps inside a price history",
                  "Financials filed before their period ended"):
        assert not found[check]["ok"], check
    assert found["Filings from the future"]["ok"]
    assert "AAA 2024-01-06" in found["Prices dated on a weekend"]["detail"]
    assert "To look into:" in coverage.audit_text(list(found.values()))
    clean = coverage.audit(db.connect(tmp_path / "clean.db"))
    assert all(r["ok"] for r in clean) and "Clean" in coverage.audit_text(clean)


def test_why_command_prints_the_answer(tmp_path, capsys):
    from trend_bot.cli import main

    setup(tmp_path).close()
    assert main(["why", "AAA", "FND", "--db", str(tmp_path / "m.db")]) == 0
    out = capsys.readouterr().out
    assert "ranked #1" in out and "is a fund" in out


def test_ranking_notes_who_was_left_out_for_too_few_measures():
    import numpy as np

    from test_ideas import panel_with_edge
    from trend_bot import ideas, ml

    df = panel_with_edge(months=2, n=60)
    last = df["month"] == df["month"].max()
    df.loc[last & (df["ticker"] == "T0"), ml.SIGNALS[3:]] = np.nan          # T0 knows 3 company measures + momentum
    ranked = ideas.rank_month(df)
    assert "T0" not in ranked.index and ranked.attrs["too_few"] == {"T0": 4}
