"""Loading insider trades for the whole market into the database.

Two sources, both free from the SEC:

1. Quarterly "Insider Transactions Data Sets": every Form 3/4/5 filed in a
   quarter as tab-separated files in one zip (2006 onwards). Fast and complete,
   but only published a few weeks after each quarter ends.
2. Daily filing indexes for the weeks since the last published quarter: each
   Form 4 is fetched and parsed individually (slower, ~1,500 filings a day).
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from typing import Callable

import pandas as pd
import requests

from trend_bot import insiders
from trend_bot.db import get_meta, set_meta

BULK_URL = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{year}q{q}_form345.zip"
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{q}/form.{ymd}.idx"
FILING_URL = "https://www.sec.gov/Archives/{path}"

Log = Callable[[str], None]


def _quarter(d: dt.date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def _quarter_bounds(year: int, q: int) -> tuple[dt.date, dt.date]:
    start = dt.date(year, 3 * (q - 1) + 1, 1)
    end = dt.date(year + (q == 4), (3 * q) % 12 + 1, 1) - dt.timedelta(days=1)
    return start, end


def _cik_to_ticker(con: sqlite3.Connection) -> dict[int, str]:
    # A company can have several share classes; prefer the shortest ticker (e.g. GOOGL over GOOGL-X).
    out: dict[int, str] = {}
    for cik, ticker in con.execute("SELECT cik, ticker FROM tickers WHERE cik IS NOT NULL ORDER BY LENGTH(ticker), ticker"):
        out.setdefault(cik, ticker)
    return out


def _date(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, format="%d-%b-%Y", errors="coerce")
    fallback = pd.to_datetime(series[parsed.isna()], errors="coerce", format="mixed")
    return parsed.fillna(fallback).dt.strftime("%Y-%m-%d")


def _role_from_bulk(rel: str, title: str) -> str:
    # RPTOWNER_RELATIONSHIP is a comma-separated list like "Director,Officer,TenPercentOwner".
    rel = (rel or "").replace(" ", "").lower()
    parts = []
    if "officer" in rel:
        parts.append(title or "Officer")
    if "director" in rel:
        parts.append("Director")
    if "tenpercent" in rel:
        parts.append("10% owner")
    return ", ".join(parts) or "Insider"


def _read_tsv(zf: zipfile.ZipFile, name: str) -> pd.DataFrame:
    member = next(n for n in zf.namelist() if n.upper().endswith(name))
    with zf.open(member) as f:
        return pd.read_csv(f, sep="\t", dtype=str, keep_default_na=False, na_values=[""],
                           quoting=3, on_bad_lines="skip", encoding="latin-1")


def parse_bulk_zip(data: bytes, cik_to_ticker: dict[int, str]) -> list[tuple]:
    """Rows for insider_trades from one quarterly zip (Form 4 open-market buys and sales only)."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    sub = _read_tsv(zf, "SUBMISSION.TSV")
    own = _read_tsv(zf, "REPORTINGOWNER.TSV")
    trans = _read_tsv(zf, "NONDERIV_TRANS.TSV")

    sub = sub[sub["DOCUMENT_TYPE"] == "4"].copy()
    sub["filed"] = _date(sub["FILING_DATE"])
    sub["planned"] = sub.get("AFF10B5ONE", pd.Series("", index=sub.index)).fillna("").str.lower().isin(["1", "true"])

    own["role"] = [_role_from_bulk(r, t) for r, t in zip(own["RPTOWNER_RELATIONSHIP"].fillna(""),
                                                       own["RPTOWNER_TITLE"].fillna(""))]
    owners = own.groupby("ACCESSION_NUMBER").agg(
        insider=("RPTOWNERNAME", lambda s: " & ".join(s.dropna())),
        role=("role", "first"),
    )
    df = trans.merge(sub, on="ACCESSION_NUMBER").merge(owners, left_on="ACCESSION_NUMBER", right_index=True, how="left")
    df["date"] = _date(df["TRANS_DATE"])
    num = lambda c: pd.to_numeric(df[c], errors="coerce")
    df["shares"], df["price"], df["owned_after"] = num("TRANS_SHARES"), num("TRANS_PRICEPERSHARE"), num("SHRS_OWND_FOLWNG_TRANS")
    df["cik"] = pd.to_numeric(df["ISSUERCIK"], errors="coerce")
    df = df.dropna(subset=["cik", "shares", "date"])
    df = df[df["TRANS_CODE"].isin(insiders.MARKET_CODES)]  # keep only open-market buys and sales

    rows = []
    for r in df.itertuples(index=False):
        cik = int(r.cik)
        ticker = cik_to_ticker.get(cik) or (r.ISSUERTRADINGSYMBOL or "").upper() or None
        rows.append((
            r.ACCESSION_NUMBER, int(r.NONDERIV_TRANS_SK), cik, ticker,
            (r.insider or "?").title(), r.role or "Insider", r.TRANS_CODE, r.date, r.filed,
            float(r.shares), None if pd.isna(r.price) else float(r.price),
            None if pd.isna(r.owned_after) else float(r.owned_after), int(bool(r.planned)),
        ))
    return rows


def _insert(con: sqlite3.Connection, rows: list[tuple]) -> None:
    con.executemany("INSERT OR REPLACE INTO insider_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def load_quarters(con: sqlite3.Connection, since_year: int = 2006, today: dt.date | None = None,
                  fetch: Callable[[str], bytes] | None = None, log: Log = print) -> int:
    """Load every published quarter not loaded yet. Returns the number of quarters added."""
    fetch = fetch or insiders._get
    today = today or dt.date.today()
    done = set(json.loads(get_meta(con, "insider_quarters", "[]")))
    mapping = _cik_to_ticker(con)
    added = 0
    year, q = since_year, 1
    while (year, q) < _quarter(today):
        key = f"{year}q{q}"
        if key not in done:
            try:
                data = fetch(BULK_URL.format(year=year, q=q))
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code != 404:
                    raise
                if today - _quarter_bounds(year, q)[1] < dt.timedelta(days=180):
                    log(f"[insiders] {key} not published yet")
                    break
                # An old quarter that's missing on the SEC site: skip it, keep going.
                log(f"[insiders] {key} not available on the SEC site, skipped")
                year, q = (year + 1, 1) if q == 4 else (year, q + 1)
                continue
            rows = parse_bulk_zip(data, mapping)
            start, end = _quarter_bounds(year, q)
            # Replace anything loaded for this period from the daily feed.
            con.execute("DELETE FROM insider_trades WHERE filed BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))
            _insert(con, rows)
            done.add(key)
            set_meta(con, "insider_quarters", json.dumps(sorted(done)))
            set_meta(con, "insider_bulk_through", end.isoformat())
            con.commit()
            added += 1
            log(f"[insiders] {key}: {len(rows):,} transactions")
        year, q = (year + 1, 1) if q == 4 else (year, q + 1)
    return added


# --- Daily feed for the current quarter ------------------------------------------------

def parse_daily_index(text: str) -> list[tuple[str, str]]:
    """(filing path, date filed) for every Form 4 in a daily form.idx, deduplicated."""
    out, seen = [], set()
    body = False
    for line in text.splitlines():
        if line.startswith("---"):
            body = True
            continue
        if not body or not line.startswith("4 "):
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        date, path = parts[-2], parts[-1]
        if path not in seen:
            seen.add(path)
            filed = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else date
            out.append((path, filed))
    return out


def parse_filing_txt(text: str, filed: str, accession: str, cik_to_ticker: dict[int, str]) -> list[tuple]:
    m = re.search(r"<XML>\s*(.*?)\s*</XML>", text, re.S | re.I)
    if not m:
        return []
    xml = m.group(1)
    root = ET.fromstring(xml)
    cik_text = root.findtext("issuer/issuerCik") or ""
    cik = int(cik_text) if cik_text.strip().isdigit() else None
    ticker = cik_to_ticker.get(cik) or (root.findtext("issuer/issuerTradingSymbol") or "").strip().upper() or None
    trades = insiders.parse_form4(xml, ticker or "?", dt.date.fromisoformat(filed), accession)
    return [
        (accession, i, cik, ticker, t.insider, t.role, t.code, t.date.isoformat(), filed,
         t.shares, t.price, t.owned_after, int(t.planned))
        for i, t in enumerate(trades)
        if t.code in insiders.MARKET_CODES
    ]


def load_recent_days(con: sqlite3.Connection, max_days: int = 30, today: dt.date | None = None,
                     fetch: Callable[[str], bytes] | None = None, log: Log = print) -> int:
    """Fill the gap after the last bulk quarter from daily filing indexes. Returns filings loaded."""
    fetch = fetch or insiders._get
    today = today or dt.date.today()
    log("[insiders] reading recent Form 4 filings one by one (about 1,500 a day, ~4 minutes per day)")
    # Continue after whatever is already covered (by the daily feed or a bulk quarter),
    # but never reach back further than max_days.
    covered = [d for d in (get_meta(con, "insider_daily_through"), get_meta(con, "insider_bulk_through")) if d]
    start = today - dt.timedelta(days=max_days)
    if covered:
        start = max(start, dt.date.fromisoformat(max(covered)) + dt.timedelta(days=1))
    mapping = _cik_to_ticker(con)
    total = 0
    day = start
    while day < today:  # today's index isn't complete until the evening
        if day.weekday() < 5:
            year, q = _quarter(day)
            try:
                text = fetch(DAILY_INDEX_URL.format(year=year, q=q, ymd=day.strftime("%Y%m%d"))).decode("latin-1")
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code not in (403, 404):
                    raise
                text = ""  # holiday: no index
            filings = parse_daily_index(text)
            if filings:
                log(f"[insiders] {day}: {len(filings)} Form 4 filings")
            for n, (path, filed) in enumerate(filings, 1):
                if n % 250 == 0:
                    log(f"[insiders] {day}: {n}/{len(filings)} filings read")
                accession = path.rsplit("/", 1)[-1].removesuffix(".txt")
                try:
                    rows = parse_filing_txt(fetch(FILING_URL.format(path=path)).decode("latin-1"), filed, accession, mapping)
                except (ET.ParseError, requests.RequestException, ValueError):
                    continue
                _insert(con, rows)
                total += 1
        set_meta(con, "insider_daily_through", day.isoformat())
        con.commit()
        day += dt.timedelta(days=1)
    return total
