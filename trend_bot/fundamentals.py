"""Company fundamentals from the SEC's XBRL "company facts" (free, every US filer since ~2009).

The SEC publishes one bulk file (companyfacts.zip, ~1.3 GB, refreshed nightly)
with every number each company has reported in its 10-K and 10-Q filings,
including the date each number was filed. We keep a handful of items and
always look them up "as of" a date, using only what had been filed by then,
so the history tests never peek at numbers that weren't public yet.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from trend_bot import insiders
from trend_bot.db import get_meta, set_meta

BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A")

# item -> XBRL tags in order of preference (taxonomy, tag, unit)
ITEMS: dict[str, list[tuple[str, str, str]]] = {
    "revenue": [("us-gaap", "Revenues", "USD"),
                ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
                ("us-gaap", "SalesRevenueNet", "USD"),
                ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax", "USD")],
    "gross_profit": [("us-gaap", "GrossProfit", "USD")],
    "cost_of_revenue": [("us-gaap", "CostOfRevenue", "USD"), ("us-gaap", "CostOfGoodsAndServicesSold", "USD")],
    "operating_income": [("us-gaap", "OperatingIncomeLoss", "USD")],
    "net_income": [("us-gaap", "NetIncomeLoss", "USD")],
    "cfo": [("us-gaap", "NetCashProvidedByUsedInOperatingActivities", "USD")],
    "assets": [("us-gaap", "Assets", "USD")],
    "liabilities": [("us-gaap", "Liabilities", "USD")],
    "equity": [("us-gaap", "StockholdersEquity", "USD"),
               ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "USD")],
    "shares": [("dei", "EntityCommonStockSharesOutstanding", "shares"),
               ("us-gaap", "CommonStockSharesOutstanding", "shares")],
}
FLOWS = {"revenue", "gross_profit", "cost_of_revenue", "operating_income", "net_income", "cfo"}


# --- Loading -----------------------------------------------------------------------------

def parse_company(doc: dict) -> list[tuple]:
    """facts rows (cik, item, priority, start, end, filed, val) from one company's JSON."""
    cik = int(doc.get("cik") or 0)
    facts = doc.get("facts") or {}
    rows = []
    for item, tags in ITEMS.items():
        for priority, (taxonomy, tag, unit) in enumerate(tags):
            entries = ((facts.get(taxonomy) or {}).get(tag) or {}).get("units", {}).get(unit) or []
            for e in entries:
                if e.get("form") not in FORMS or e.get("val") is None or not e.get("end") or not e.get("filed"):
                    continue
                start = e.get("start")
                if item in FLOWS:
                    if not start:
                        continue
                    days = (dt.date.fromisoformat(e["end"]) - dt.date.fromisoformat(start)).days
                    if not 350 <= days <= 380:  # keep full fiscal years only
                        continue
                elif start:
                    continue
                rows.append((cik, item, priority, start, e["end"], e["filed"], float(e["val"])))
    return rows


def load_bulk(con: sqlite3.Connection, data: bytes | Path, ciks: set[int] | None = None,
              log: Callable[[str], None] = print) -> int:
    """Replace the facts table from a companyfacts.zip (bytes or a path). Returns rows stored."""
    zf = zipfile.ZipFile(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data)
    con.execute("DELETE FROM facts")
    total, batch = 0, []
    names = [n for n in zf.namelist() if n.endswith(".json")]
    for i, name in enumerate(names, 1):
        try:
            cik = int(Path(name).stem.removeprefix("CIK"))
        except ValueError:
            continue
        if ciks is not None and cik not in ciks:
            continue
        try:
            batch.extend(parse_company(json.loads(zf.read(name))))
        except (ValueError, KeyError):
            continue
        if len(batch) > 200_000:
            con.executemany("INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
            total += len(batch)
            batch = []
        if i % 2000 == 0:
            log(f"[fundamentals] {i:,}/{len(names):,} companies read")
    con.executemany("INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
    total += len(batch)
    set_meta(con, "facts_updated", dt.datetime.now().isoformat(timespec="seconds"))
    con.commit()
    return total


def download_bulk(path: Path, log: Callable[[str], None] = print) -> Path:
    """Stream companyfacts.zip to disk (it's large), identifying ourselves as the SEC asks."""
    import requests

    path.parent.mkdir(parents=True, exist_ok=True)
    log("[fundamentals] downloading the SEC's company facts file (~1.3 GB)...")
    with requests.get(BULK_URL, headers={"User-Agent": insiders.user_agent()}, stream=True, timeout=120) as r:
        if r.status_code in (403, 429):
            raise RuntimeError("The SEC is still limiting requests. Progress is saved - run the same command again later.")
        r.raise_for_status()
        tmp = path.with_suffix(".part")
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        tmp.replace(path)
    return path


def update_fundamentals(con: sqlite3.Connection, max_age_days: int = 7, force: bool = False,
                        download: Callable[[Path], Path] | None = None, log: Callable[[str], None] = print) -> int:
    """Refresh facts weekly (the file is big). Only companies we track are stored."""
    last = get_meta(con, "facts_updated")
    if not force and last and dt.datetime.fromisoformat(last) > dt.datetime.now() - dt.timedelta(days=max_age_days):
        return 0
    path = insiders.CACHE_DIR / "companyfacts.zip"
    started = time.time()
    (download or (lambda p: download_bulk(p, log)))(path)
    ciks = {c for (c,) in con.execute("SELECT DISTINCT cik FROM tickers WHERE cik IS NOT NULL")}
    n = load_bulk(con, path, ciks, log)
    try:
        path.unlink()  # 1.3 GB we don't need to keep
    except OSError:
        pass
    log(f"[fundamentals] {n:,} reported values stored ({time.time() - started:.0f}s)")
    return n


# --- Looking values up as of a date --------------------------------------------------------------

def _pick_latest(df: pd.DataFrame) -> pd.DataFrame:
    """Per company: the most recent period; among equals, the preferred tag, then the latest filing."""
    df = df.sort_values(["cik", "end", "priority", "filed"], ascending=[True, True, False, True])
    return df.drop_duplicates("cik", keep="last").set_index("cik")


def as_of(facts: pd.DataFrame, when: pd.Timestamp, max_age_days: int = 550) -> pd.DataFrame:
    """Latest value of every item per company, using only filings made on or before `when`.

    Flows are the last full fiscal year; balance-sheet items and shares the latest reported.
    Also returns *_prior: the same item one year earlier (for growth rates).
    """
    known = facts[(facts["filed"] <= when) & (facts["end"] >= when - pd.Timedelta(days=max_age_days))]
    older = facts[facts["filed"] <= when]
    out = {}
    for item in ITEMS:
        rows = known[known["item"] == item]
        if rows.empty:
            continue
        latest = _pick_latest(rows)
        out[item] = latest["val"]
        # One year earlier: a period ending 330-400 days before the latest one.
        prev = older[older["item"] == item].merge(latest[["end"]].rename(columns={"end": "latest_end"}),
                                                  left_on="cik", right_index=True)
        gap = (prev["latest_end"] - prev["end"]).dt.days
        prev = prev[(gap >= 330) & (gap <= 400)]
        if len(prev):
            out[f"{item}_prior"] = _pick_latest(prev)["val"]
        if item == "shares":
            out["shares_date"] = latest["end"]
    return pd.DataFrame(out)


def load_facts(con: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM facts", con, parse_dates=["start", "end", "filed"])


# --- Factor values ----------------------------------------------------------------------------------

def _ratio(a: pd.Series, b: pd.Series, positive_denominator: bool = True) -> pd.Series:
    b = b.where(b > 0) if positive_denominator else b.where(b != 0)
    return a / b


def factor_values(f: pd.DataFrame, market_cap: pd.Series) -> pd.DataFrame:
    """Classic fundamental signals per company from as_of() values and market value (in $)."""
    g = lambda c: f[c] if c in f else pd.Series(np.nan, index=f.index)
    gross = g("gross_profit").fillna(g("revenue") - g("cost_of_revenue"))
    mcap = market_cap.reindex(f.index)
    return pd.DataFrame({
        # Quality
        "gross_profitability": _ratio(gross, g("assets")),
        "roe": _ratio(g("net_income"), g("equity")),
        "accruals": _ratio(g("net_income") - g("cfo"), g("assets")),
        "leverage": _ratio(g("liabilities"), g("assets")),
        # Growth / investment
        "revenue_growth": _ratio(g("revenue"), g("revenue_prior")) - 1,
        "asset_growth": _ratio(g("assets"), g("assets_prior")) - 1,
        # Value (cheap vs expensive)
        "earnings_yield": _ratio(g("net_income"), mcap),
        "book_to_market": _ratio(g("equity").where(g("equity") > 0), mcap),
        "sales_to_price": _ratio(g("revenue"), mcap),
    })


# Which way each factor is expected to point (+1: higher is better), from the research literature.
EXPECTED = {
    "gross_profitability": +1, "roe": +1, "accruals": -1, "leverage": -1,
    "revenue_growth": +1, "asset_growth": -1,
    "earnings_yield": +1, "book_to_market": +1, "sales_to_price": +1,
}
DESCRIPTIONS = {
    "gross_profitability": "gross profit / assets (Novy-Marx)",
    "roe": "net income / equity",
    "accruals": "(net income - operating cash flow) / assets; lower is better (Sloan)",
    "leverage": "liabilities / assets",
    "revenue_growth": "sales vs a year earlier",
    "asset_growth": "assets vs a year earlier; lower is better (investment effect)",
    "earnings_yield": "net income / market value",
    "book_to_market": "book equity / market value (Fama-French value)",
    "sales_to_price": "sales / market value",
}
