"""The market data in SQL Server too (database TrendBotMarket, same Azure server as the journal), so
20 years of prices can be queried in SSMS. The bot itself keeps working from its own file (market.db);
this is a copy, refreshed after each nightly run (market-mirror.yml).

Tables (dbo.): prices (every stock, every day: open, high, low, close, adjusted close, volume) and
tickers (name, exchange, latest price, how much it trades).

Incremental: each night only the new days are sent. When a stock's history was re-adjusted since
(a split: Yahoo re-sends all of its old prices), its whole history is sent again. The first fill is
~30 million rows; it's committed as it goes, so a run that stops is simply continued by the next.
"""

from __future__ import annotations

import os
import sqlite3
import time

PRICES = """ticker VARCHAR(12) NOT NULL, price_date DATE NOT NULL, [open] FLOAT, high FLOAT, low FLOAT, [close] FLOAT,
    adj_close FLOAT, volume FLOAT, PRIMARY KEY (ticker, price_date)"""
TICKERS = """ticker VARCHAR(12) NOT NULL PRIMARY KEY, cik INT, name NVARCHAR(300), exchange VARCHAR(40),
    first_date DATE, last_date DATE, last_close FLOAT, dollar_vol FLOAT"""
ROWS_PER_INSERT = 1000      # SQL Server's limit for one INSERT ... VALUES
TOLERANCE = 1e-6


def connect():
    from trend_bot.journal import open_sql

    return open_sql(os.environ.get("AZURE_SQL_MARKET_DATABASE") or "TrendBotMarket", timeout=600)


def ensure_tables(conn) -> None:
    cur = conn.cursor()
    cur.execute(f"IF OBJECT_ID('dbo.prices', 'U') IS NULL CREATE TABLE dbo.prices ({PRICES})")
    cur.execute(f"IF OBJECT_ID('dbo.tickers', 'U') IS NULL CREATE TABLE dbo.tickers ({TICKERS})")
    conn.commit()


def insert(cur, table: str, cols: str, rows: list[tuple]) -> None:
    """Many rows per statement (much faster than one at a time over the internet)."""
    width = len(rows[0]) if rows else 0
    for i in range(0, len(rows), ROWS_PER_INSERT):
        part = rows[i:i + ROWS_PER_INSERT]
        group = "(" + ", ".join(["%s"] * width) + ")"
        cur.execute(f"INSERT INTO dbo.{table} ({cols}) VALUES " + ", ".join([group] * len(part)),
                    tuple(v for r in part for v in r))


def plan(local: sqlite3.Connection, remote_last: dict[str, tuple[str, float]]) -> tuple[dict[str, str | None], int]:
    """Per ticker, what to send: {ticker: None (everything) or the last date already there}."""
    out, full = {}, 0
    for t, last in local.execute("SELECT ticker, MAX(date) FROM prices GROUP BY ticker"):
        have = remote_last.get(t)
        if have is None:
            out[t], full = None, full + 1
            continue
        row = local.execute("SELECT adj_close FROM prices WHERE ticker = ? AND date = ?", (t, have[0])).fetchone()
        same = row is not None and row[0] is not None and have[1] is not None and \
            abs(row[0] - have[1]) <= TOLERANCE * max(1.0, abs(row[0]))
        if not same:                                  # re-adjusted (a split) or missing: send it all again
            out[t], full = None, full + 1
        elif last > have[0]:
            out[t] = have[0]
    return out, full


def sync(conn, local: sqlite3.Connection, log=print, budget_seconds: float = 4 * 3600) -> dict[str, int]:
    """Send what's new. Stops cleanly after `budget_seconds` (the next run carries on)."""
    ensure_tables(conn)
    cur = conn.cursor()
    cur.execute("SELECT p.ticker, CONVERT(VARCHAR(10), p.price_date, 23), p.adj_close FROM dbo.prices p JOIN "
                "(SELECT ticker, MAX(price_date) AS d FROM dbo.prices GROUP BY ticker) m "
                "ON p.ticker = m.ticker AND p.price_date = m.d")
    remote_last = {t: (d, v) for t, d, v in cur.fetchall()}
    todo, full = plan(local, remote_last)
    log(f"[mirror] {len(todo):,} stocks to update ({full:,} from scratch)")
    started, sent, done = time.time(), 0, 0
    for t, after in sorted(todo.items()):
        if time.time() - started > budget_seconds:
            log("[mirror] time's up for tonight; the next run continues")
            break
        if after is None:
            cur.execute("DELETE FROM dbo.prices WHERE ticker = %s", (t,))
            rows = local.execute("SELECT ticker, date, open, high, low, close, adj_close, volume FROM prices "
                                 "WHERE ticker = ? ORDER BY date", (t,)).fetchall()
        else:
            rows = local.execute("SELECT ticker, date, open, high, low, close, adj_close, volume FROM prices "
                                 "WHERE ticker = ? AND date > ? ORDER BY date", (t, after)).fetchall()
        if rows:
            insert(cur, "prices", "ticker, price_date, [open], high, low, [close], adj_close, volume", rows)
        conn.commit()
        sent += len(rows)
        done += 1
        if done % 200 == 0:
            log(f"[mirror] {done:,}/{len(todo):,} stocks, {sent:,} rows ({sent / max(time.time() - started, 1):,.0f}/s)")
    cur.execute("DELETE FROM dbo.tickers")
    tick = local.execute("SELECT ticker, cik, name, exchange, first_date, last_date, last_close, dollar_vol "
                         "FROM tickers").fetchall()
    if tick:
        insert(cur, "tickers", "ticker, cik, name, exchange, first_date, last_date, last_close, dollar_vol", tick)
    conn.commit()
    log(f"[mirror] done: {sent:,} price rows for {done:,} stocks, {len(tick):,} tickers")
    return {"stocks": done, "rows": sent, "tickers": len(tick)}
