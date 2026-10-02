from test_model import DATES, model_db  # noqa: F401  (S79 drifts up most, S00 down)
from trend_bot import alerts, track


def add_picks(con):
    day = str(DATES[-120].date())
    rows = [(day, f"S{i:02d}", "mix_top", 50.0, "") for i in range(70, 80)]       # the risers
    rows += [(day, f"S{i:02d}", "mix_bottom", 50.0, "") for i in range(0, 10)]    # the fallers: meant to lag
    rows += [(str(DATES[-5].date()), f"S{i:02d}", "ideas_buy", 50.0, "") for i in range(5)]  # too young
    con.executemany("INSERT INTO picks VALUES (?, ?, ?, ?, ?)", rows)
    con.commit()


def test_scorecard_grades_each_kind_of_pick(model_db):
    add_picks(model_db)
    card = track.scorecard(model_db).set_index("signal")
    assert set(card.index) == {"mix_top", "mix_bottom"}               # 5-day-old buys don't count yet
    assert card.loc["mix_top", "picks"] == 10 and card.loc["mix_top", "avg_days"] >= 100
    assert card.loc["mix_bottom", "meant_to_lag"] and card.loc["mix_bottom", "avg_vs_bench"] < card.loc["mix_top", "avg_vs_bench"]
    e = alerts.scorecard_embed(card.reset_index())
    assert e["title"].startswith("📊 Pick scorecard") and "should lag" in e["description"]


def test_scorecard_sent_once_a_month(model_db, tmp_path, monkeypatch):
    from trend_bot.cli import main

    add_picks(model_db)
    sent = []
    monkeypatch.setattr(alerts, "send", lambda url, payload, timeout=15: sent.append(payload))
    args = ["alert", "S01", "--from-db", "--db", str(tmp_path / "market.db"), "--webhook", "https://h",
            "--state", str(tmp_path / "s.json"), "--no-news", "--no-insiders", "--model"]
    assert main(args) == 0
    titles = [e["title"] for p in sent for e in p["embeds"]]
    assert sum(t.startswith("📊 Pick scorecard") for t in titles) == 1
    sent.clear()
    assert main(args) == 0
    assert not any(e["title"].startswith("📊") for p in sent for e in p["embeds"])
