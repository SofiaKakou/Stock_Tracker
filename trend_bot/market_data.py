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
BENCHMARKS = {"SPY": "SPDR S&P 500 ETF", "QQQ": "Invesco QQQ (Nasdaq-100)", "IWM": "iShares Russell 2000",
              "RSP": "Invesco S&P 500 Equal Weight ETF"}

Downloader = Callable[..., dict[str, pd.DataFrame]]

# Suffixes the SEC list uses for things that aren't common shares.
NON_STOCK_SUFFIXES = ("WT", "WS", "W", "U", "UN", "R", "RT", "RI", "RW")
# Nasdaq 5th-letter codes for non-common securities: convertible bonds (G/H/I), preferreds
# (M/N/O/P), rights (R), with-warrants (T), units (U), warrants (W), and misc (Z).
# Not L: real share classes use it too (GOOGL).
NASDAQ_NON_STOCK = set("GHIMNOPRTUWZ")


def is_common_stock(ticker: str) -> bool:
    """False for warrants, units, rights and preferred shares (e.g. AACPW, AAC-WT, BAC-PL).

    Class shares like BRK-B stay in.
    """
    t = ticker.upper()
    if "-" in t:
        suffix = t.rsplit("-", 1)[1]
        return not (suffix in NON_STOCK_SUFFIXES or suffix.startswith("P"))
    return not (len(t) == 5 and t[-1] in NASDAQ_NON_STOCK)



# --- Universe ------------------------------------------------------------------

# Nasdaq's daily lists of every US-listed security, with an ETF flag (covers ETFs and ETNs).
SYMBOL_LISTS = {
    "nasdaq": ("https://www.nasdaqtrader.com/dynamic/SymbDir/nasdaqlisted.txt", "Symbol"),
    "other": ("https://www.nasdaqtrader.com/dynamic/SymbDir/otherlisted.txt", "ACT Symbol"),
}


def parse_symbol_list(text: str, symbol_col: str) -> dict[str, tuple[bool, str]]:
    """{ticker: (is ETF/ETN, security name)} from a Nasdaq Trader symbol directory file."""
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("File Creation Time")]
    if not lines:
        return {}
    header = lines[0].split("|")
    if symbol_col not in header or "ETF" not in header:
        return {}
    i_sym, i_etf, i_name = header.index(symbol_col), header.index("ETF"), header.index("Security Name")
    out = {}
    for ln in lines[1:]:
        f = ln.split("|")
        if len(f) < len(header):
            continue
        out[f[i_sym].strip().upper().replace(".", "-")] = (f[i_etf].strip() == "Y", f[i_name].strip())
    return out


FTP_HOST = "ftp.nasdaqtrader.com"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) trend_bot"


def _http_fetch(url: str) -> bytes:
    import requests

    resp = requests.get(url, timeout=30, headers={"User-Agent": BROWSER_UA})
    resp.raise_for_status()
    return resp.content


def _ftp_fetch(url: str) -> bytes:
    """Same file from Nasdaq's FTP server (SymbolDirectory/<name>)."""
    import ftplib
    import io

    buf = io.BytesIO()
    with ftplib.FTP(FTP_HOST, timeout=30) as ftp:
        ftp.login()
        ftp.retrbinary(f"RETR SymbolDirectory/{url.rsplit('/', 1)[-1]}", buf.write)
    return buf.getvalue()


def update_etf_flags(con: sqlite3.Connection, fetch: Callable[[str], bytes] | None = None,
                     log: Callable[[str], None] = print) -> int:
    """Mark ETFs/ETNs (e.g. leveraged products filed under a bank) using Nasdaq's symbol lists.

    Tries the website, then Nasdaq's FTP server. Always reports what happened;
    if neither works, nothing changes.
    """
    sources = [fetch] if fetch else [_http_fetch, _ftp_fetch]
    flags: dict[str, tuple[bool, str]] = {}
    for url, col in SYMBOL_LISTS.values():
        name = url.rsplit("/", 1)[-1]
        problems = []
        for source in sources:
            try:
                raw = source(url).decode("utf-8-sig", "replace")
            except Exception as e:
                problems.append(f"{e}")
                continue
            parsed = parse_symbol_list(raw, col)
            if parsed:
                flags.update(parsed)
                break
            first = raw.strip().splitlines()[0][:60] if raw.strip() else "(empty)"
            problems.append(f"unexpected content: {first!r}")
        else:
            log(f"[universe] couldn't read Nasdaq's {name} ({'; '.join(problems)}); ETF/ETN flags not updated")
            return 0
    rows = [(int(is_etf), sec_name, t) for t, (is_etf, sec_name) in flags.items()]
    con.executemany("UPDATE tickers SET is_etf = ?, security_name = ? WHERE ticker = ?", rows)
    con.commit()
    matched = con.execute("SELECT COUNT(*), SUM(is_etf) FROM tickers WHERE is_etf IS NOT NULL").fetchone()
    log(f"[universe] Nasdaq lists: {len(flags):,} securities read; {matched[0]:,} of our tickers matched, "
        f"{matched[1] or 0:,} are ETFs/ETNs (left out of screens and models)")
    return int(matched[1] or 0)


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
    """Raw daily bars for many tickers: {ticker: DataFrame[Open, High, Low, Close, Adj Close, Volume, Stock Splits]}."""
    import yfinance as yf

    raw = yf.download(tickers, start=start, period=None if start else period, interval="1d",
                      auto_adjust=False, actions=True, group_by="ticker", threads=True, progress=False)
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


# --- Backup source: Stooq (free CSV) when Yahoo fails --------------------------------------------

STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}&d1={start}&i=d"
BACKUP_LIMIT = 300        # stocks per run: Stooq is a small free service, so be gentle
BACKUP_MATCH = 0.02       # Stooq's close on our last stored day must be within 2% of ours


def stooq_symbol(ticker: str) -> str:
    return ticker.lower().replace(".", "-") + ".us"


def stooq_download(ticker: str, start: str) -> pd.DataFrame:
    """Daily bars from Stooq since `start` (YYYY-MM-DD): [Open, High, Low, Close, Volume]."""
    import io

    import requests

    url = STOOQ_URL.format(symbol=stooq_symbol(ticker), start=start.replace("-", ""))
    resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 trend-bot"})
    resp.raise_for_status()
    text = resp.text.strip()
    if not text.lower().startswith("date"):
        return pd.DataFrame()   # "No data" or an error page
    df = pd.read_csv(io.StringIO(text), parse_dates=["Date"]).set_index("Date")
    return df[[c for c in ("Open", "High", "Low", "Close", "Volume") if c in df]].dropna(subset=["Close"])


def backup_priority(con: sqlite3.Connection, tickers: list[str]) -> list[str]:
    """Most important first: benchmark funds, the portfolios' stocks, then the most traded."""
    held = {t for (t,) in con.execute("SELECT ticker FROM ideas_holdings UNION SELECT ticker FROM model_holdings")}
    vol = dict(con.execute("SELECT ticker, COALESCE(dollar_vol, 0) FROM tickers"))
    rank = lambda t: (t not in BENCHMARKS, t not in held, -vol.get(t, 0))
    return sorted(tickers, key=rank)


def fill_from_backup(con: sqlite3.Connection, tickers: list[str], fetch=stooq_download,
                     limit: int = BACKUP_LIMIT, log: Callable[[str], None] = print) -> int:
    """Add the days missing since each ticker's last stored date from the backup source. Only for
    tickers we already have; skipped when the backup's price on our last day doesn't match ours
    (different split adjustment). Dividend adjustment carries on from our last stored day; the next
    time Yahoo works it sees the difference and reloads the ticker from Yahoo."""
    filled = 0
    for t in backup_priority(con, tickers)[:limit]:
        last = con.execute("SELECT date, close, adj_close FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                           (t,)).fetchone()
        if not last:
            continue
        try:
            df = fetch(t, last[0])
        except Exception:  # noqa: BLE001 - a backup that fails just leaves the ticker as it was
            continue
        if df.empty or pd.Timestamp(last[0]) not in df.index:
            continue
        theirs = float(df.loc[pd.Timestamp(last[0]), "Close"])
        if not theirs or abs(theirs / last[1] - 1) > BACKUP_MATCH:
            continue
        new = df[df.index > pd.Timestamp(last[0])]
        if new.empty:
            continue
        ratio = (last[2] / last[1]) if last[1] else 1.0
        rows = [(t, d.strftime("%Y-%m-%d"), r.Open, r.High, r.Low, r.Close, r.Close * ratio, r.Volume)
                for d, r in new.iterrows()]
        con.executemany("INSERT OR REPLACE INTO prices VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute("UPDATE tickers SET last_date = ?, last_close = ?, updated_at = ?, fail_count = 0 WHERE ticker = ?",
                    (rows[-1][1], rows[-1][5], dt.datetime.now().isoformat(timespec="seconds"), t))
        filled += 1
    con.commit()
    if filled:
        log(f"[prices] {filled:,} stock(s) filled in from Stooq (backup source)")
    return filled


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


def _save_splits(con: sqlite3.Connection, ticker: str, df: pd.DataFrame, full_history: bool) -> None:
    if "Stock Splits" not in df:
        return
    if full_history:
        con.execute("DELETE FROM splits WHERE ticker = ?", (ticker,))
    s = df["Stock Splits"].fillna(0)
    rows = [(ticker, d.strftime("%Y-%m-%d"), float(r)) for d, r in s[s > 0].items()]
    con.executemany("INSERT OR REPLACE INTO splits VALUES (?, ?, ?)", rows)
    if full_history:
        con.execute("UPDATE tickers SET splits_checked = 1 WHERE ticker = ?", (ticker,))


def backfill_splits(con: sqlite3.Connection, downloader: Downloader = yahoo_download, batch_size: int = 100,
                    pause: float = 1.0, limit: int | None = None, log: Callable[[str], None] = print) -> int:
    """One-time: record past stock splits for tickers whose full history was saved before splits were kept."""
    todo = [t for (t,) in con.execute(
        "SELECT ticker FROM tickers WHERE last_date IS NOT NULL AND splits_checked IS NULL ORDER BY ticker")]
    if limit:
        todo = todo[:limit]
    if not todo:
        return 0
    log(f"[splits] looking up past stock splits for {len(todo):,} tickers (one time)...")
    for i in range(0, len(todo), batch_size):
        chunk = todo[i : i + batch_size]
        try:
            got = downloader(chunk, period="max")
        except Exception as e:
            log(f"[splits] download error: {e}; will continue next run")
            break
        for t in chunk:
            if t in got:
                _save_splits(con, t, got[t], full_history=True)
        con.commit()
        log(f"[splits] {min(i + batch_size, len(todo)):,}/{len(todo):,}")
        if pause and i + batch_size < len(todo):
            time.sleep(pause)
    return len(todo)


def split_factor_after(splits: pd.DataFrame, ticker: str, dates: pd.DatetimeIndex) -> pd.Series:
    """Product of split ratios strictly after each date: turns a split-adjusted price into the actual one."""
    s = splits[splits["ticker"] == ticker]
    out = pd.Series(1.0, index=dates)
    for d, r in zip(pd.to_datetime(s["date"]), s["ratio"]):
        out[dates < d] *= r
    return out


def _save(con: sqlite3.Connection, ticker: str, df: pd.DataFrame, replace_all: bool) -> None:
    _save_splits(con, ticker, df, full_history=replace_all)
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
    backup=None,
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
    counts = {"updated": 0, "new": 0, "reloaded": 0, "failed": 0, "backup": 0}
    missed: list[str] = []   # known tickers Yahoo didn't deliver: tried from the backup source at the end
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
                missed += [t for t, last in todo[i:] if last is not None]
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
                if last is not None:
                    missed.append(t)
            elif last is not None and _needs_reload(con, t, df):
                reload.append(t)
            else:
                _save(con, t, df, replace_all=last is None)  # a new ticker gets its full history
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

    if missed and backup is not None:
        counts["backup"] = fill_from_backup(con, missed, fetch=backup, log=log)
    set_meta(con, "prices_updated", dt.datetime.now().isoformat(timespec="seconds"))
    con.commit()
    return counts
