"""8-K red flags: warning-sign events companies must report to the SEC.

When something important happens, a company files a short 8-K naming the "items" it covers.
A few items are classic warning signs that research has linked to weak returns afterwards:

- 4.02  past financial statements can no longer be relied on (a restatement is coming)
- 4.01  the auditor resigned or was replaced
- 2.06  a material impairment (assets written down)
- 2.04  an event that speeds up what the company owes (often a debt default)
- 3.01  a warning from the stock exchange about failing its listing rules
- 1.03  bankruptcy or receivership
- 2.05  costs of closing or exiting a business (restructuring)
- 5.02  a director or top officer leaving or arriving (noisier: also covers routine changes)

Late filing notices (forms NT 10-K / NT 10-Q: "our report will be late") count too; they
often come before restatements or worse.

Everything comes from the SEC's weekly bulk file the bot already reads (sec_submissions.py),
so there are no extra downloads. Signals for each month use only events filed by then.
"""

from __future__ import annotations

import sqlite3

import pandas as pd

ITEMS = {
    "4.02": "past financial statements can't be relied on",
    "4.01": "auditor changed",
    "2.06": "assets written down (impairment)",
    "2.04": "debt default or accelerated obligation",
    "3.01": "stock exchange listing warning",
    "1.03": "bankruptcy",
    "2.05": "restructuring / exit costs",
    "5.02": "director or officer change",
}
RED_FLAG_ITEMS = {"4.02", "4.01", "2.06", "2.04", "3.01", "1.03", "2.05"}
EXEC_ITEMS = {"5.02"}
LATE_FORMS = {"NT 10-K", "NT 10-Q"}
EVENT_FORMS = {"8-K", "8-K/A"} | LATE_FORMS
WINDOW_DAYS = 180    # an event counts for about two quarters
SIGNALS = ["red_flags", "late_filing", "exec_changes"]


def keep(form: str, items: str | None) -> str | None:
    """The red-flag items of one filing ('4.02,5.02'), 'late' for late notices, or None to skip it."""
    if form in LATE_FORMS:
        return "late"
    if form not in EVENT_FORMS or not items:
        return None
    found = sorted({i.strip() for i in items.split(",")} & set(ITEMS))
    return ",".join(found) or None


def save(con: sqlite3.Connection, cik: int, cols: dict) -> int:
    """Store a company's red-flag filings from one page of its submissions (column lists)."""
    forms = cols.get("form") or []
    pick = lambda key: cols.get(key) or [None] * len(forms)
    rows = []
    for form, acc, filed, items in zip(forms, pick("accessionNumber"), pick("filingDate"), pick("items")):
        what = keep(form, items)
        if what and acc and filed:
            rows.append((acc, cik, form, filed, what))
    con.executemany("INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?)", rows)
    return len(rows)


def load(con: sqlite3.Connection) -> pd.DataFrame:
    """One row per filing with a column per signal (how many such events it carries)."""
    df = pd.read_sql_query("SELECT cik, form, filed, items FROM events", con, parse_dates=["filed"])
    items = df["items"].str.split(",")
    df["red_flags"] = items.map(lambda xs: len(set(xs) & RED_FLAG_ITEMS))
    df["exec_changes"] = items.map(lambda xs: len(set(xs) & EXEC_ITEMS))
    df["late_filing"] = (df["items"] == "late").astype(int)
    return df


def counts(ev: pd.DataFrame, when: pd.Timestamp, window_days: int = WINDOW_DAYS) -> pd.DataFrame:
    """Per company (cik): how many events of each kind were filed in the `window_days` up to `when`."""
    recent = ev[(ev["filed"] <= when) & (ev["filed"] > when - pd.Timedelta(days=window_days))]
    return recent.groupby("cik")[SIGNALS].sum()


def recent_flags(con: sqlite3.Connection, ciks: list[int], days: int = 30) -> pd.DataFrame:
    """Red flags and late filings of these companies in the last `days` (for alerts; 5.02 left out as too routine)."""
    if not ciks:
        return pd.DataFrame(columns=["accession", "cik", "form", "filed", "items"])
    since = (pd.Timestamp.today() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    df = pd.read_sql_query(
        f"""SELECT accession, cik, form, filed, items FROM events
            WHERE filed >= ? AND cik IN ({','.join('?' * len(ciks))}) ORDER BY filed DESC""",
        con, params=[since, *ciks])
    serious = [s == "late" or bool(set(s.split(",")) & RED_FLAG_ITEMS) for s in df["items"]]
    return df[pd.Series(serious, index=df.index, dtype=bool)]


def describe(items: str) -> str:
    if items == "late":
        return "filed a notice that its report will be late"
    return "; ".join(ITEMS[i] for i in items.split(",") if i in RED_FLAG_ITEMS | EXEC_ITEMS)
