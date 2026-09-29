"""Filling the market database with the stock universe and daily prices."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from typing import Callable

import pandas as pd

from trend_bot import insiders
from trend_bot.db import set_meta

UNIVERSE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
MAIN_EXCHANGES = ("NYSE", "Nasdaq", "CBOE")
# Always tracked (used as the market benchmark), even if the SEC list lacks them.
BENCHMARKS = {"SPY": "SPDR S&P 500 ETF", "QQQ": "Invesco QQQ (Nasdaq-100)", "IWM": "iShares Russell 2000"}

Downloader = Callable[..., dict[str, pd.DataFrame]]

# Suffixes the SEC list uses for things that aren't common shares.
NON_STOCK_SUFFIXES = ("WT", "WS", "W", "U", "UN", "R", "RT", "RI")


def is_common_stock(ticker: str) -> bool:
    """False for warrants, units, rights and preferred shares (e.g. AACPW, AAC-WT, BAC-PL).

    Class shares like BRK-B stay in.
    """
    t = ticker.upper()
    if "-" in t:
        suffix = t.rsplit("-", 1)[1]
        return not (suffix in NON_STOCK_SUFFIXES or suffix.startswith("P"))
    # Nasdaq 5-letter codes: 5th letter W = warrant, U = unit, R = rights, Z = other.
    return not (len(t) == 5 and t[-1] in "WURZ")



# --- Universe ------------------------------------------------------------------

def update_universe(con: sqlite3.Connection, include_otc: bool = False, all_securities: bool = False) -> int:
    """Refresh the ticker list from the SEC. Returns the number of tickers tracked.

    Warrants, units, rights and preferred shares are left out unless all_securities is set.
    """
    raw = insiders._cached(insiders.CACHE_DIR / "company_tickers_exchange.json", UNIVERSE_URL, max_age_hours=24)
    doc = json.loads(raw)
    fields = doc["fields"]
    rows = []
    for rec in doc["data"]:
        r = dict(zip(fields, rec))
        exchange = r.get("exchange") or ""
        if not r.get("ticker") or (exchange not in MAIN_EXCHANGES and not (include_otc and exchange == "OTC")):
            continue
        if not all_securities and not is_common_stock(r["ticker"]):
            continue
        rows.append((r["ticker"].upper(), int(r["cik"]), r.get("name"), exchange))
    for t, name in BENCHMARKS.items():
        rows.append((t, None, name, "ETF"))
    if not all_securities:
        # Drop non-stocks added by earlier versions (and their prices).
        junk = [(tk,) for (tk,) in con.execute("SELECT ticker FROM tickers") if not is_common_stock(tk)]
        con.executemany("DELETE FROM prices WHERE ticker = ?", junk)
        con.executemany("DELETE FROM tickers WHERE ticker = ?", junk)
    con.executemany(
        """INSERT INTO tickers(ticker, cik, name, exchange) VALUES (?, ?, ?, ?)
           ON CONFLICT(ticker) DO UPDATE SET
               cik = COALESCE(excluded.cik, tickers.cik), name = excluded.name, exchange = excluded.exchange""",
        rows,
    )
    set_meta(con, "universe_updated", dt.datetime.now().isoformat(timespec="seconds"))
    con.commit()
    return len(rows)


# --- Downloading -----------------------------------------------------------------

def yahoo_download(tickers: list[str], start: str | None = None, period: str | None = None) -> dict[str, pd.DataFrame]:
    """Raw daily bars for many tickers: {ticker: DataFrame[Open, High, Low, Close, Adj Close, Volume]}."""
    import yfinance as yf

    raw = yf.download(tickers, start=start, period=None if start else period, interval="1d",
                      auto_adjust=False, actions=False, group_by="ticker", threads=True, progress=False)
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out
    if isinstance(raw.columns, pd.MultiIndex):
        for t in raw.columns.get_level_values(0).unique():
            df = raw[t].dropna(how="all")
            if not df.empty:
                out[str(t).upper()] = df
    elif len(tickers) == 1:
        out[tickers[0].upper()] = raw.dropna(how="all")
    return out


def _rows(ticker: str, df: pd.DataFrame) -> list[tuple]:
    df = df.dropna(subset=["Close"])
    adj = df["Adj Close"] if "Adj Close" in df else df["Close"]
    return [
        (ticker, d.strftime("%Y-%m-%d"), o, h, l, c, a, v)
        for d, o, h, l, c, a, v in zip(df.index, df["Open"], df["High"], df["Low"], df["Close"], adj, df["Volume"])
    ]


def _needs_reload(con: sqlite3.Connection, ticker: str, df: pd.DataFrame) -> bool:
    """True if Yahoo's adjusted history changed (new dividend or split) on overlapping days."""
    if df.empty:
        return False
    first = df.index[0].strftime("%Y-%m-%d")
    stored = dict(con.execute("SELECT date, adj_close FROM prices WHERE ticker = ? AND date >= ?", (ticker, first)))
    adj = df["Adj Close"] if "Adj Close" in df else df["Close"]
    for d, a in zip(df.index, adj):
        old = stored.get(d.strftime("%Y-%m-%d"))
        if old and a and abs(a / old - 1) > 1e-4:
            return True
    return False


def _save(con: sqlite3.Connection, ticker: str, df: pd.DataFrame, replace_all: bool) -> None:
    if replace_all:
        con.execute("DELETE FROM prices WHERE ticker = ?", (ticker,))
    con.executemany("INSERT OR REPLACE INTO prices VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _rows(ticker, df))
    tail = con.execute(
        "SELECT close, adj_close, volume FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 20", (ticker,)
    ).fetchall()
    first, last = con.execute("SELECT MIN(date), MAX(date) FROM prices WHERE ticker = ?", (ticker,)).fetchone()
    dollar_vol = sum((c or 0) * (v or 0) for c, _, v in tail) / max(len(tail), 1)
    con.execute(
        """UPDATE tickers SET first_date = ?, last_date = ?, last_close = ?, dollar_vol = ?,
           fail_count = 0, updated_at = ? WHERE ticker = ?""",
        (first, last, tail[0][1] if tail else None, dollar_vol, dt.datetime.now().isoformat(timespec="seconds"), ticker),
    )


def update_prices(
    con: sqlite3.Connection,
    period: str = "max",
    batch_size: int = 100,
    pause: float = 1.0,
    max_age_hours: float = 6,
    retry_failed: bool = False,
    limit: int | None = None,
    downloader: Downloader = yahoo_download,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    """Download new bars for every ticker not refreshed in the last `max_age_hours`.

    Progress is committed after every batch, so an interrupted run simply
    continues where it stopped next time.
    """
    cutoff = (dt.datetime.now() - dt.timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
    q = "SELECT ticker, last_date FROM tickers WHERE (updated_at IS NULL OR updated_at < ?)"
    if not retry_failed:
        q += " AND fail_count < 3"
    todo = con.execute(q + " ORDER BY last_date IS NOT NULL, ticker", [cutoff]).fetchall()
    if limit:
        todo = todo[:limit]
    counts = {"updated": 0, "new": 0, "reloaded": 0, "failed": 0}
    if not todo:
        log("[prices] everything is up to date")
        return counts

    started, done, backoff = time.time(), 0, 0
    i = 0
    while i < len(todo):
        chunk = todo[i : i + batch_size]
        new = [t for t, last in chunk if last is None]
        old = [(t, last) for t, last in chunk if last is not None]
        got: dict[str, pd.DataFrame] = {}
        try:
            if new:
                got.update(downloader(new, period=period))
            if old:
                start = (dt.date.fromisoformat(min(last for _, last in old)) - dt.timedelta(days=7)).isoformat()
                got.update(downloader([t for t, _ in old], start=start))
        except Exception as e:  # network trouble: treat like an empty batch
            log(f"[prices] download error: {e}")
            got = {}

        if not got:
            backoff += 1
            if backoff > 3:
                log("[prices] Yahoo keeps returning nothing (probably rate-limiting). "
                    "Progress is saved - run the update again later.")
                break
            wait = 30 * 2 ** (backoff - 1)
            log(f"[prices] empty batch, waiting {wait}s before retrying...")
            time.sleep(wait)
            continue
        backoff = 0

        reload = []
        for t, last in chunk:
            df = got.get(t)
            if df is None or df.dropna(subset=["Close"]).empty:
                con.execute("UPDATE tickers SET fail_count = fail_count + 1, updated_at = ? WHERE ticker = ?",
                            (dt.datetime.now().isoformat(timespec="seconds"), t))
                counts["failed"] += 1
            elif last is not None and _needs_reload(con, t, df):
                reload.append(t)
            else:
                _save(con, t, df, replace_all=False)
                counts["new" if last is None else "updated"] += 1
        if reload:
            full = downloader(reload, period=period)
            for t in reload:
                if t in full:
                    _save(con, t, full[t], replace_all=True)
                    counts["reloaded"] += 1
        con.commit()

        i += len(chunk)
        done += len(chunk)
        rate = done / max(time.time() - started, 1e-9)
        eta = (len(todo) - done) / rate if rate else 0
        log(f"[prices] {done}/{len(todo)} tickers  (~{eta / 60:.0f} min left)")
        if i < len(todo) and pause:
            time.sleep(pause)

    set_meta(con, "prices_updated", dt.datetime.now().isoformat(timespec="seconds"))
    con.commit()
    return counts
