import json

import pandas as pd

from test_model import DATES, model_db  # noqa: F401
from trend_bot import report
from trend_bot.db import set_meta


def test_at_a_glance_shows_portfolio_record_flags_and_probation(model_db):  # noqa: F811
    con = model_db
    start = str(DATES[-60].date())
    con.executemany("INSERT INTO ideas_holdings VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [("S79", 1, start, 50.0, 0.9, "Technology", "cheap, profitable"),
                     ("S05", 2, start, 40.0, 0.8, "Energy", "beating earnings")])
    con.executemany("INSERT INTO ideas_periods VALUES (?, ?, 1)", [(start, "S79"), (start, "S05")])
    today = pd.Timestamp.today()
    con.execute("INSERT INTO events VALUES ('x', 6, '8-K', ?, '4.02')", (str((today - pd.Timedelta(days=10)).date()),))
    set_meta(con, "candidates_scoreboard", json.dumps({"date": "2026-10-02", "rows": {
        "net_issuance": {"verdict": "PASSES", "ic": 0.021, "ic_t": 3.4, "from": "2010-06"},
        "litigious": {"verdict": "not proven", "ic": -0.002, "ic_t": -0.4, "from": "2010-06"},
        "report_change": {"verdict": "no data yet"}}}))
    con.commit()
    html = "".join(report._glance_section(con, str))
    assert "top-ideas portfolio since " + start in html and "S&P 500 over the same days" in html
    assert "<b>S79</b>" in html and "cheap, profitable" in html
    assert "<b>S05</b>" in html and "past financial statements can't be relied on" in html   # S05 is cik 6
    assert html.index("net_issuance") < html.index("litigious") < html.index("report_change")
    assert "✅ <b>net_issuance</b>" in html and "+3.4" in html


def test_at_a_glance_before_the_first_update(tmp_path):
    from trend_bot import db

    con = db.connect(tmp_path / "m.db")
    html = "".join(report._glance_section(con, str))
    assert "starts at its first monthly update" in html and "probation" not in html
