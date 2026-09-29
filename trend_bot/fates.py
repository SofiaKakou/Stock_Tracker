"""What happened to companies that disappeared (survivorship-bias correction).

Yahoo drops the price history of companies that are no longer listed, so an
event study only sees the survivors. The SEC still has every company's
filings, which tell us how it ended:

- bankrupt:  an 8-K with Item 1.03 "Bankruptcy or Receivership"
- acquired:  deregistered (Form 15 / 25) shortly after merger paperwork
             (merger proxy, tender-offer response, or an 8-K "change in control")
- delisted:  deregistered for another reason (went private, moved abroad, ...)
- unknown:   still filing, or no clear sign (e.g. moved to over-the-counter trading)
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Callable

import requests

from trend_bot import insiders

DEREGISTRATION = {"15-12B", "15-12G", "15-15D", "15F-12B", "15F-12G", "15F-15D", "25", "25-NSE"}
MERGER_FORMS = {"DEFM14A", "DEFM14C", "PREM14A", "PREM14C", "SC 14D9", "SC 14D9/A", "SC TO-T", "SC 13E3"}
MERGER_LOOKBACK = dt.timedelta(days=400)


def classify(doc: dict) -> tuple[str, str | None]:
    """(status, date) from a company's SEC submissions JSON."""
    recent = doc.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    items = recent.get("items") or [""] * len(forms)
    filings = list(zip(forms, dates, items))

    bankrupt = [d for f, d, it in filings if f.startswith("8-K") and "1.03" in (it or "")]
    if bankrupt:
        return "bankrupt", min(bankrupt)

    dereg = [d for f, d, _ in filings if f in DEREGISTRATION]
    if not dereg:
        return "unknown", None
    end = min(dereg)
    start = (dt.date.fromisoformat(end) - MERGER_LOOKBACK).isoformat()
    merger = any(
        start <= d <= end and (f in MERGER_FORMS or (f.startswith("8-K") and "5.01" in (it or "")))
        for f, d, it in filings
    )
    return ("acquired" if merger else "delisted"), end


def companies_to_check(con: sqlite3.Connection, recheck_days: int = 90) -> list[int]:
    """Companies with insider buying by 2+ people but no price data, not checked recently.

    A final outcome (bankrupt/acquired/delisted) is never re-checked.
    """
    cutoff = (dt.datetime.now() - dt.timedelta(days=recheck_days)).isoformat(timespec="seconds")
    rows = con.execute(
        """SELECT cik FROM insider_trades
           WHERE code = 'P' AND cik IS NOT NULL
             AND cik NOT IN (SELECT cik FROM tickers WHERE cik IS NOT NULL AND last_date IS NOT NULL)
             AND cik NOT IN (SELECT cik FROM company_fates
                             WHERE status IN ('bankrupt', 'acquired', 'delisted') OR checked_at >= ?)
           GROUP BY cik HAVING COUNT(DISTINCT insider) >= 2
           ORDER BY cik""",
        (cutoff,),
    )
    return [r[0] for r in rows]


def update_fates(con: sqlite3.Connection, fetch: Callable[[str], bytes] | None = None,
                 log: Callable[[str], None] = print, limit: int | None = None) -> dict[str, int]:
    fetch = fetch or insiders._get
    ciks = companies_to_check(con)
    if limit:
        ciks = ciks[:limit]
    counts: dict[str, int] = {}
    if not ciks:
        log("[fates] nothing to check")
        return counts
    log(f"[fates] checking what happened to {len(ciks):,} companies without price data...")
    for n, cik in enumerate(ciks, 1):
        try:
            doc = json.loads(fetch(insiders.SUBMISSIONS_URL.format(cik=cik)))
            status, date = classify(doc)
            name = doc.get("name")
        except requests.HTTPError:
            status, date, name = "unknown", None, None
        con.execute(
            "INSERT OR REPLACE INTO company_fates VALUES (?, ?, ?, ?, ?)",
            (cik, name, status, date, dt.datetime.now().isoformat(timespec="seconds")),
        )
        counts[status] = counts.get(status, 0) + 1
        if n % 50 == 0:
            con.commit()
        if n % 250 == 0:
            log(f"[fates] {n:,}/{len(ciks):,} checked")
    con.commit()
    return counts
