import datetime as dt
import json

import pytest

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
                       "ranking_snapshots": 1, "measure_snapshots": 2, "forecast_outcomes": 0, "stocks": 0}
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


def test_the_stock_list_marks_funds_sectors_and_the_portfolio(tmp_path):
    con = db.connect(tmp_path / "m.db")
    con.executemany("INSERT INTO tickers(ticker, name, exchange, sic) VALUES (?, ?, ?, ?)",
                    [("AAPL", "Apple Inc.", "Nasdaq", 3571), ("SPY", "SPDR S&P 500 ETF Trust", "NYSE Arca", None)])
    con.execute("INSERT INTO ideas_holdings(ticker, rank) VALUES ('AAPL', 1)")
    rows = {r["ticker"]: r for r in journal.stock_rows(con)}
    assert rows["AAPL"]["sector"] == "Technology" and rows["AAPL"]["in_portfolio"] == 1 and rows["AAPL"]["is_fund"] == 0
    assert rows["SPY"]["sector"] == "Fund / ETF" and rows["SPY"]["is_fund"] == 1 and rows["SPY"]["in_portfolio"] == 0
    conn = FakeConn()
    journal.replace_all(conn, "stocks", list(rows.values()))
    assert conn.sql[0][0] == "DELETE FROM dbo.stocks" and conn.sql[1][0].startswith("INSERT INTO dbo.stocks (ticker, name")


def test_the_star_schema_script_is_complete():
    from trend_bot import collect, daytrade, intraday

    batches = journal.dw_script()
    sql = "\n".join(batches)
    assert "/*" + "PATTERNS*/" not in sql and "/*" + "SERIES*/" not in sql and "/*" + "ACCOUNTS*/" not in sql
    assert not any(b.upper() == "GO" for b in batches) and batches[0].startswith("--")
    for p in list(daytrade.PATTERN_TEST) + list(intraday.STRATEGIES) + list(collect.SERIES):
        assert f"N'{p}'" in sql                                          # every pattern and economic series
    assert "N'VIX, the market''s fear gauge'" in sql                     # quotes escaped
    for t in ("DimStock", "DimDate", "FactForecast", "FactDayTrade", "FactNews", "vStockSearch"):
        assert f"dw.{t}" in sql
    # Every procedure and view is replaced each night; tables only made when missing.
    creates = [b for b in batches if "CREATE TABLE" in b]
    assert creates and all("IF OBJECT_ID" in b.split("CREATE TABLE")[0] for b in creates)


def test_a_sleeping_database_is_given_time_to_wake_up(monkeypatch):
    import pymssql

    calls = []

    def fake_connect(**kw):
        calls.append(kw["database"])
        if len(calls) < 3:
            raise pymssql.OperationalError(40613, b"Database 'TrendBotMarket' is not currently available.")
        return "conn"

    monkeypatch.setattr(pymssql, "connect", fake_connect)
    for k in ("AZURE_SQL_SERVER", "AZURE_SQL_USER", "AZURE_SQL_PASSWORD"):
        monkeypatch.setenv(k, "x")
    assert journal.open_sql("TrendBotMarket", 600, wait=0, log=lambda *_: None) == "conn" and len(calls) == 3

    def wrong_password(**kw):
        calls.append("bad")
        raise pymssql.OperationalError(18456, b"Login failed")

    monkeypatch.setattr(pymssql, "connect", wrong_password)
    try:
        journal.open_sql("TrendBotMarket", 600, wait=0, log=lambda *_: None)
    except pymssql.OperationalError:
        pass
    assert calls.count("bad") == 1                                   # other errors aren't retried


def test_each_snapshot_gets_its_stocks_next_month_and_the_markets(tmp_path):
    import pandas as pd

    con = db.connect(tmp_path / "m.db")
    days = [str(d.date()) for d in pd.bdate_range("2026-09-01", periods=30)]
    for t, step in (("AAA", 0.01), ("BBB", -0.01), ("SPY", 0.002)):
        con.executemany("INSERT INTO prices(ticker, date, adj_close) VALUES (?, ?, ?)",
                        [(t, d, 100 * (1 + step) ** i) for i, d in enumerate(days)])
    con.executemany("INSERT INTO ranking_snapshots VALUES (?, ?, ?, 0, ?, 'x', 1)",
                    [(days[0], "AAA", 1, 1.0), (days[0], "BBB", 2, 0.5), (days[25], "AAA", 1, 1.0)])
    rows = {(r["snapshot_date"], r["ticker"]): r for r in journal.outcome_rows(con, [days[0], days[25]],
                                                                             today=dt.date(2026, 10, 20))}
    a = rows[(days[0], "AAA")]
    assert a["days"] == 21 and a["final"] == 1 and a["ret"] == pytest.approx(1.01 ** 21 - 1)
    assert a["market_ret"] == pytest.approx(1.002 ** 21 - 1) and rows[(days[0], "BBB")]["ret"] < 0
    late = rows[(days[25], "AAA")]
    assert late["days"] == 4 and late["final"] == 0          # only 4 days so far: not final yet
    assert (days[0], "SPY") not in rows                      # the market itself wasn't ranked
