import json

from conftest import submissions_zip
from trend_bot import db, fates, sec_submissions


def doc(name, sic=None, forms=(), dates=(), items=None):
    return {"name": name, "sic": str(sic) if sic else "", "sicDescription": f"code {sic}" if sic else "",
            "filings": {"recent": {"form": list(forms), "filingDate": list(dates),
                                   "items": list(items) if items else [""] * len(forms)}}}


def test_bulk_file_fills_industry_codes_and_fates(tmp_path, monkeypatch):
    monkeypatch.setattr(sec_submissions.insiders, "CACHE_DIR", tmp_path / "sec")
    con = db.connect(tmp_path / "m.db")
    con.executemany("INSERT INTO tickers(ticker, cik, last_date) VALUES (?, ?, '2026-09-30')",
                    [("BANK", 1), ("SOFT", 2)])
    # Two big companies without prices (from their filings): one went bankrupt, one was bought out.
    con.executemany("INSERT INTO facts VALUES (?, 'public_float', 0, NULL, '2015-06-30', '2016-02-15', 2e9)",
                    [(10,), (11,)])
    con.execute("INSERT INTO company_fates VALUES (12, 'Old Co', 'bankrupt', '2012-01-05', '2020-01-01')")
    con.commit()
    files = {1: doc("Bank Corp", 6022), 2: doc("Soft Inc", 7372),
             10: doc("Bust Co", 3711, ["10-K", "8-K"], ["2016-02-15", "2017-03-01"], ["", "1.03"]),
             11: doc("Bought Co", 2834, ["DEFM14A", "15-12B"], ["2018-01-10", "2018-04-02"]),
             99: doc("Unrelated", 1000)}
    calls = []

    def fetch(path):
        calls.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(submissions_zip(files))
        return path

    counts = sec_submissions.update(con, fetch=fetch, log=lambda *_: None)
    assert counts["industry"] == 2 and counts["fates"] == 2 and counts["bankrupt"] == 1 and counts["acquired"] == 1
    assert dict(con.execute("SELECT ticker, sic FROM tickers")) == {"BANK": 6022, "SOFT": 7372}
    got = {c: (s, d) for c, s, d in con.execute("SELECT cik, status, fate_date FROM company_fates")}
    assert got[10] == ("bankrupt", "2017-03-01") and got[11] == ("acquired", "2018-04-02")
    assert got[12] == ("bankrupt", "2012-01-05")                  # final outcomes are left alone
    assert 99 not in got
    assert not (tmp_path / "sec" / "submissions.zip").exists()    # 1.5 GB not kept
    assert fates.companies_to_check(con) == []

    assert sec_submissions.update(con, fetch=fetch, log=lambda *_: None) == {}   # weekly: not again today
    assert len(calls) == 1
    sec_submissions.update(con, fetch=fetch, force=True, log=lambda *_: None)
    assert len(calls) == 2
