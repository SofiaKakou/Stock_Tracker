import datetime as dt
import json

from trend_bot import base, db

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


def by_key(buildings):
    return {b["key"]: b for b in buildings}


def test_a_healthy_bot_is_a_green_village_with_optional_parts_not_built(tmp_path):
    b = by_key(base.survey(healthy(tmp_path), today=TODAY, daytrade_path=tmp_path / "none.json"))
    assert b["hall"]["status"] == "ok" and b["hall"]["headline"] == "Everything is fine"
    assert all(b[k]["status"] == "ok" for k in ("mine", "library", "tower", "lab", "gate"))
    assert all(b[k]["status"] == "off" for k in ("crawler", "camp", "arena", "vault", "clock"))


def test_problems_show_on_their_building_and_the_town_hall(tmp_path):
    con = healthy(tmp_path)
    con.execute("UPDATE tickers SET last_date = '2026-09-25'")
    db.set_meta(con, "sec_paused_until", "2999-01-01T12:00")
    con.execute("INSERT INTO run_log(started, command, exit_code, error) VALUES ('2026-10-06T22:40:00', 'journal', 1, 'boom')")
    b = by_key(base.survey(con, today=TODAY, failed_steps=["collecting news / the economy failed"],
                           daytrade_path=None))
    assert b["mine"]["status"] == "bad" and "8 trading days behind" in b["mine"]["headline"]
    assert b["library"]["status"] == "warn" and "slow down" in b["library"]["headline"]
    assert b["crawler"]["status"] == "warn" and b["vault"]["status"] == "bad"
    assert b["clock"]["status"] == "warn" and "1 failed" in b["clock"]["headline"]
    assert b["hall"]["status"] == "bad" and "to look at" in b["hall"]["headline"]


def test_day_trading_and_demo_accounts_are_read(tmp_path):
    con = healthy(tmp_path)
    db.set_meta(con, "paper_status", json.dumps({"date": "2026-10-06", "equity": 101234.5}))
    state = tmp_path / "daytrade.json"
    state.write_text(json.dumps({"days": {"2026-10-05": {"gap": {"pnl": 12.5, "trades": 3}},
                                          "2026-10-06": {"gap": {"pnl": -2.5, "trades": 1}}}}))
    b = by_key(base.survey(con, today=TODAY, daytrade_path=state))
    assert b["camp"]["status"] == "ok" and "$101,234" in b["camp"]["details"][0]
    assert b["arena"]["status"] == "ok" and b["arena"]["stat"] == "+$10" and "4 trades" in b["arena"]["details"][0]


def test_the_page_is_one_self_contained_file(tmp_path):
    buildings = base.survey(healthy(tmp_path), today=TODAY, daytrade_path=None)
    buildings[1]["headline"] = "</script><b>x"
    page = base.build_base(buildings, run_url="https://example.invalid/run")
    assert page.startswith("<!doctype html>") and "Trend Bot Village" in page
    assert page.count('<button class="plot') == len(base.ART) == len(buildings)
    assert "</script><b>x" not in page and "https://example.invalid/run" in page
    assert "http" not in page.replace("https://example.invalid/run", "").replace("http://www.w3.org", "")


def test_base_command_writes_the_page(tmp_path, capsys):
    from trend_bot.cli import main

    healthy(tmp_path).close()
    out = tmp_path / "base.html"
    assert main(["base", "--db", str(tmp_path / "m.db"), "--out", str(out), "--daytrade", str(tmp_path / "x")]) == 0
    assert out.exists() and "Price mine" in capsys.readouterr().out
