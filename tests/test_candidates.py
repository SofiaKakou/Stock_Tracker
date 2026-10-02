import numpy as np
import pandas as pd

from test_model import model_db  # noqa: F401
from trend_bot import alerts, candidates, db, ml


def table(months=72, stocks=200, seed=1):
    rng = np.random.default_rng(seed)
    month = np.repeat(pd.date_range("2015-01-31", periods=months, freq="ME"), stocks)
    n = len(month)
    df = pd.DataFrame({"month": month, "ticker": np.tile([f"T{i}" for i in range(stocks)], months)})
    for c in ml.SIGNALS + ["mom12"]:
        df[c] = rng.normal(size=n)
    good = rng.normal(size=n)
    df["ret"] = rng.normal(0, 0.1, n) - 0.03 * good          # less issuance -> better next month
    df["net_issuance"] = good
    df["litigious"] = rng.normal(size=n)                     # pure noise
    df["red_flags"] = np.where(df["month"] >= df["month"].unique()[-30], good, np.nan)   # only 30 months so far
    return df


def test_scoreboard_passes_only_signals_that_work_in_both_halves(monkeypatch):
    monkeypatch.setattr(ml, "CANDIDATES", {"net_issuance", "litigious", "red_flags", "risk_change"})
    board = candidates.scoreboard(table())
    assert board.loc["net_issuance", "verdict"] == "PASSES"
    assert board.loc["net_issuance", "mix_gain_first"] > 0 and board.loc["net_issuance", "ic_t"] > 2
    assert board.loc["litigious", "verdict"] == "not proven"
    assert board.loc["red_flags", "verdict"] == "not enough history yet"     # 15 months a half
    assert board.loc["risk_change", "verdict"] == "no data yet"
    text = candidates.text(board)
    assert "net_issuance     PASSES" in text and "no data yet" in text


def test_scoreboard_is_saved_and_posted_once_with_the_ask(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "CANDIDATES", {"net_issuance", "litigious"})
    con = db.connect(tmp_path / "m.db")
    candidates.save(con, candidates.scoreboard(table()))
    board = candidates.load(con)
    e = alerts.candidates_embed(board)
    assert e["description"].startswith("✅ **net_issuance**: PASSES")
    assert "**net_issuance** passed both tests" in e["description"] and "❌ **litigious**" in e["description"]


def test_study_candidates_runs_on_the_real_pipeline(model_db, tmp_path, monkeypatch, capsys):  # noqa: F811
    from test_fundamentals import bulk_zip
    from test_model import DATES
    from trend_bot import fundamentals
    from trend_bot.cli import main

    years = list(range(DATES[0].year - 1, DATES[-1].year))
    data = bulk_zip(years)
    monkeypatch.setattr(fundamentals.insiders, "CACHE_DIR", tmp_path / "sec")
    fundamentals.update_fundamentals(model_db, download=lambda p: (p.parent.mkdir(parents=True, exist_ok=True),
                                                                   p.write_bytes(data), p)[-1],
                                     log=lambda *_: None)
    assert main(["study", "candidates", "--db", str(tmp_path / "market.db"), "--since", str(DATES[260].date())]) == 0
    out = capsys.readouterr().out
    assert "Signals on probation" in out and "net_issuance" in out and "report_change" in out
    assert candidates.load(model_db)["rows"]["report_change"]["verdict"] == "no data yet"
