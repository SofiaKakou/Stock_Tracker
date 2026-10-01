import json

import numpy as np
import pandas as pd

from test_fundamentals import bulk_zip  # noqa: F401
from test_model import DATES, model_db  # noqa: F401
from trend_bot import db, fundamentals, ml


def synthetic(n_months=72, n_stocks=300, seed=0):
    """Next month's return depends on an interaction: cheap AND a positive earnings surprise."""
    rng = np.random.default_rng(seed)
    months = pd.date_range("2012-01-31", periods=n_months, freq="ME")
    rows = []
    for m in months:
        f = pd.DataFrame(rng.normal(size=(n_stocks, len(ml.FEATURES))), columns=ml.FEATURES)
        both = (f["book_to_market"] > 0.5) & (f["sue"] > 0.5)
        f["ret"] = 0.03 * both + rng.normal(0, 0.05, n_stocks)
        f["month"], f["ticker"] = m, [f"T{i}" for i in range(n_stocks)]
        rows.append(f)
    return pd.concat(rows, ignore_index=True)


def test_walk_forward_finds_an_interaction_on_unseen_years():
    df = synthetic()
    preds = ml.walk_forward(df, first_year=2014, trees=150, log=lambda *_: None)
    assert preds["month"].dt.year.min() == 2014          # nothing predicted from its own training data
    card = ml.scorecard(preds, split_year=2016)
    t = card["all"]
    assert t.loc["model", "mean_ic"] > 0.05 and t.loc["model", "ic_t"] > 5
    assert t.loc["model", "mean_ic"] > t.loc["simple_mix", "mean_ic"]
    assert t.loc["model", "top10_per_year"] > t.loc["model", "average_stock_per_year"]
    passed, text = ml.verdict(card, 2016)
    assert passed and text.startswith("PASSED")


def test_noise_does_not_pass():
    df = synthetic()
    df["ret"] = np.random.default_rng(1).normal(0, 0.05, len(df))
    preds = ml.walk_forward(df, first_year=2014, trees=50, log=lambda *_: None)
    passed, text = ml.verdict(ml.scorecard(preds, split_year=2016), 2016)
    assert not passed and text.startswith("NOT PASSED")


def test_today_ranks_the_latest_month():
    df = synthetic(n_months=30)
    latest = df["month"].max()
    df.loc[df["month"] == latest, "ret"] = np.nan        # not known yet
    ranking, imp = ml.today(df, trees=80)
    assert len(ranking) == 300 and ranking["score"].is_monotonic_decreasing
    assert set(imp.index[:2]) == {"book_to_market", "sue"}


def test_study_ml_end_to_end(model_db, tmp_path, monkeypatch, capsys):
    from trend_bot.cli import main

    years = list(range(DATES[0].year - 1, DATES[-1].year))
    data = bulk_zip(years)
    monkeypatch.setattr(fundamentals.insiders, "CACHE_DIR", tmp_path / "sec")
    fundamentals.update_fundamentals(model_db, download=lambda p: (p.parent.mkdir(parents=True, exist_ok=True),
                                                                   p.write_bytes(data), p)[-1],
                                     log=lambda *_: None)
    assert main(["study", "ml", "--db", str(tmp_path / "market.db"), "--trees", "20", "--universe", "100"]) == 0
    out = capsys.readouterr().out
    assert "Verdict:" in out and "Top 15:" in out and "simple_mix" in out
    saved = json.loads(db.get_meta(model_db, "ml_verdict"))
    assert set(saved) >= {"passed", "date", "ic", "mix_ic"}
