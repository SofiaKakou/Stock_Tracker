"""How much did a company rewrite its annual / quarterly report since last year? ("Lazy Prices")

Companies mostly copy last year's 10-K / 10-Q wording. Research (Cohen, Malloy and Nguyen,
"Lazy Prices", Journal of Finance 2020) found that when a company changes its report a lot,
especially the risk sections, its stock tends to lag over the following months: big rewrites
often come with news the company would rather not stress. Firms that change little did fine.

For every 10-K and 10-Q of the companies we track, the bot downloads the main document
from the SEC (paced, a batch per night), turns it into word counts, and keeps only a
compact fingerprint (word counts hashed into 8,192 buckets). It then compares each report
with the same kind of report about a year earlier: cosine similarity of the word counts,
1 = identical wording. The signal is `report_change` = 1 - similarity, known from the day
the new report was filed.

No AI here: this is plain text statistics, cheap and free of hindsight.
"""

from __future__ import annotations

import html
import re
import sqlite3
import time
import zlib
from typing import Callable

import numpy as np
import pandas as pd

from trend_bot import insiders

ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
BUCKETS = 8192
FIRST_YEAR = 2009        # XBRL financials start here, so the other signals do too
FRESH_DAYS = 200         # a report change counts for about two quarters
MIN_WORDS = 2000         # shorter documents are usually cover pages or exhibits, not the report

_TAGS = re.compile(r"<[^>]+>")
_HIDDEN = re.compile(r"<(ix:header|script|style)[^>]*>.*?</\1>", re.S | re.I)
_WORD = re.compile(r"[a-z]{3,}")


# --- Text -> fingerprint -------------------------------------------------------------------

def words(raw: bytes) -> list[str]:
    """Lower-case words of an EDGAR HTML document (tags, hidden XBRL data and numbers dropped)."""
    text = raw.decode("utf-8", "replace")
    text = _HIDDEN.sub(" ", text)
    text = html.unescape(_TAGS.sub(" ", text))
    return _WORD.findall(text.lower())


def fingerprint(ws: list[str]) -> np.ndarray:
    """Word counts hashed into BUCKETS slots (crc32: the same on every machine and run)."""
    idx = np.fromiter((zlib.crc32(w.encode()) % BUCKETS for w in ws), dtype=np.int64, count=len(ws))
    return np.bincount(idx, minlength=BUCKETS).astype(np.uint32)


def pack(vec: np.ndarray) -> bytes:
    return zlib.compress(vec.astype(np.uint32).tobytes(), 9)


def unpack(blob: bytes) -> np.ndarray:
    return np.frombuffer(zlib.decompress(blob), dtype=np.uint32).astype(np.float64)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(a @ b / (na * nb)) if na and nb else np.nan


# --- Downloading ----------------------------------------------------------------------------

def to_fetch(con: sqlite3.Connection, ciks: set[int] | None = None, since_year: int = FIRST_YEAR) -> pd.DataFrame:
    """Reports not read yet, newest first (so today's signal is ready before the history fills in)."""
    df = pd.read_sql_query(
        """SELECT f.accession, f.cik, f.form, f.filed, f.primary_doc FROM filings f
           LEFT JOIN doc_vectors v ON v.accession = f.accession
           WHERE v.accession IS NULL AND f.primary_doc IS NOT NULL AND f.filed >= ?
           ORDER BY f.filed DESC""", con, params=[f"{since_year}-01-01"])
    return df[df["cik"].isin(ciks)] if ciks is not None else df


def update(con: sqlite3.Connection, limit: int = 2500, ciks: set[int] | None = None,
           fetch: Callable[[str], bytes] | None = None, log: Callable[[str], None] = print) -> int:
    """Read up to `limit` new reports from the SEC and store their fingerprints. Returns how many."""
    import requests

    fetch = fetch or insiders._get
    todo = to_fetch(con, ciks).head(limit)
    if todo.empty:
        return 0
    log(f"[reports] reading {len(todo):,} annual/quarterly reports from the SEC "
        f"({len(to_fetch(con, ciks)):,} still to read)...")
    started, done = time.time(), 0
    for r in todo.itertuples(index=False):
        url = ARCHIVE_URL.format(cik=r.cik, acc=r.accession.replace("-", ""), doc=r.primary_doc)
        try:
            ws = words(fetch(url))
        except requests.HTTPError:
            ws = []  # gone or moved: remember it, don't ask again
        if len(ws) >= MIN_WORDS:
            con.execute("INSERT OR REPLACE INTO doc_vectors VALUES (?, ?, ?)",
                        (r.accession, len(ws), pack(fingerprint(ws))))
        else:
            con.execute("INSERT OR REPLACE INTO doc_vectors VALUES (?, 0, NULL)", (r.accession,))
        done += 1
        if done % 50 == 0:
            con.commit()
        if done % 500 == 0:
            log(f"[reports] {done:,}/{len(todo):,} ({time.time() - started:.0f}s)")
    con.commit()
    return done


# --- The signal ---------------------------------------------------------------------------------

def changes(con: sqlite3.Connection) -> pd.DataFrame:
    """Each report compared with the same kind of report about a year earlier.

    Columns: cik, form, filed, similarity (1 = same wording), report_change (1 - similarity).
    A 10-Q is matched with the 10-Q whose period ended 330-400 days earlier (the same quarter
    last year); a 10-K with the previous 10-K.
    """
    df = pd.read_sql_query(
        """SELECT f.accession, f.cik, f.form, f.filed, COALESCE(f.period, f.filed) AS period, v.vec
           FROM filings f JOIN doc_vectors v ON v.accession = f.accession
           WHERE v.words > 0""", con, parse_dates=["filed", "period"])
    rows = []
    for (cik, form), g in df.groupby(["cik", "form"]):
        g = g.sort_values("period").reset_index(drop=True)
        vecs = [unpack(b) for b in g["vec"]]
        periods = g["period"].to_numpy()
        for i in range(len(g)):
            lag = (periods[i] - periods[:i]).astype("timedelta64[D]").astype(int)
            j = np.where((lag >= 330) & (lag <= 400))[0]
            if len(j):
                sim = cosine(vecs[i], vecs[j[-1]])
                rows.append((cik, form, g.at[i, "filed"], sim, 1 - sim))
    return pd.DataFrame(rows, columns=["cik", "form", "filed", "similarity", "report_change"])


def latest(ch: pd.DataFrame, when: pd.Timestamp, fresh_days: int = FRESH_DAYS) -> pd.Series:
    """Per company: the change measured by the most recent report filed in the `fresh_days` up to `when`."""
    recent = ch[(ch["filed"] <= when) & (ch["filed"] > when - pd.Timedelta(days=fresh_days))]
    return recent.sort_values("filed").drop_duplicates("cik", keep="last").set_index("cik")["report_change"]
