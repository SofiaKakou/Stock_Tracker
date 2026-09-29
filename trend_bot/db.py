"""Local market database (a single SQLite file, market.db by default).

Tables
------
tickers        the stock universe (from the SEC's ticker list) + download bookkeeping
prices         daily OHLCV per ticker. OHLC are split-adjusted but not
               dividend-adjusted (Yahoo's raw "Close"); adj_close includes
               dividends. Readers get fully adjusted prices via adj_close/close.
insider_trades one row per Form 4 transaction (open-market and otherwise)
meta           key/value bookkeeping (which SEC quarters/days are loaded)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

DEFAULT_DB = "market.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickers (
    ticker      TEXT PRIMARY KEY,
    cik         INTEGER,
    name        TEXT,
    exchange    TEXT,
    first_date  TEXT,
    last_date   TEXT,
    last_close  REAL,
    dollar_vol  REAL,            -- average daily $ volume over the last 20 bars
    fail_count  INTEGER DEFAULT 0,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS tickers_cik ON tickers(cik);

CREATE TABLE IF NOT EXISTS prices (
    ticker    TEXT NOT NULL,
    date      TEXT NOT NULL,
    open      REAL, high REAL, low REAL, close REAL, adj_close REAL, volume REAL,
    PRIMARY KEY (ticker, date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS insider_trades (
    accession   TEXT NOT NULL,
    line        INTEGER NOT NULL,
    cik         INTEGER,         -- issuer (company) CIK
    ticker      TEXT,
    insider     TEXT,
    role        TEXT,
    code        TEXT,            -- P = open-market buy, S = open-market sale, ...
    date        TEXT,            -- transaction date
    filed       TEXT,            -- date the filing became public
    shares      REAL,
    price       REAL,
    owned_after REAL,
    planned     INTEGER,         -- 1 = 10b5-1 pre-scheduled plan
    PRIMARY KEY (accession, line)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS insider_ticker ON insider_trades(ticker, filed);
CREATE INDEX IF NOT EXISTS insider_code ON insider_trades(code, filed);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def _adjust(df: pd.DataFrame) -> pd.DataFrame:
    """Raw rows -> fully adjusted OHLCV with the same columns load_prices returns."""
    factor = (df["adj_close"] / df["close"]).where(df["close"] > 0, 1.0).fillna(1.0)
    out = pd.DataFrame({
        "Open": df["open"] * factor, "High": df["high"] * factor, "Low": df["low"] * factor,
        "Close": df["adj_close"].fillna(df["close"]), "Volume": df["volume"],
    })
    return out


def load_prices(con: sqlite3.Connection, ticker: str, start: str | None = None) -> pd.DataFrame:
    """One ticker's adjusted prices, like data.load_prices returns."""
    q = "SELECT date, open, high, low, close, adj_close, volume FROM prices WHERE ticker = ?"
    params: list = [ticker.upper()]
    if start:
        q += " AND date >= ?"
        params.append(start)
    df = pd.read_sql_query(q + " ORDER BY date", con, params=params, parse_dates=["date"])
    if df.empty:
        raise ValueError(f"{ticker} is not in the database")
    return _adjust(df.set_index("date").rename_axis("Date")).dropna(subset=["Close"])


def load_many(con: sqlite3.Connection, tickers: list[str] | None = None, start: str | None = None) -> dict[str, pd.DataFrame]:
    """Adjusted prices for many tickers at once: {ticker: DataFrame}."""
    q = "SELECT ticker, date, open, high, low, close, adj_close, volume FROM prices WHERE 1=1"
    params: list = []
    if start:
        q += " AND date >= ?"
        params.append(start)
    if tickers is not None:
        q += f" AND ticker IN ({','.join('?' * len(tickers))})"
        params.extend(t.upper() for t in tickers)
    df = pd.read_sql_query(q + " ORDER BY ticker, date", con, params=params, parse_dates=["date"])
    out = {}
    for t, g in df.groupby("ticker", sort=False):
        out[t] = _adjust(g.set_index("date").rename_axis("Date")).dropna(subset=["Close"])
    return out


def status(con: sqlite3.Connection) -> dict:
    one = lambda q: con.execute(q).fetchone()
    n_tickers, n_with_prices = one("SELECT COUNT(*), SUM(last_date IS NOT NULL) FROM tickers")
    n_rows = one("SELECT COUNT(*) FROM prices")[0]
    first, last = one("SELECT MIN(first_date), MAX(last_date) FROM tickers")
    n_trades, ins_first, ins_last = one("SELECT COUNT(*), MIN(filed), MAX(filed) FROM insider_trades")
    n_buys = one("SELECT COUNT(*) FROM insider_trades WHERE code = 'P'")[0]
    return {
        "tickers": n_tickers, "tickers_with_prices": n_with_prices or 0,
        "price_rows": n_rows, "price_first": first, "price_last": last,
        "insider_trades": n_trades, "insider_buys": n_buys, "insider_first": ins_first, "insider_last": ins_last,
        "failed_tickers": one("SELECT COUNT(*) FROM tickers WHERE fail_count >= 3")[0],
    }
