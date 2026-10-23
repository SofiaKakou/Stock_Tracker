"""The trading journal in SQL Server (Azure SQL, free offer): everything the bot did, in tables you
can open in SSMS.

Written once a night by the nightly run (only that job needs access): each table is rebuilt for the
days it covers, so running it twice changes nothing. Nothing happens without the AZURE_SQL_SERVER,
AZURE_SQL_USER and AZURE_SQL_PASSWORD secrets (optional AZURE_SQL_DATABASE, default TrendBotTrading).

Tables (dbo.):
- day_trades       every day trade: pattern, side, shares, signal strength, price expected vs got
                   (slippage), exit, return, $ result
- day_summary      each day's result per pattern
- account_values   both demo accounts' value at each day's close (and the S&P 500 for comparison)
- monthly_holdings what the portfolio to follow held from each monthly update (bought = new that month)
- safety_events    emergency exits and warnings for the portfolio's stocks
- study_results    the latest 10-year test of each day-trading pattern
- runs             the bot's logbook: every command run, when, how it ended, and what made it
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3

TABLES = {
    "day_trades": """trade_date DATE NOT NULL, symbol VARCHAR(12) NOT NULL, pattern VARCHAR(20), side SMALLINT,
        qty FLOAT, signal FLOAT, expected FLOAT, entry FLOAT, exit_price FLOAT, slippage FLOAT, ret FLOAT, pnl FLOAT,
        entry_time VARCHAR(5), PRIMARY KEY (trade_date, symbol)""",
    "day_summary": """trade_date DATE NOT NULL, pattern VARCHAR(20) NOT NULL, pnl FLOAT, trades INT,
        PRIMARY KEY (trade_date, pattern)""",
    "account_values": """account VARCHAR(10) NOT NULL, value_date DATE NOT NULL, value FLOAT,
        PRIMARY KEY (account, value_date)""",
    "monthly_holdings": """portfolio_date DATE NOT NULL, ticker VARCHAR(12) NOT NULL, bought BIT,
        PRIMARY KEY (portfolio_date, ticker)""",
    "safety_events": """event_date DATE NOT NULL, ticker VARCHAR(12) NOT NULL, kind VARCHAR(10) NOT NULL,
        reason NVARCHAR(400) NOT NULL, PRIMARY KEY (event_date, ticker, kind)""",
    "study_results": """pattern VARCHAR(30) NOT NULL PRIMARY KEY, trades INT, days INT, per_trade FLOAT,
        yearly FLOAT, first_half FLOAT, second_half FLOAT, hit_rate FLOAT, t_stat FLOAT, luck FLOAT,
        verdict NVARCHAR(200), updated DATE""",
    "runs": """started VARCHAR(30) NOT NULL, command NVARCHAR(200) NOT NULL, finished VARCHAR(30), exit_code INT,
        error NVARCHAR(400), seconds FLOAT, code VARCHAR(64), data VARCHAR(64), PRIMARY KEY (started, command)""",
}
KEYS = {"day_trades": "trade_date", "day_summary": "trade_date", "account_values": "value_date",
        "monthly_holdings": "portfolio_date", "safety_events": "event_date", "runs": "started"}


def connect():
    """A connection to the journal database, or None when it isn't set up."""
    server, user, pw = (os.environ.get(k) for k in ("AZURE_SQL_SERVER", "AZURE_SQL_USER", "AZURE_SQL_PASSWORD"))
    if not (server and user and pw):
        return None
    import pymssql

    return pymssql.connect(server=server, user=user, password=pw,
                           database=os.environ.get("AZURE_SQL_DATABASE") or "TrendBotTrading",
                           login_timeout=60, timeout=300, tds_version="7.4")


def ensure_tables(conn) -> None:
    cur = conn.cursor()
    for name, cols in TABLES.items():
        cur.execute(f"IF OBJECT_ID('dbo.{name}', 'U') IS NULL CREATE TABLE dbo.{name} ({cols})")
    conn.commit()


def replace(conn, table: str, rows: list[dict]) -> int:
    """Write `rows` into `table`, replacing what's there for the same days (study_results: all of it)."""
    if not rows:
        return 0
    cur = conn.cursor()
    cols = list(rows[0])
    key = KEYS.get(table)
    if key:
        days = sorted({r[key] for r in rows})
        for i in range(0, len(days), 500):
            part = days[i:i + 500]
            cur.execute(f"DELETE FROM dbo.{table} WHERE {key} IN ({','.join(['%s'] * len(part))})", tuple(part))
    elif table == "study_results":
        cur.execute("DELETE FROM dbo.study_results")
    sql = f"INSERT INTO dbo.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
    cur.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    conn.commit()
    return len(rows)


# --- What goes in (built from what the bot already keeps; pure, so it's easy to test) ------------

def day_trade_rows(state: dict) -> list[dict]:
    return [{"trade_date": r["date"], "symbol": r["symbol"], "pattern": r.get("pattern"), "side": r.get("side"),
             "qty": r.get("qty"), "signal": r.get("signal"), "expected": r.get("expected"), "entry": r.get("entry"),
             "exit_price": r.get("exit"), "slippage": r.get("slippage"), "ret": r.get("ret"), "pnl": r.get("pnl"),
             "entry_time": r.get("time")} for r in state.get("log", [])]


def day_summary_rows(state: dict) -> list[dict]:
    return [{"trade_date": d, "pattern": p, "pnl": r["pnl"], "trades": r["trades"]}
            for d, day in (state.get("days") or {}).items() for p, r in day.items()]


def account_rows(hist: dict) -> list[dict]:
    return [{"account": name, "value_date": d, "value": v} for name, h in hist.items()
            for d, v in zip(h["dates"], h["values"])]


def holdings_rows(con: sqlite3.Connection) -> list[dict]:
    return [{"portfolio_date": s, "ticker": t, "bought": int(b)}
            for s, t, b in con.execute("SELECT start, ticker, bought FROM ideas_periods ORDER BY start, ticker")]


def safety_rows(found: dict, today: dt.date) -> list[dict]:
    return [{"event_date": today.isoformat(), "ticker": t, "kind": kind, "reason": why[:400]}
            for kind in ("exit", "warn") for t, why in found.get(kind, [])]


def study_rows(results: dict | None, today: dt.date) -> list[dict]:
    from trend_bot import intraday

    rows = []
    for name, r in (results or {}).items():
        if not r.get("days"):
            continue
        rows.append({"pattern": name, "trades": r["trades"], "days": r["days"], "per_trade": r["per_trade"],
                     "yearly": r["yearly"], "first_half": r["early"], "second_half": r["late"], "hit_rate": r["hit"],
                     "t_stat": r["t"], "luck": r["luck"], "verdict": intraday.verdict(r)[:200],
                     "updated": today.isoformat()})
    return rows


def run_rows(con: sqlite3.Connection, days: int = 14) -> list[dict]:
    """The logbook's last `days` days."""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = con.execute("SELECT started, command, finished, exit_code, error, seconds, code, data FROM run_log "
                       "WHERE started >= ?", (since,)).fetchall()
    seen, out = set(), []
    for s, c, f, e, err, sec, code, data in rows:
        key = (s, (c or "")[:200])
        if key in seen:
            continue
        seen.add(key)
        out.append({"started": s, "command": key[1], "finished": f, "exit_code": e, "error": (err or "")[:400],
                    "seconds": sec, "code": (code or "")[:64], "data": (data or "")[:64]})
    return out


def sync(conn, market: sqlite3.Connection, day_state: dict, intraday_results: dict | None,
         today: dt.date | None = None, log=print) -> dict[str, int]:
    """Write everything into the journal. Returns rows written per table."""
    from trend_bot import accounts
    from trend_bot.db import get_meta

    today = today or dt.date.today()
    ensure_tables(conn)
    found = json.loads(get_meta(market, "safety_last") or '{"exit": [], "warn": []}')
    written = {
        "day_trades": replace(conn, "day_trades", day_trade_rows(day_state)),
        "day_summary": replace(conn, "day_summary", day_summary_rows(day_state)),
        "account_values": replace(conn, "account_values", account_rows(accounts.load(market))),
        "monthly_holdings": replace(conn, "monthly_holdings", holdings_rows(market)),
        "safety_events": replace(conn, "safety_events", safety_rows(found, today)) if found.get("date") == today.isoformat() else 0,
        "study_results": replace(conn, "study_results", study_rows(intraday_results, today)),
        "runs": replace(conn, "runs", run_rows(market)),
    }
    log("[journal] " + ", ".join(f"{t} {n:,}" for t, n in written.items()))
    return written
