import datetime as dt
import json

from trend_bot import db, office

TODAY = dt.date(2026, 10, 7)  # a Wednesday


def healthy(tmp_path):
    con = db.connect(tmp_path / "m.db")
    con.execute("INSERT INTO tickers(ticker, last_date) VALUES ('SPY', '2026-10-06')")
    for key in ("facts_updated", "submissions_updated", "short_volume_last"):
        db.set_meta(con, key, "2026-10-05T10:00:00")
    db.set_meta(con, "mix_ranking", json.dumps({"date": "2026-10-01", "stocks": 900, "top": [], "bottom": []}))
    db.set_meta(con, "safety_last", json.dumps({"date": "2026-10-06"}))
    con.commit()
    return con


def by_key(workers):
    return {w["key"]: w for w in workers}


def test_a_healthy_bot_is_all_working_with_optional_desks_empty(tmp_path):
    b = by_key(office.survey(healthy(tmp_path), today=TODAY, daytrade_path=tmp_path / "none.json"))
    assert b["boss"]["status"] == "ok" and b["boss"]["headline"] == "Everything is fine"
    assert all(b[k]["status"] == "ok" for k in ("prices", "sec", "finra", "ideas", "safety"))
    assert all(b[k]["status"] == "off" for k in ("news", "demo", "daytrade", "journal", "logbook"))


def test_problems_show_on_their_worker_and_the_boss(tmp_path):
    con = healthy(tmp_path)
    con.execute("UPDATE tickers SET last_date = '2026-09-25'")
    db.set_meta(con, "sec_paused_until", "2999-01-01T12:00")
    con.execute("INSERT INTO run_log(started, command, exit_code, error) VALUES ('2026-10-06T22:40:00', 'journal', 1, 'boom')")
    b = by_key(office.survey(con, today=TODAY, failed_steps=["collecting news / the economy failed"],
                           daytrade_path=None))
    assert b["prices"]["status"] == "bad" and "8 trading days behind" in b["prices"]["headline"]
    assert b["sec"]["status"] == "warn" and "slow down" in b["sec"]["headline"]
    assert b["news"]["status"] == "warn" and b["journal"]["status"] == "bad"
    assert b["logbook"]["status"] == "warn" and "1 failed" in b["logbook"]["headline"]
    assert b["boss"]["status"] == "bad" and "to look at" in b["boss"]["headline"]


def test_day_trading_and_demo_accounts_are_read(tmp_path):
    con = healthy(tmp_path)
    db.set_meta(con, "paper_status", json.dumps({"date": "2026-10-06", "equity": 101234.5}))
    state = tmp_path / "daytrade.json"
    state.write_text(json.dumps({"days": {"2026-10-05": {"gap": {"pnl": 12.5, "trades": 3}},
                                          "2026-10-06": {"gap": {"pnl": -2.5, "trades": 1}}}}))
    b = by_key(office.survey(con, today=TODAY, daytrade_path=state))
    assert b["demo"]["status"] == "ok" and "$101,234" in b["demo"]["details"][0]
    assert b["daytrade"]["status"] == "ok" and b["daytrade"]["stat"] == "+$10" and "4 trades" in b["daytrade"]["details"][0]


def test_the_page_is_one_self_contained_file(tmp_path):
    workers = office.survey(healthy(tmp_path), today=TODAY, daytrade_path=None)
    workers[1]["headline"] = "</script><b>x"
    page = office.build_office(workers, run_url="https://example.invalid/run")
    assert page.startswith("<!doctype html>") and "Trend Bot Office" in page
    assert page.count('<button class="row"') == len(workers) == len(office.DESKS) + 1
    assert set(office.LOOKS) == set(office.PLATE) == {w["key"] for w in workers}
    assert "</script><b>x" not in page and "https://example.invalid/run" in page
    assert "<script src" not in page and "<link" not in page   # nothing loaded from elsewhere


def test_office_command_writes_the_page(tmp_path, capsys):
    from trend_bot.cli import main

    healthy(tmp_path).close()
    out = tmp_path / "office.html"
    assert main(["office", "--db", str(tmp_path / "m.db"), "--out", str(out), "--daytrade", str(tmp_path / "x")]) == 0
    assert out.exists() and "Price collector" in capsys.readouterr().out


def test_every_workflow_and_nightly_step_has_a_worker_to_show_it_live():
    from pathlib import Path

    import yaml

    flows = Path(__file__).resolve().parents[1] / ".github/workflows"
    assert {f.name for f in flows.glob("*.yml")} == set(office.LIVE_WORKFLOWS)
    assert set(office.LIVE_WORKFLOWS.values()) - {"steps"} <= set(office.LOOKS)
    nightly = yaml.safe_load((flows / "nightly.yml").read_text())["jobs"]
    names = [s["name"] for job in nightly.values() for s in job["steps"] if "name" in s]
    assert all(set(office.step_workers(n)) <= set(office.LOOKS) for n in names)
    expect = {"Update data": ["prices", "sec", "finra"], "Retry prices later": ["prices"], "Did the SEC refuse?": ["sec"],
              "Try the SEC again from this machine": ["sec"], "Monthly honesty check": ["ideas"],
              "Portfolio safety": ["safety"], "Demo account": ["demo"], "Crypto account summary": ["demo"],
              "Demo accounts over time": ["demo"], "Collect news, the economy and the ranking": ["news"],
              "Trading journal": ["journal"], "Report": ["boss"], "Bot health": ["boss"], "Office": ["logbook"],
              "Restore day-trading results": ["logbook"], "Keep the report": ["logbook"], "Save database": ["logbook"]}
    assert {n: office.step_workers(n) for n in expect} == expect
    assert set(expect) <= set(names)


def test_the_page_follows_the_repository_live_only_when_it_knows_it(tmp_path):
    workers = office.survey(healthy(tmp_path), today=TODAY, daytrade_path=None)
    page = office.build_office(workers, repo="someone/Stock_Tracker")
    assert '"repo": "someone/Stock_Tracker"' in page and "api.github.com/repos/" in page
    assert '"repo": null' in office.build_office(workers)
