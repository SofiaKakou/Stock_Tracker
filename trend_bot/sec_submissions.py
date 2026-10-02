"""Industry codes and company fates for every company at once, from the SEC's bulk file.

Looking these up one company at a time (data.sec.gov/submissions/CIK....json) takes
thousands of requests: a few thousand a night at most, and none at all while the
SEC has asked us to slow down. The SEC also publishes every company's submissions
in one file (submissions.zip, refreshed nightly), so one download a week fills in:

- the industry code (SIC) of every company we track, and
- what happened to every company without prices that the studies need
  (bankrupt / bought out / delisted; see fates.classify).
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

from trend_bot import fates, insiders
from trend_bot.db import get_meta, set_meta

BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"


def download(path: Path, log: Callable[[str], None] = print) -> Path:
    """Stream submissions.zip to disk (it's large), identifying ourselves as the SEC asks."""
    import requests

    path.parent.mkdir(parents=True, exist_ok=True)
    log("[sec] downloading the SEC's bulk company file (submissions.zip, ~1.5 GB)...")
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


REPORT_FORMS = ("10-K", "10-Q")


def _save_reports(con: sqlite3.Connection, cik: int, cols: dict) -> int:
    """Store a company's 10-K / 10-Q filings from one page of its submissions (column lists)."""
    forms = cols.get("form") or []
    pick = lambda key: cols.get(key) or [None] * len(forms)
    rows = [(acc, cik, f, filed, period or None, doc or None)
            for f, acc, filed, period, doc in zip(forms, pick("accessionNumber"), pick("filingDate"),
                                                   pick("reportDate"), pick("primaryDocument"))
            if f in REPORT_FORMS and acc and filed]
    con.executemany("INSERT OR IGNORE INTO filings VALUES (?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def apply(con: sqlite3.Connection, data: bytes | Path, log: Callable[[str], None] = print) -> dict[str, int]:
    """Fill industry codes and fates from a submissions.zip (bytes or a path). Returns counts."""
    zf = zipfile.ZipFile(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data)
    tracked = {c for (c,) in con.execute("SELECT DISTINCT cik FROM tickers WHERE cik IS NOT NULL")}
    # Every company without prices whose fate a study needs, final outcomes excepted.
    need_fate = set(fates.companies_to_check(con, recheck_days=0))
    now = dt.datetime.now().isoformat(timespec="seconds")
    counts = {"industry": 0, "fates": 0, "reports": 0}
    all_names = [n for n in zf.namelist() if n.endswith(".json")]
    # Older filings of busy companies sit in extra pages (CIK..-submissions-001.json).
    for name in (n for n in all_names if "-submissions-" in n):
        try:
            cik = int(Path(name).stem.split("-")[0].removeprefix("CIK"))
        except ValueError:
            continue
        if cik in tracked:
            try:
                counts["reports"] += _save_reports(con, cik, json.loads(zf.read(name)))
            except ValueError:
                continue
    names = [n for n in all_names if "-submissions-" not in n]
    for i, name in enumerate(names, 1):
        try:
            cik = int(Path(name).stem.removeprefix("CIK"))
        except ValueError:
            continue
        if cik not in tracked and cik not in need_fate:
            continue
        try:
            doc = json.loads(zf.read(name))
        except ValueError:
            continue
        if cik in tracked:
            sic = int(doc["sic"]) if str(doc.get("sic") or "").isdigit() else None
            con.execute("UPDATE tickers SET sic = ?, sic_desc = ?, info_checked_at = ? WHERE cik = ?",
                        (sic, doc.get("sicDescription"), now, cik))
            counts["industry"] += sic is not None
            counts["reports"] += _save_reports(con, cik, doc.get("filings", {}).get("recent", {}))
        if cik in need_fate:
            status, date = fates.classify(doc)
            con.execute("INSERT OR REPLACE INTO company_fates VALUES (?, ?, ?, ?, ?)",
                        (cik, doc.get("name"), status, date, now))
            counts["fates"] += 1
            counts[status] = counts.get(status, 0) + 1
        if i % 20000 == 0:
            con.commit()
            log(f"[sec] {i:,}/{len(names):,} companies read")
    set_meta(con, "submissions_updated", now)
    con.commit()
    return counts


def update(con: sqlite3.Connection, max_age_days: int = 7, force: bool = False,
           fetch: Callable[[Path], Path] | None = None, log: Callable[[str], None] = print) -> dict[str, int]:
    """Weekly (the file is big): download submissions.zip, apply it, delete it."""
    last = get_meta(con, "submissions_updated")
    if not force and last and dt.datetime.fromisoformat(last) > dt.datetime.now() - dt.timedelta(days=max_age_days):
        return {}
    path = insiders.CACHE_DIR / "submissions.zip"
    started = time.time()
    (fetch or (lambda p: download(p, log)))(path)
    try:
        counts = apply(con, path, log)
    finally:
        try:
            path.unlink()  # we don't need to keep 1.5 GB
        except OSError:
            pass
    log(f"[sec] industry codes for {counts['industry']:,} companies; {counts['reports']:,} annual/quarterly "
        f"reports listed; fates for {counts['fates']:,} "
        f"({', '.join(f'{k} {v:,}' for k, v in sorted(counts.items()) if k not in ('industry', 'fates', 'reports')) or 'none'}) "
        f"in {time.time() - started:.0f}s")
    return counts
