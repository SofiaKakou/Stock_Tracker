"""Company details from the SEC (industry code), used to tell funds from companies.

Closed-end funds and ETFs trade like stocks, but they aren't operating
companies: their "trend flips" just follow their holdings, and their insider
trades mean little. The SEC's industry code (SIC) identifies them reliably:
6722 = open-end funds, 6726 = closed-end funds / unit investment trusts.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from typing import Callable

import requests

from trend_bot import insiders

FUND_SIC = {6722, 6726}
# Names that are always funds/ETFs, even when the SEC code is something else.
ETF_WORDS = re.compile(r"\b(ETF|ETN|ISHARES|SPDR|PROSHARES|DIREXION)\b", re.I)
# Used only while the SEC code hasn't been looked up yet.
FUND_WORDS = re.compile(r"\b(FUND|FD|PORTFOLIO|MUNICIPAL|MUNI|PREFERRED INCOME|INCOME (FUND|TRUST))\b", re.I)


def is_fund(name: str | None, sic: int | None, exchange: str | None) -> bool:
    name = name or ""
    if exchange == "ETF" or ETF_WORDS.search(name):
        return True
    if sic is not None:
        return sic in FUND_SIC
    return bool(FUND_WORDS.search(name))


def fund_tickers(con: sqlite3.Connection) -> set[str]:
    rows = con.execute("SELECT ticker, name, sic, exchange FROM tickers")
    return {t for t, name, sic, exch in rows if is_fund(name, sic, exch)}


def update_company_info(con: sqlite3.Connection, fetch: Callable[[str], bytes] | None = None,
                        log: Callable[[str], None] = print, limit: int | None = None) -> int:
    """Look up the industry code of every listed company not checked yet (most traded first)."""
    fetch = fetch or insiders._get
    ciks = [r[0] for r in con.execute(
        """SELECT cik FROM tickers WHERE cik IS NOT NULL AND info_checked_at IS NULL
           GROUP BY cik ORDER BY MAX(COALESCE(dollar_vol, 0)) DESC""")]
    if limit:
        ciks = ciks[:limit]
    if not ciks:
        return 0
    log(f"[companies] looking up industry codes for {len(ciks):,} companies (once per company)...")
    now = dt.datetime.now().isoformat(timespec="seconds")
    for n, cik in enumerate(ciks, 1):
        try:
            doc = json.loads(fetch(insiders.SUBMISSIONS_URL.format(cik=cik)))
            sic = int(doc["sic"]) if str(doc.get("sic") or "").isdigit() else None
            desc = doc.get("sicDescription")
        except (requests.HTTPError, ValueError):
            sic, desc = None, None
        con.execute("UPDATE tickers SET sic = ?, sic_desc = ?, info_checked_at = ? WHERE cik = ?",
                    (sic, desc, now, cik))
        if n % 50 == 0:
            con.commit()
        if n % 500 == 0:
            log(f"[companies] {n:,}/{len(ciks):,}")
    con.commit()
    return len(ciks)
