import io
import json
import zipfile

import pandas as pd

from conftest import submissions_zip
from test_fundamentals import bulk_zip  # noqa: F401
from test_model import DATES, model_db  # noqa: F401
from trend_bot import db, events, factors, fundamentals, ml, sec_submissions
from trend_bot.commands.watchlist import _red_flag_embed


def test_keep_only_red_flag_items_and_late_notices():
    assert events.keep("8-K", "2.02,9.01") is None                 # earnings release: not a red flag
    assert events.keep("8-K", "9.01,4.02,5.02") == "4.02,5.02"
    assert events.keep("NT 10-K", "") == "late"
    assert events.keep("10-K", "4.02") is None and events.keep("8-K", None) is None


def bulk(tmp_path, recent, older=None):
    def fetch(path):
        data = io.BytesIO(submissions_zip({7: {"name": "A", "sic": "7372", "filings": {"recent": recent}}}))
        if older:
            with zipfile.ZipFile(data, "a") as z:
                z.writestr("CIK0000000007-submissions-001.json", json.dumps(older))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.getvalue())
        return path
    return fetch


def test_bulk_file_stores_red_flags_and_is_read_again_when_it_learns_more(tmp_path, monkeypatch):
    monkeypatch.setattr(sec_submissions.insiders, "CACHE_DIR", tmp_path / "sec")
    con = db.connect(tmp_path / "m.db")
    con.execute("INSERT INTO tickers(ticker, cik, last_date) VALUES ('AAA', 7, '2026-09-30')")
    recent = {"form": ["8-K", "8-K", "NT 10-Q", "4"], "accessionNumber": ["e1", "e2", "e3", "e4"],
              "filingDate": ["2025-03-01", "2025-04-01", "2025-05-20", "2025-06-01"],
              "items": ["2.02,9.01", "4.01,9.01", "", ""], "reportDate": ["", "", "", ""],
              "primaryDocument": ["a.htm", "b.htm", "c.htm", "d.xml"]}
    older = {"form": ["8-K"], "accessionNumber": ["e0"], "filingDate": ["2012-01-05"], "items": ["4.02"]}
    db.set_meta(con, "submissions_updated", pd.Timestamp.now().isoformat())   # read yesterday, by older code
    counts = sec_submissions.update(con, fetch=bulk(tmp_path, recent, older), log=lambda *_: None)
    assert counts["events"] == 3                                   # forced: the file has new things to read
    got = con.execute("SELECT accession, form, items FROM events ORDER BY filed").fetchall()
    assert got == [("e0", "8-K", "4.02"), ("e2", "8-K", "4.01"), ("e3", "NT 10-Q", "late")]
    assert sec_submissions.update(con, fetch=bulk(tmp_path, recent), log=lambda *_: None) == {}   # fresh now


def test_counts_use_only_events_filed_in_the_window(tmp_path):
    con = db.connect(tmp_path / "m.db")
    con.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                    [("a", 1, "8-K", "2025-01-10", "4.02,5.02"), ("b", 1, "NT 10-K", "2025-03-01", "late"),
                     ("c", 2, "8-K", "2024-01-10", "2.06"), ("d", 1, "8-K", "2025-07-01", "4.01")])
    c = events.counts(events.load(con), pd.Timestamp("2025-06-30"))
    assert c.loc[1].to_dict() == {"red_flags": 1, "late_filing": 1, "exec_changes": 1}
    assert 2 not in c.index                                        # too old by then; the July one isn't known yet


def test_red_flags_are_studied_but_stay_out_of_the_mix(model_db, tmp_path, monkeypatch, capsys):  # noqa: F811
    from trend_bot.cli import main

    for sig in events.SIGNALS:
        assert sig in fundamentals.EXPECTED and sig in fundamentals.DESCRIPTIONS
        assert sig not in ml.SIGNALS and sig not in ml.FEATURES
    years = list(range(DATES[0].year - 1, DATES[-1].year))
    data = bulk_zip(years)
    monkeypatch.setattr(fundamentals.insiders, "CACHE_DIR", tmp_path / "sec")
    fundamentals.update_fundamentals(model_db, download=lambda p: (p.parent.mkdir(parents=True, exist_ok=True),
                                                                   p.write_bytes(data), p)[-1],
                                     log=lambda *_: None)
    # The weakest stocks (low numbers) keep reporting warning signs.
    rows = [(f"{cik}-{d.date()}", cik, "8-K", str(d.date()), "4.02,2.06")
            for cik in range(1, 21) for d in DATES[200::60]]
    model_db.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", rows)
    model_db.commit()
    df = factors.monthly_factors(model_db, since=str(DATES[260].date()), log=lambda *_: None)
    assert (df["red_flags"] > 0).any() and (df["red_flags"] == 0).any() and df["red_flags"].notna().all()
    res = factors.study(df, split_year=int(df["month"].dt.year.max()))
    assert res["all"].loc["red_flags", "mean_ic"] > 0              # fewer red flags = the stronger stocks here
    assert main(["study", "factors", "--db", str(tmp_path / "market.db"), "--since", str(DATES[260].date())]) == 0
    assert "red_flags" in capsys.readouterr().out


def test_alert_lists_new_red_flags_once(tmp_path):
    con = db.connect(tmp_path / "m.db")
    con.executemany("INSERT INTO tickers(ticker, cik) VALUES (?, ?)", [("AAA", 1), ("BBB", 2), ("CCC", 3)])
    con.execute("INSERT INTO ideas_holdings(ticker) VALUES ('BBB')")
    today = pd.Timestamp.today()
    day = lambda n: str((today - pd.Timedelta(days=n)).date())
    con.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                    [("a", 1, "8-K", day(3), "4.01"), ("b", 2, "NT 10-Q", day(5), "late"),
                     ("c", 1, "8-K", day(4), "5.02"),                       # routine: not alerted
                     ("d", 3, "8-K", day(2), "4.02"),                       # not followed
                     ("e", 1, "8-K", day(90), "4.02")])                     # too old
    e = _red_flag_embed(con, ["aaa"])
    text = e["description"]
    assert "**AAA** (watchlist)" in text and "auditor changed" in text
    assert "**BBB** (top-ideas portfolio)" in text and "will be late" in text
    assert "CCC" not in text and text.count("**") == 4
    assert _red_flag_embed(con, ["aaa"]) is None                   # already sent
    assert _red_flag_embed(con, ["zzz"]) is None                   # nothing for a ticker without events
    assert events.recent_flags(con, [99]).columns.tolist() == ["accession", "cik", "form", "filed", "items"]
