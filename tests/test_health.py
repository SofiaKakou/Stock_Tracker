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
    for title in ("Market weather", "Top ideas this month", "The portfolio to follow", "Pick scorecard",
                  "Insiders are buying", "Price trend turned up", "Warning signs in your stocks", "Market screen",
                  "Measures on trial", "Your watchlist today", "Bot health", "SEC data is back", "Daily email",
                  "Research run", "What this means", "Start here"):
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


def test_monthly_checks_run_once_a_month_and_retired_models_quarterly(tmp_path, monkeypatch, capsys):
    from trend_bot.cli import main
    from trend_bot.commands import data

    path = str(tmp_path / "m.db")
    db.connect(path).close()

    class Day(dt.date):
        value = dt.date(2026, 11, 2)

        @classmethod
        def today(cls):
            return cls.value

    monkeypatch.setattr(data.dt, "date", Day)
    assert main(["monthly-due", "--db", path]) == 0
    steps = capsys.readouterr().out.split("\n")
    assert "study ideas" in steps and "study audit" in steps and "study ml" not in steps     # November: not quarterly
    assert main(["monthly-due", "--db", path, "--mark"]) == 0
    capsys.readouterr()
    main(["monthly-due", "--db", path])
    assert capsys.readouterr().out == ""                                                   # done for the month
    Day.value = dt.date(2027, 1, 1)
    main(["monthly-due", "--db", path])
    assert "study ml" in capsys.readouterr().out.split("\n")                               # January: quarterly
    Day.value = dt.date(2027, 1, 15)
    main(["monthly-due", "--db", path])
    assert capsys.readouterr().out == ""                                                   # past day 3
    main(["monthly-due", "--db", path, "--force"])
    assert "study forecast" in capsys.readouterr().out                                     # manual runs: everything


def test_stale_earnings_figures_are_flagged(tmp_path):
    con = healthy(tmp_path)
    con.executemany("INSERT INTO tickers(ticker, cik, last_date) VALUES (?, ?, '2026-10-06')",
                    [("FRESH", 1), ("OLD", 2), ("FUND", 3), ("ELSE", 4)])
    con.execute("INSERT INTO ideas_holdings(ticker, rank) VALUES ('FRESH', 1), ('FUND', 2)")
    db.set_meta(con, "mix_ranking", json.dumps({"date": "2026-10-01", "top": [{"ticker": "OLD"}], "bottom": []}))
    facts = [(1, "net_income", "2026-04-01", "2026-06-30", "2026-09-20"),
             (1, "net_income", "2026-01-01", "2026-03-31", "2026-05-05"),
             (2, "revenue", "2025-07-01", "2025-09-30", "2025-11-05"),
             (2, "revenue", "2025-10-01", "2025-12-31", "2026-02-20"),
             (3, "revenue", "2024-01-01", "2024-12-31", "2025-03-01"),  # reports only once a year: left out
             (3, "revenue", "2023-01-01", "2023-12-31", "2024-03-01"),
             (2, "assets", None, "2026-06-30", "2026-08-05"),        # a balance-sheet item doesn't count
             (4, "revenue", "2024-10-01", "2024-12-31", "2025-02-20")]  # not recommended: not our concern
    con.executemany("INSERT INTO facts(cik, item, priority, start, end, filed, val) VALUES (?, ?, 0, ?, ?, ?, 1)", facts)
    assert health.stale_earnings(con, TODAY) == [("OLD", "2026-02-20")]
    text = "\n".join(health.check(con, today=TODAY))
    assert "No new quarterly figures in over 150 days for 1 stock(s)" in text and "OLD (last 2026-02-20)" in text
    assert "SEC data may have stopped" not in text


def test_financials_that_stop_coming_in_are_flagged(tmp_path):
    con = healthy(tmp_path)
    con.execute("INSERT INTO facts(cik, item, priority, start, end, filed, val) "
                "VALUES (1, 'revenue', 0, '2026-04-01', '2026-06-30', '2026-08-20', 1)")
    text = "\n".join(health.check(con, today=TODAY))
    assert "newest figures are from 2026-08-20 (48 days ago)" in text
