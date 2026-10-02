import numpy as np
import pandas as pd
import pytest
import requests

from conftest import submissions_zip
from test_fundamentals import bulk_zip  # noqa: F401
from test_model import DATES, model_db  # noqa: F401
from trend_bot import db, factors, fundamentals, ml, sec_submissions, text_changes

BASE = ("our business depends on customers suppliers demand pricing competition regulation growth "
        "revenue margin products services markets employees technology strategy risk ").split()


def doc(seed: int, rewrite: float = 0.0, n: int = 3000) -> bytes:
    """An EDGAR-like HTML report: mostly the standard wording, `rewrite` share of new words."""
    rng = np.random.default_rng(seed)
    ws = list(rng.choice(BASE, n))
    k = int(n * rewrite)
    ws[:k] = [f"novel{rng.integers(0, 400)}word" for _ in range(k)]
    return ("<html><ix:header><x>hidden xbrl 123 secret</x></ix:header><body><p>"
            + " ".join(ws) + " 2025 $1,234</p></body></html>").encode()


def test_words_drop_tags_hidden_xbrl_and_numbers():
    ws = text_changes.words(b"<div><ix:header>secret stuff</ix:header><b>Risk</b> &amp; Revenue 2025 up</div>")
    assert ws == ["risk", "revenue"]


def test_same_wording_scores_1_and_rewrites_score_lower():
    fp = lambda raw: text_changes.fingerprint(text_changes.words(raw))
    a, same, rewritten = fp(doc(1)), fp(doc(1)), fp(doc(1, rewrite=0.5))
    assert text_changes.cosine(a, same) == pytest.approx(1.0)
    assert text_changes.cosine(a, rewritten) < 0.9
    assert np.array_equal(text_changes.unpack(text_changes.pack(a)), a.astype(float))


def add_filings(con, rows):
    con.executemany("INSERT INTO filings VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.commit()


def test_update_reads_newest_first_and_remembers_failures(tmp_path):
    con = db.connect(tmp_path / "m.db")
    add_filings(con, [("0001-24-1", 1, "10-K", "2024-02-15", "2023-12-31", "a.htm"),
                      ("0001-25-1", 1, "10-K", "2025-02-15", "2024-12-31", "b.htm"),
                      ("0001-25-2", 1, "10-Q", "2025-05-01", "2025-03-31", "gone.htm"),
                      ("0001-25-3", 1, "10-Q", "2025-08-01", "2025-06-30", "tiny.htm"),
                      ("0002-25-1", 2, "10-K", "2025-02-20", "2024-12-31", "other.htm")])
    seen = []

    def fetch(url):
        seen.append(url)
        if url.endswith("gone.htm"):
            raise requests.HTTPError("404")
        return b"<p>too short</p>" if url.endswith("tiny.htm") else doc(1)

    assert text_changes.update(con, limit=3, ciks={1}, fetch=fetch, log=lambda *_: None) == 3
    assert [u.rsplit("/", 1)[1] for u in seen] == ["tiny.htm", "gone.htm", "b.htm"]   # newest first, cik 1 only
    assert seen[0] == "https://www.sec.gov/Archives/edgar/data/1/0001253/tiny.htm"
    words = dict(con.execute("SELECT accession, words FROM doc_vectors"))
    assert words["0001-25-2"] == 0 and words["0001-25-3"] == 0 and words["0001-25-1"] > text_changes.MIN_WORDS
    assert text_changes.update(con, limit=10, ciks={1}, fetch=fetch, log=lambda *_: None) == 1  # only a.htm left


def test_changes_compare_the_same_report_a_year_earlier(tmp_path):
    con = db.connect(tmp_path / "m.db")
    add_filings(con, [("k1", 1, "10-K", "2023-02-15", "2022-12-31", "k1.htm"),
                      ("k2", 1, "10-K", "2024-02-15", "2023-12-31", "k2.htm"),   # same wording
                      ("k3", 1, "10-K", "2025-02-15", "2024-12-31", "k3.htm"),   # heavily rewritten
                      ("q1", 1, "10-Q", "2024-05-01", "2024-03-31", "q1.htm"),   # no 10-Q a year earlier
                      ("k9", 2, "10-K", "2025-02-20", "2024-12-31", "k9.htm")])
    docs = {"k1.htm": doc(1), "k2.htm": doc(1), "k3.htm": doc(1, rewrite=0.6), "q1.htm": doc(1), "k9.htm": doc(2)}
    text_changes.update(con, ciks={1, 2}, fetch=lambda url: docs[url.rsplit("/", 1)[1]], log=lambda *_: None)
    ch = text_changes.changes(con).set_index("filed")
    assert len(ch) == 2 and set(ch["form"]) == {"10-K"}                 # first years and lone 10-Q: nothing
    assert ch.loc["2024-02-15", "report_change"] == pytest.approx(0.0, abs=1e-9)
    assert ch.loc["2025-02-15", "report_change"] > 0.1
    latest = text_changes.latest(ch.reset_index(), pd.Timestamp("2025-03-31"))
    assert latest[1] == pytest.approx(ch.loc["2025-02-15", "report_change"])
    assert text_changes.latest(ch.reset_index(), pd.Timestamp("2025-12-31")).empty      # too old by then


def test_bulk_file_lists_reports_including_older_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(sec_submissions.insiders, "CACHE_DIR", tmp_path / "sec")
    con = db.connect(tmp_path / "m.db")
    con.execute("INSERT INTO tickers(ticker, cik, last_date) VALUES ('AAA', 7, '2026-09-30')")
    recent = {"form": ["10-K", "4", "10-Q"], "accessionNumber": ["a1", "a2", "a3"],
              "filingDate": ["2025-02-15", "2025-03-01", "2025-05-01"], "reportDate": ["2024-12-31", "", "2025-03-31"],
              "primaryDocument": ["k.htm", "f4.xml", "q.htm"]}
    older = {"form": ["10-K"], "accessionNumber": ["a0"], "filingDate": ["2010-02-15"],
             "reportDate": ["2009-12-31"], "primaryDocument": ["old.htm"]}

    def fetch(path):
        import io, json, zipfile
        data = io.BytesIO(submissions_zip({7: {"name": "A", "sic": "7372", "filings": {"recent": recent}}}))
        with zipfile.ZipFile(data, "a") as z:
            z.writestr("CIK0000000007-submissions-001.json", json.dumps(older))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.getvalue())
        return path

    counts = sec_submissions.update(con, fetch=fetch, log=lambda *_: None)
    assert counts["reports"] == 3
    got = con.execute("SELECT accession, form, period, primary_doc FROM filings ORDER BY filed").fetchall()
    assert got == [("a0", "10-K", "2009-12-31", "old.htm"), ("a1", "10-K", "2024-12-31", "k.htm"),
                   ("a3", "10-Q", "2025-03-31", "q.htm")]


def test_report_change_is_studied_but_stays_out_of_the_mix(model_db, tmp_path, monkeypatch, capsys):
    from trend_bot.cli import main

    assert "report_change" in fundamentals.EXPECTED
    assert "report_change" not in ml.SIGNALS and "report_change" not in ml.FEATURES
    years = list(range(DATES[0].year - 1, DATES[-1].year))
    data = bulk_zip(years)
    monkeypatch.setattr(fundamentals.insiders, "CACHE_DIR", tmp_path / "sec")
    fundamentals.update_fundamentals(model_db, download=lambda p: (p.parent.mkdir(parents=True, exist_ok=True),
                                                                   p.write_bytes(data), p)[-1],
                                     log=lambda *_: None)
    # Every company files a 10-K each year; the weakest stocks (low numbers) rewrite theirs the most.
    rows, docs = [], {}
    for cik in range(1, 81):
        for y in years[1:]:
            acc = f"{cik}-{y}"
            rows.append((acc, cik, "10-K", f"{y}-02-15", f"{y - 1}-12-31", f"{acc}.htm"))
            docs[f"{acc}.htm"] = doc(cik, rewrite=0.0 if y == years[1] else (80 - cik) / 160)
    add_filings(model_db, rows)
    text_changes.update(model_db, limit=10_000, fetch=lambda url: docs[url.rsplit("/", 1)[1]], log=lambda *_: None)
    df = factors.monthly_factors(model_db, since=str(DATES[260].date()), log=lambda *_: None)
    assert df["report_change"].notna().any()
    res = factors.study(df, split_year=int(df["month"].dt.year.max()))
    assert res["all"].loc["report_change", "mean_ic"] > 0        # less rewriting = the stronger stocks here
    assert main(["study", "factors", "--db", str(tmp_path / "market.db"), "--since", str(DATES[260].date())]) == 0
    assert "report_change" in capsys.readouterr().out
