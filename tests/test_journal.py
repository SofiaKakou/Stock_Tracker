import datetime as dt
import json

from trend_bot import db, journal


class FakeConn:
    """Records the SQL; enough to check what the journal writes."""

    def __init__(self):
        self.sql, self.rows = [], {}

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.sql.append((sql, params))

    def executemany(self, sql, rows):
        table = sql.split("dbo.")[1].split(" ")[0]
        self.rows.setdefault(table, []).extend(rows)
        self.sql.append((sql, len(rows)))

    def fetchall(self):
        return []                                   # nothing stored yet

    def commit(self):
        pass


def test_the_journal_writes_every_table_once_per_day(tmp_path):
    con = db.connect(tmp_path / "m.db")
    con.executemany("INSERT INTO ideas_periods VALUES (?, ?, ?)", [("2026-10-02", "AAA", 1), ("2026-10-02", "BBB", 1)])
    db.set_meta(con, "demo_history", json.dumps({"monthly": {"dates": ["2026-10-23"], "values": [100_000.0]},
                                                 "spy": {"dates": ["2026-10-23"], "values": [760.0]}}))
    db.set_meta(con, "safety_last", json.dumps({"date": dt.date.today().isoformat(),
                                                "exit": [["AAA", "bankruptcy filing"]], "warn": []}))
    con.execute("INSERT INTO run_log(started, command, exit_code) VALUES (?, 'nightly', 0)",
                (dt.datetime.now().isoformat(timespec="seconds"),))
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    con.execute("INSERT INTO news VALUES (1, ?, 'AAPL', 'Apple beats', 's', 'Benzinga', 'u', 0.1)", (now,))
    con.executemany("INSERT INTO macro VALUES (?, ?, ?)", [("VIXCLS", "2026-10-22", 18.5), ("DGS10", "2026-10-22", 4.1)])
    con.execute("INSERT INTO ranking_snapshots VALUES ('2026-10-23', 'AAA', 1, 0.9, 1.0, 'Energy', 15)")
    con.executemany("INSERT INTO measure_snapshots VALUES ('2026-10-23', 'AAA', ?, ?)", [("roe", 0.8), ("sue", 0.6)])
    con.commit()
    state = {"log": [{"date": "2026-10-23", "symbol": "AAPL", "pattern": "breakout", "side": -1, "qty": 14,
                      "entry": 347.3, "exit": 345.0, "ret": 0.0066, "pnl": 32.2, "time": "10:30"}],
             "days": {"2026-10-23": {"breakout": {"pnl": 32.2, "trades": 1}}}}
    results = {"gap_fade": {"trades": 10, "days": 9, "per_trade": -0.0002, "yearly": -0.1, "early": -0.1,
                            "late": -0.1, "hit": 0.5, "t": -2.1, "luck": 0.9}}
    conn = FakeConn()
    written = journal.sync(conn, con, state, results, log=lambda *_: None)
    assert written == {"day_trades": 1, "day_summary": 1, "account_values": 2, "monthly_holdings": 2,
                       "safety_events": 1, "study_results": 1, "runs": 1, "news": 1, "macro": 2,
                       "ranking_snapshots": 1, "measure_snapshots": 2}
    alters = [s for s, _ in conn.sql if "ALTER TABLE" in s]
    assert len(alters) == 3                              # bid, ask, spread added to an older day_trades
    creates = [s for s, _ in conn.sql if s.startswith("IF OBJECT_ID")]
    assert len(creates) == len(journal.TABLES)                          # tables made if missing
    deletes = [(s, p) for s, p in conn.sql if s.startswith("DELETE FROM dbo.day_trades")]
    assert deletes == [("DELETE FROM dbo.day_trades WHERE trade_date IN (%s)", ("2026-10-23",))]   # rerun-safe
    assert conn.rows["day_trades"][0][:3] == ("2026-10-23", "AAPL", "breakout")
    assert conn.rows["safety_events"][0][1:3] == ("AAA", "exit")


def test_not_set_up_means_nothing_happens(monkeypatch):
    for k in ("AZURE_SQL_SERVER", "AZURE_SQL_USER", "AZURE_SQL_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert journal.connect() is None


def test_every_column_written_exists_in_its_table():
    cols = {t: {c.split()[0] for c in spec.replace("\n", " ").split(",") if c.strip() and not c.strip().startswith("PRIMARY")}
            for t, spec in journal.TABLES.items()}
    row = {"date": "2026-10-23", "symbol": "A"}
    assert set(journal.day_trade_rows({"log": [row]})[0]) <= cols["day_trades"]
    assert set(journal.day_summary_rows({"days": {"2026-10-23": {"gap": {"pnl": 1, "trades": 1}}}})[0]) <= cols["day_summary"]
    assert set(journal.account_rows({"day": {"dates": ["2026-10-23"], "values": [1.0]}})[0]) <= cols["account_values"]
    assert set(journal.safety_rows({"exit": [("A", "x")]}, dt.date(2026, 10, 23))[0]) <= cols["safety_events"]
