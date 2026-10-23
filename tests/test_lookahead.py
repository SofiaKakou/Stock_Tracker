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
