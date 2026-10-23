import numpy as np
import pandas as pd

from test_ml import synthetic
from test_model import model_db  # noqa: F401
from trend_bot import ml_lab


def test_variants_are_tested_on_unseen_years_and_a_real_pattern_wins():
    df = synthetic()
    preds = ml_lab.walk_forward(df, first_year=2014, trees=120, log=lambda *_: None)
    assert preds["month"].dt.year.min() == 2014
    assert set(ml_lab.VARIANTS) <= set(preds.columns)
    card = ml_lab.scorecard(preds, split_year=2016)
    assert set(card.index) == set(ml_lab.VARIANTS) | {"simple_mix"}
    # The data hides an interaction a plain average can't see: the plain model wins.
    assert card.loc["base", "verdict"] == "BEATS the simple mix"
    assert card.loc["classify", "ic"] > card.loc["simple_mix", "ic"]
    assert "Winner(s): " in ml_lab.text(card, 2016)


def test_no_winner_on_pure_noise():
    df = synthetic()
    df["ret"] = np.random.default_rng(1).normal(0, 0.05, len(df))
    preds = ml_lab.walk_forward(df, first_year=2014, trees=40, log=lambda *_: None)
    card = ml_lab.scorecard(preds, split_year=2016)
    assert not (card["verdict"] == "BEATS the simple mix").any()
    assert "The simple mix stays" in ml_lab.text(card, 2016)


def test_pca_and_dissimilarity():
    rng = np.random.default_rng(0)
    base = rng.random((500, 3))
    X = np.column_stack([base, base[:, 0] * 0.999, base[:, 1]])     # two duplicate columns
    mean, comp = ml_lab.pca_fit(X, 0.999)
    assert comp.shape == (3, 5)
    test = np.vstack([X[:5], np.full((1, 5), 5.0)])                 # five seen rows, one far away
    di = ml_lab.dissimilarity(X, test, rng, sample=500)
    assert (di[:5] < 0.2).all() and di[5] > 3


def test_combine_falls_back_to_the_simple_mix():
    preds = pd.DataFrame({"month": pd.Timestamp("2020-01-31"), "base": [0.9, 0.1, 0.5, 0.45],
                          "simple_mix": [0.1, 0.2, 0.9, 0.3], "di": [0.5, 0.5, 2.0, 0.5]})
    out = ml_lab.combine(preds)
    assert out.loc[2, "abstain"] == out["simple_mix"].rank(pct=True)[2]   # unlike training: the mix decides
    assert out.loc[0, "abstain"] == 1.0                                   # familiar: the model decides
    assert np.allclose(out["blend"], 0.5 * out["base"].rank(pct=True) + 0.5 * out["simple_mix"].rank(pct=True))


def test_study_ml_lab_end_to_end(model_db, tmp_path, monkeypatch, capsys):  # noqa: F811
    from test_fundamentals import bulk_zip
    from test_model import DATES
    from trend_bot import fundamentals
    from trend_bot.cli import main

    data = bulk_zip(list(range(DATES[0].year - 1, DATES[-1].year)))
    monkeypatch.setattr(fundamentals.insiders, "CACHE_DIR", tmp_path / "sec")
    fundamentals.update_fundamentals(model_db, download=lambda p: (p.parent.mkdir(parents=True, exist_ok=True),
                                                                   p.write_bytes(data), p)[-1], log=lambda *_: None)
    assert main(["study", "mllab", "--db", str(tmp_path / "market.db"), "--trees", "20", "--universe", "100",
                 "--first-year", str(DATES[-1].year - 1)]) == 0
    out = capsys.readouterr().out
    assert "ML lab" in out and "simple_mix" in out and "(the bar to beat)" in out and "regularized" in out
