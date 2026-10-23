import pandas as pd

from test_fundamentals import bulk_zip
from test_model import DATES, model_db  # noqa: F401
from trend_bot import fundamentals, lookahead


def _with_financials(con, tmp_path, monkeypatch):
    years = list(range(DATES[0].year - 1, DATES[-1].year))
    data = bulk_zip(years)
    monkeypatch.setattr(fundamentals.insiders, "CACHE_DIR", tmp_path / "sec")
    fundamentals.update_fundamentals(con, download=lambda p: (p.parent.mkdir(parents=True, exist_ok=True),
                                                              p.write_bytes(data), p)[-1], log=lambda *_: None)
    return con


def test_the_signals_only_use_what_was_known(model_db, tmp_path, monkeypatch):  # noqa: F811
    con = _with_financials(model_db, tmp_path, monkeypatch)
    month = (DATES[-120] + pd.offsets.MonthEnd(0)).strftime("%Y-%m-%d")
    res = lookahead.check(con, [month], workdir=tmp_path, log=lambda *_: None)
    (table, uni), = res.values()
    assert uni["both"] > 20 and uni["only_full"] == 0 and uni["only_cut"] == 0
    assert (table["differ"] == 0).all() and len(table) > 5
    assert lookahead.verdict(res) == (True, [])
    assert "No look-ahead found" in lookahead.text(res)
    assert not list(tmp_path.glob("lookahead-*.db"))               # the copy is cleaned up


def test_a_peek_at_the_future_is_caught(model_db, tmp_path, monkeypatch):  # noqa: F811
    con = _with_financials(model_db, tmp_path, monkeypatch)
    real = fundamentals.as_of
    # Sabotage: financials looked up four months after the decision date (what a look-ahead bug does).
    monkeypatch.setattr(fundamentals, "as_of",
                        lambda facts, when, *a, **k: real(facts, when + pd.Timedelta(days=120), *a, **k))
    month = f"{DATES[-1].year}-01-31"      # the test companies file their yearly numbers each Feb 15
    res = lookahead.check(con, [month], workdir=tmp_path, log=lambda *_: None)
    clean, bad = lookahead.verdict(res)
    assert not clean and bad
    assert "Look-ahead suspected in:" in lookahead.text(res)


def test_months_to_check():
    assert lookahead.parse_months(None) == list(lookahead.DEFAULT_MONTHS)
    assert lookahead.parse_months("2012-06-30, 2021-06-30") == ["2012-06-30", "2021-06-30"]
    yearly = lookahead.yearly_months(pd.Timestamp("2026-10-23"))
    assert yearly[0] == f"{lookahead.FIRST_YEAR}-06-30" and yearly[-1] == "2025-06-30" and len(yearly) == 17


def test_many_months_get_a_summary_of_where_each_signal_differed():
    ok = pd.DataFrame({"stocks": [10, 10], "differ": [0, 0], "max_gap": [0.0, 0.0], "examples": ["", ""]},
                      index=["sue", "roe"])
    bad = ok.copy()
    bad.loc["sue", ["differ", "max_gap", "examples"]] = [2, 0.5, "X 1/2"]
    uni = {"both": 10, "only_full": 0, "only_cut": 0}
    res = {"2010-06": (ok, uni), "2011-06": (bad, uni), "2012-06": (ok, uni), "2013-06": (bad, uni)}
    out = lookahead.text(res)
    assert "sue                    2 of 4 months: 2011-06 (2), 2013-06 (2)" in out
    assert "roe" not in out.split("Where each")[1].split("Look-ahead")[0]
