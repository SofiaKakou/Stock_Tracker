import datetime as dt
import json

from trend_bot import db, health

TODAY = dt.date(2026, 10, 7)  # a Wednesday


def healthy(tmp_path):
    con = db.connect(tmp_path / "m.db")
    con.execute("INSERT INTO tickers(ticker, last_date) VALUES ('SPY', '2026-10-06')")
    for key in ("facts_updated", "submissions_updated", "short_volume_last"):
        db.set_meta(con, key, "2026-10-05T10:00:00")
    db.set_meta(con, "mix_ranking", json.dumps({"date": "2026-10-01", "top": [], "bottom": []}))
    con.commit()
    return con


def test_all_fine_says_nothing(tmp_path):
    con = healthy(tmp_path)
    assert health.check(con, today=TODAY) == []
    assert health.to_send(con, [], today=TODAY) is None


def test_problems_are_found_in_plain_words(tmp_path):
    con = healthy(tmp_path)
    con.execute("UPDATE tickers SET last_date = '2026-09-29'")
    db.set_meta(con, "sec_paused_until", "2999-01-01T12:00")
    db.set_meta(con, "facts_updated", "2026-09-01T10:00:00")
    db.set_meta(con, "mix_ranking", json.dumps({"date": "2026-08-01"}))
    con.execute("DELETE FROM meta WHERE key = 'short_volume_last'")
    p = health.check(con, today=TODAY, failed_steps=["'study ml' failed"], job_status="failure")
    text = "\n".join(p)
    assert "Prices are 6 trading days old" in text and "SEC asked us to slow down" in text
    assert "Company financials: last updated 36 days ago" in text and "Short selling data: never" in text
    assert "top-ideas list is 67 days old" in text and "'study ml' failed" in text and "'failure'" in text


def test_warning_not_repeated_every_night_and_all_clear_once(tmp_path):
    con = healthy(tmp_path)
    p = ["Prices are 3 trading days old"]
    assert health.to_send(con, p, today=TODAY) == "problems"
    assert health.to_send(con, p, today=TODAY + dt.timedelta(days=1)) is None          # same problem: quiet
    assert health.to_send(con, p + ["new"], today=TODAY + dt.timedelta(days=1)) == "problems"   # changed
    assert health.to_send(con, p + ["new"], today=TODAY + dt.timedelta(days=4)) == "problems"   # reminder
    assert health.to_send(con, [], today=TODAY + dt.timedelta(days=5)) == "recovered"
    assert health.to_send(con, [], today=TODAY + dt.timedelta(days=6)) is None
    e = health.embed("problems", p, "https://example.invalid/run")
    assert e["title"] == "⚠️ Bot health" and "Open tonight's run" in e["description"]


def test_health_command_posts_to_discord(tmp_path, monkeypatch, capsys):
    from trend_bot import alerts
    from trend_bot.cli import main

    healthy(tmp_path).close()
    problems = tmp_path / "problems.txt"
    problems.write_text("the data update didn't finish\n")
    sent = []
    monkeypatch.setattr(alerts, "send", lambda url, payload, timeout=15: sent.append(payload))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    args = ["health", "--db", str(tmp_path / "m.db"), "--discord", "--problems", str(problems)]
    assert main(args) == 0
    assert sent and "data update didn't finish" in sent[0]["embeds"][0]["description"]
    assert main(args) == 0 and len(sent) == 1                                  # same problem: not again
    assert main(["health", "--db", str(tmp_path / "missing.db")]) == 0
    assert "didn't carry over" in capsys.readouterr().out


def test_guide_posted_once_until_it_changes(tmp_path, monkeypatch, capsys):
    from trend_bot import alerts
    from trend_bot.cli import main

    healthy(tmp_path).close()
    guide = tmp_path / "GUIDE.md"
    guide.write_text("# How to read the bot\n")
    sent = []
    monkeypatch.setattr(alerts, "send_file", lambda url, path, message="", timeout=60: sent.append((path.name, message)))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    args = ["send-guide", "--once", "--path", str(guide), "--db", str(tmp_path / "m.db")]
    assert main(args) == 0 and len(sent) == 1 and "pin" in sent[0][1]
    assert main(args) == 0 and len(sent) == 1                 # unchanged: not again
    guide.write_text("# How to read the bot (updated)\n")
    assert main(args) == 0 and len(sent) == 2


def test_real_guide_covers_every_message():
    from pathlib import Path

    text = (Path(__file__).parent.parent / "GUIDE.md").read_text()
    for title in ("Market weather", "Top ideas", "Top-ideas portfolio", "Pick scorecard", "Weekly outlook",
                  "Insider buying", "Market screen", "All-signal model", "Bot health", "Research run"):
        assert title in text


def test_sec_data_is_back_said_once_after_a_gap(tmp_path, monkeypatch):
    from trend_bot import alerts
    from trend_bot.cli import main
    from trend_bot.commands import data

    con = healthy(tmp_path)
    db.set_meta(con, "sec_last_ok", (dt.date.today() - dt.timedelta(days=17)).isoformat())
    db.set_meta(con, "sec_last_refusal", "429: Request Rate Threshold Exceeded")
    data._note_sec_ok(con, filings=812, companies=3_950, reports=1_500, bulk=True, added=0)
    assert db.get_meta(con, "sec_last_refusal") == "" and db.get_meta(con, "sec_last_ok") == dt.date.today().isoformat()
    e = health.catchup_embed(con)
    assert e["title"] == "✅ SEC data is back" and "after 17 days" in e["description"]
    assert "812 recent filing" in e["description"] and "3,950 companies" in e["description"]
    data._note_sec_ok(con, filings=5, companies=0, reports=10, bulk=False, added=0)   # next night: no gap
    assert json.loads(db.get_meta(con, "sec_catchup"))["filings"] == 812           # the catch-up stays until sent
    con.close()

    sent = []
    monkeypatch.setattr(alerts, "send", lambda url, payload, timeout=15: sent.append(payload))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    args = ["health", "--db", str(tmp_path / "m.db"), "--discord"]
    assert main(args) == 0 and main(args) == 0
    assert [p["embeds"][0]["title"] for p in sent].count("✅ SEC data is back") == 1
