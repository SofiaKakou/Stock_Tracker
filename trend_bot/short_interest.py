"""Short selling data from FINRA (free, no key).

Two kinds of data:

- Short interest: how many shares of each stock were sold short and not yet
  bought back, reported twice a month (around the 15th and the month end, the
  "settlement date"). FINRA publishes each report roughly 7 business days later,
  so the studies only use a report from 12 calendar days after its date.
- Short volume: each day, how much of the trading through FINRA's off-exchange
  facilities was short selling (consolidated files from August 2018). Kept as
  monthly totals per stock.

Research has found that heavily shorted stocks, and stocks whose short interest
jumps, tend to do worse afterwards: short sellers are often well-informed.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from typing import Callable

import numpy as np
import pandas as pd

from trend_bot.db import get_meta, set_meta

SI_URL = "https://cdn.finra.org/equity/otcmarket/biweekly/shrt{date:%Y%m%d}.csv"
SV_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date:%Y%m%d}.txt"
SI_FIRST = dt.date(2017, 12, 1)      # FINRA's site has reports from Dec 29, 2017 on
SV_FIRST = dt.date(2018, 8, 1)       # consolidated daily short volume starts here
PUBLISH_LAG_DAYS = 12                # report date -> publicly available (conservative)

Fetch = Callable[[str], "bytes | None"]


def symbol(s: str) -> str:
    """FINRA symbol -> the database's ticker style (BRK.B / BRK/B -> BRK-B)."""
    return s.strip().upper().replace(".", "-").replace("/", "-").replace(" ", "-")


def match(s: str, keep: set[str] | None) -> str | None:
    """Ticker in our database for a FINRA symbol, or None.

    Short interest files write class shares without a separator (BRKB, BFA): try BRK-B, BF-A.
    """
    t = symbol(s)
    if keep is None or t in keep:
        return t
    if len(t) > 1 and f"{t[:-1]}-{t[-1]}" in keep:
        return f"{t[:-1]}-{t[-1]}"
    return None


def download(url: str, timeout: float = 60) -> bytes | None:
    """File contents, or None if FINRA doesn't have it (it answers 403 for missing files)."""
    import requests

    for attempt in range(3):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 trend-bot"})
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (403, 404):
            return None
        if r.status_code == 200:
            return r.content
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"FINRA download kept failing: {url}")


# --- Short interest ------------------------------------------------------------------------

def _weekday_on_or_before(d: dt.date) -> dt.date:
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def report_targets(start: dt.date, end: dt.date) -> list[dt.date]:
    """The scheduled report dates (15th and last day of each month, moved back to a weekday).

    A holiday can push the real date a few days earlier; update_short_interest tries those too.
    """
    out = []
    m = dt.date(start.year, start.month, 1)
    while m <= end:
        nxt = (m + dt.timedelta(days=32)).replace(day=1)
        for d in (m.replace(day=15), nxt - dt.timedelta(days=1)):
            d = _weekday_on_or_before(d)
            if start <= d <= end:
                out.append(d)
        m = nxt
    return out


def _rows(text: str):
    """Rows of a pipe-separated FINRA file as dicts.

    Split by hand rather than with the csv module: some older files have a stray quote
    inside a company name, which makes a csv reader swallow the rest of the file.
    """
    lines = text.splitlines()
    if not lines:
        return
    clean = lambda v: v.strip().strip('"').strip()
    header = [clean(h) for h in lines[0].split("|")]
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) != len(header):
            continue  # e.g. the record count at the end of short volume files
        yield dict(zip(header, (clean(p) for p in parts)))


def parse_short_interest(data: bytes, keep: set[str] | None = None) -> list[tuple]:
    """(ticker, settle, short, avg_volume) rows from one FINRA short interest file."""
    text = data.decode("utf-8-sig", "replace")
    rows = []
    for r in _rows(text):
        try:
            t = match(r["symbolCode"], keep)
            short = float(r["currentShortPositionQuantity"])
            settle = r["settlementDate"].strip()
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if t is None:
            continue
        try:
            vol = float(r.get("averageDailyVolumeQuantity") or "nan")
        except ValueError:
            vol = float("nan")
        rows.append((t, settle, short, vol))
    return rows


def update_short_interest(con: sqlite3.Connection, fetch: Fetch | None = None, today: dt.date | None = None,
                          start: dt.date | None = None, pause: float = 0.2, log: Callable[[str], None] = print) -> int:
    """Download every short interest report not loaded yet. Returns the number of reports added."""
    fetch, today, start = fetch or download, today or dt.date.today(), start or SI_FIRST
    keep = {t for (t,) in con.execute("SELECT ticker FROM tickers")}
    tried = {r[0] for r in con.execute("SELECT target FROM short_interest_files")}
    added = 0
    for target in report_targets(start, today - dt.timedelta(days=7)):
        key = target.isoformat()
        if key in tried:
            continue
        found = None
        for back in range(5):  # holidays move the date a little earlier
            d = _weekday_on_or_before(target - dt.timedelta(days=back))
            data = fetch(SI_URL.format(date=d))
            time.sleep(pause)
            if data:
                found = d
                break
        if found is None:
            # Recent reports may simply not be out yet; only give up on older ones.
            if target < today - dt.timedelta(days=30):
                con.execute("INSERT OR REPLACE INTO short_interest_files VALUES (?, NULL)", (key,))
                con.commit()
            continue
        rows = parse_short_interest(data, keep)
        con.executemany("INSERT OR REPLACE INTO short_interest VALUES (?, ?, ?, ?)", rows)
        con.execute("INSERT OR REPLACE INTO short_interest_files VALUES (?, ?)", (key, found.isoformat()))
        con.commit()
        added += 1
        if added % 24 == 0:
            log(f"[short] {added} short interest reports loaded (up to {found})")
    return added


def load_short_interest(con: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM short_interest", con, parse_dates=["settle"])
    df["available"] = df["settle"] + pd.Timedelta(days=PUBLISH_LAG_DAYS)
    return df


# --- Short volume -------------------------------------------------------------------------------

def parse_short_volume(data: bytes, keep: set[str] | None = None) -> tuple[str | None, list[tuple]]:
    """(date, [(ticker, short_volume, total_volume)]) from one daily FINRA file."""
    text = data.decode("utf-8-sig", "replace")
    rows, day = [], None
    for r in _rows(text):
        try:
            t = symbol(r["Symbol"])
            short, total = float(r["ShortVolume"]), float(r["TotalVolume"])
            day = day or r["Date"].strip()
        except (KeyError, TypeError, ValueError, AttributeError):
            continue  # the last line is a record count
        if keep is not None and t not in keep:
            continue
        rows.append((t, short, total))
    return day, rows


def update_short_volume(con: sqlite3.Connection, fetch: Fetch | None = None, today: dt.date | None = None,
                        start: dt.date | None = None, pause: float = 0.1, log: Callable[[str], None] = print) -> int:
    """Add each new trading day's short volume to the monthly totals. Returns the days added."""
    fetch, today, start = fetch or download, today or dt.date.today(), start or SV_FIRST
    keep = {t for (t,) in con.execute("SELECT ticker FROM tickers")}
    last = get_meta(con, "short_volume_last")
    d = dt.date.fromisoformat(last) + dt.timedelta(days=1) if last else start
    added = 0
    while d < today:
        if d.weekday() < 5:
            data = fetch(SV_URL.format(date=d))
            time.sleep(pause)
            if data is None and d >= today - dt.timedelta(days=3):
                break  # not published yet; try again next run
            if data:
                _, rows = parse_short_volume(data, keep)
                month = d.strftime("%Y-%m")
                con.executemany(
                    """INSERT INTO short_volume VALUES (?, ?, ?, ?, 1)
                       ON CONFLICT(ticker, month) DO UPDATE SET short = short + excluded.short,
                       total = total + excluded.total, days = days + 1""",
                    [(t, month, s, v) for t, s, v in rows])
                added += 1
                if added % 100 == 0:
                    log(f"[short] {added} days of short volume loaded (up to {d})")
        # Same transaction as the totals, so a day is never counted twice.
        set_meta(con, "short_volume_last", d.isoformat())
        con.commit()
        d += dt.timedelta(days=1)
    return added


def update(con: sqlite3.Connection, fetch: Fetch | None = None, log: Callable[[str], None] = print) -> None:
    """Load any new short interest reports and daily short volume (the first run fetches the history)."""
    n = update_short_interest(con, fetch=fetch, log=log)
    m = update_short_volume(con, fetch=fetch, log=log)
    log(f"[short] {n} short interest report(s) and {m} day(s) of short volume added")


# --- Factor values ----------------------------------------------------------------------------------

def split_factor(splits: pd.DataFrame | None, after: pd.Timestamp) -> float:
    """Product of the split ratios after a date (turns a share count then into today's basis)."""
    if splits is None or splits.empty:
        return 1.0
    return float(np.prod(splits.loc[pd.to_datetime(splits["date"]) > after, "ratio"]))


def factor_values(si: pd.DataFrame, sv: pd.DataFrame, when: pd.Timestamp, shares: pd.Series,
                  shares_date: pd.Series, splits: pd.DataFrame, max_age_days: int = 45) -> pd.DataFrame:
    """Short selling signals per ticker at a month end, using only reports public by then.

    shares / shares_date: shares outstanding per ticker and the date of that count (from the filings).
    """
    known = si[(si["available"] <= when) & (si["settle"] >= when - pd.Timedelta(days=max_age_days))]
    latest = known.sort_values("settle").drop_duplicates("ticker", keep="last").set_index("ticker")
    out = pd.DataFrame(index=latest.index)
    vol = latest["avg_volume"].where(latest["avg_volume"] > 0)
    out["days_to_cover"] = latest["short"] / vol

    # Change vs the report about a month earlier (25-40 days before).
    prev = si[si["ticker"].isin(latest.index)].merge(latest[["settle"]].rename(columns={"settle": "latest"}),
                                                     left_on="ticker", right_index=True)
    gap = (prev["latest"] - prev["settle"]).dt.days
    prev = prev[gap.between(25, 40)].sort_values("settle").drop_duplicates("ticker", keep="last").set_index("ticker")
    out["short_change"] = np.log((latest["short"] + 1) / (prev["short"].reindex(latest.index) + 1))

    # Short interest as a share of shares outstanding, both in today's split basis.
    by_ticker = {t: g for t, g in splits.groupby("ticker")} if len(splits) else {}
    ratio = {}
    for t, r in latest.iterrows():
        n = shares.get(t)
        if n is None or pd.isna(n) or n <= 0:
            continue
        s = by_ticker.get(t)
        ratio[t] = (r["short"] * split_factor(s, r["settle"])) / (n * split_factor(s, shares_date[t]))
    out["short_ratio"] = pd.Series(ratio, dtype=float)

    # Last month's short volume share (published daily, so the month just ended is known).
    month = when.strftime("%Y-%m")
    m = sv[(sv["month"] == month) & (sv["total"] > 0)].set_index("ticker")
    out = out.join((m["short"] / m["total"]).rename("short_volume_ratio"), how="outer")
    return out
