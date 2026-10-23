import json

from trend_bot import db, report, runs


def test_every_command_leaves_a_logbook_line_with_a_stamp(tmp_path, capsys):
    from trend_bot.cli import main

    path = tmp_path / "m.db"
    con = db.connect(path)
    con.execute("INSERT INTO prices(ticker, date, close) VALUES ('SPY', '2026-10-22', 500)")
    con.commit()
    assert main(["health", "--db", str(path)]) == 0
    stamp = runs.current_stamp()
    assert stamp.startswith("code ") and "(prices to 2026-10-22)" in stamp and "settings " in stamp
    (row,) = runs.history(con)
    assert row["command"] == "health" and row["ok"] and row["data"]["prices_to"] == "2026-10-22"
    assert row["code"] and row["seconds"] is not None


def test_a_failing_command_is_logged_as_failed(tmp_path, monkeypatch):
    from trend_bot import health
    from trend_bot.cli import main

    path = tmp_path / "m.db"
    db.connect(path).close()
    monkeypatch.setattr(health, "check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        main(["health", "--db", str(path)])
    except RuntimeError:
        pass
    (row,) = runs.history(db.connect(path))
    assert not row["ok"] and "boom" in row["error"]


def test_fingerprint_changes_with_the_data(tmp_path):
    con = db.connect(tmp_path / "m.db")
    a = runs.data_fingerprint(con)
    con.execute("INSERT INTO prices(ticker, date, close) VALUES ('SPY', '2026-10-22', 500)")
    b = runs.data_fingerprint(con)
    assert a["id"] != b["id"] and b["parts"]["prices"] == [1, "2026-10-22"]
    assert runs.data_fingerprint(con)["id"] == b["id"]                       # same data, same fingerprint


def test_report_shows_the_logbook(tmp_path):
    con = db.connect(tmp_path / "m.db")
    con.execute("INSERT INTO run_log VALUES ('2026-10-23T01:00:00', '2026-10-23T01:20:00', 'ideas', 'abc1234', ?, "
                "'{}', ?, 0, NULL, 1200)", (json.dumps({"id": "7b41e0"}), json.dumps({"stocks_ranked": 912})))
    html = "".join(report._history_section(con, str))
    assert "Logbook" in html and "912 stocks ranked" in html and "abc1234" in html and "20 min" in html
