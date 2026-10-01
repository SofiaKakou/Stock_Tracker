import numpy as np
import pandas as pd
import pytest

from test_model import model_db  # noqa: F401  (fixture: 80 simulated stocks + SPY)
from trend_bot import forecast, track
from trend_bot.cli import main


def test_logit_learns_a_known_relationship():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(5000, 2))
    p_true = 1 / (1 + np.exp(-(0.3 + 1.5 * X[:, 0])))  # only the first input matters
    y = (rng.random(5000) < p_true).astype(float)
    m = forecast.Logit(l2=0.1).fit(X, y)
    assert m.w[1] == pytest.approx(1.5, abs=0.15) and abs(m.w[2]) < 0.1
    p = m.predict(X)
    assert abs(p.mean() - y.mean()) < 0.01
    m2 = forecast.Logit.from_json(m.to_json())
    np.testing.assert_allclose(m2.predict(X), p)


def test_training_rows_and_walk_forward(model_db):
    rows = forecast.training_rows(model_db)
    assert set(forecast.INPUTS) <= set(rows.columns) and {"up", "beat"} <= set(rows.columns)
    assert rows["weather"].isin([0.0, 1.0]).all() and rows["weather"].nunique() == 2
    first = int(rows["month"].dt.year.max())
    preds = forecast.walk_forward(rows.assign(), first_year=first)
    assert len(preds) and (preds["month"].dt.year == first).all()
    assert preds["p_beat"].between(0, 1).all()
    card = forecast.scorecard(preds)
    assert {"accuracy", "naive_accuracy", "brier", "naive_brier"} <= set(card["summary"].columns)
    assert "Top 10% ideas" in card["ideas"].index


def test_live_predictions_cache_and_tracking(model_db):
    models, trained = forecast.load_models(model_db, log=lambda *_: None)
    calls = []
    again, _ = forecast.load_models(model_db, log=calls.append)
    assert calls == []  # cached for the month
    preds, date, invest = forecast.predict_today(model_db, models)
    assert preds["p_beat"].is_monotonic_decreasing and preds["why"].str.len().gt(0).all()
    new = track.record_forecast(model_db, preds, date, n=5)
    assert len(new) == 10
    assert track.record_forecast(model_db, preds, date, n=5) == set()


def test_cli_forecast_study_report_alert(model_db, tmp_path, monkeypatch, capsys):
    from trend_bot import alerts

    path = str(tmp_path / "market.db")
    assert main(["forecast", "--db", path, "--top", "5"]) == 0
    out = capsys.readouterr().out
    assert "Most likely to beat the S&P 500" in out and "Most likely to lag" in out

    last_year = model_db.execute("SELECT MAX(last_date) FROM tickers").fetchone()[0][:4]
    assert main(["study", "forecast", "--db", path, "--first-year", last_year]) == 0
    out = capsys.readouterr().out
    assert "How often it was right" in out and "Top 10% ideas" in out

    assert main(["report", "--db", path, "--out", str(tmp_path / "r")]) == 0
    assert "30-day outlook" in (tmp_path / "r" / "latest.html").read_text(encoding="utf-8")

    sent = []
    monkeypatch.setattr(alerts, "send", lambda url, payload: sent.append(payload))
    args = ["alert", "S01", "--from-db", "--db", path, "--webhook", "https://h", "--state", str(tmp_path / "s.json"),
            "--no-news", "--no-insiders", "--forecast"]
    assert main(args) == 0
    assert any(e["title"].startswith("🔮") for p in sent for e in p["embeds"])
    sent.clear()
    assert main(args) == 0  # same week: not sent again
    assert not any(e["title"].startswith("🔮") for p in sent for e in p["embeds"])
