"""Look-ahead check: does any signal quietly use information from the future?

The most common way a stock test lies is by using data that wasn't known yet on the day it
pretends to decide. The monthly signal table is built to avoid that (financials by filing date,
short data by publication date, and so on); this module checks it.

For a few past month-ends, it makes a copy of the database cut off at that date (every price,
filing, event and short-interest row after it deleted), rebuilds that month's signals from the
copy, and compares them with the same month in the normal table built from all the data. If a
signal only uses what was known at the time, both versions agree exactly. Any difference means a
peek at the future.

What it can't see: the price data itself comes pre-adjusted for later dividends and splits, so
price *levels* in the past are slightly off; returns (what the signals use) are not affected.
Companies that later went bankrupt or were bought out are added back by design (with their real
return), so they are left out of the comparison.
"""

from __future__ import annotations

import gc
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

# Table -> the date column after which a row wasn't known yet.
CUTOFFS = {
    "prices": "date",
    "splits": "date",
    "insider_trades": "filed",
    "facts": "filed",
    "filings": "filed",
    "events": "filed",
    "short_interest": "settle",
    "company_fates": "fate_date",
}
SKIP_COLUMNS = {"month", "ticker", "ret", "gone"}   # 'ret' is next month's return by design
DEFAULT_MONTHS = ("2012-06-30", "2016-06-30", "2021-06-30")


def truncated_copy(con: sqlite3.Connection, path: Path, when: pd.Timestamp) -> sqlite3.Connection:
    """A copy of the database with nothing after `when` (a month-end)."""
    from trend_bot import db

    path = Path(path)
    path.unlink(missing_ok=True)
    con.execute("VACUUM INTO ?", (str(path),))
    out = db.connect(path)
    day = when.strftime("%Y-%m-%d")
    for table, col in CUTOFFS.items():
        out.execute(f"DELETE FROM {table} WHERE {col} > ?", (day,))
    out.execute("DELETE FROM short_volume WHERE month > ?", (when.strftime("%Y-%m"),))
    out.execute("DELETE FROM doc_vectors WHERE accession NOT IN (SELECT accession FROM filings)")
    out.commit()
    return out


def compare(full: pd.DataFrame, cut: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Per signal: stocks compared, how many differ, and the largest difference."""
    keep = lambda d: d[d["gone"].isna()] if "gone" in d else d
    a, b = keep(full).set_index("ticker"), keep(cut).set_index("ticker")
    both = a.index.intersection(b.index)
    cols = [c for c in a.columns if c in b.columns and c not in SKIP_COLUMNS
            and pd.api.types.is_numeric_dtype(a[c]) and pd.api.types.is_numeric_dtype(b[c])]
    rows = {}
    for c in cols:
        x, y = a.loc[both, c].astype(float), b.loc[both, c].astype(float)
        nan_mismatch = x.isna() != y.isna()
        gap = (x - y).abs().where(~(x.isna() | y.isna()))
        differ = nan_mismatch | (gap > 1e-9 * (1 + x.abs()))
        rows[c] = {"stocks": int((x.notna() | y.notna()).sum()), "differ": int(differ.sum()),
                   "max_gap": float(gap.max()) if gap.notna().any() else 0.0}
    universe = {"both": len(both), "only_full": len(a.index.difference(b.index)),
                "only_cut": len(b.index.difference(a.index))}
    return pd.DataFrame(rows).T, universe


def check(con: sqlite3.Connection, months=DEFAULT_MONTHS, workdir: str | Path = ".", log=print,
          full: pd.DataFrame | None = None) -> dict[str, tuple[pd.DataFrame, dict]]:
    """Run the check for each month-end; returns {month: (per-signal comparison, universe overlap)}."""
    from trend_bot import factors

    months = [pd.Timestamp(m) + pd.offsets.MonthEnd(0) for m in months]
    if full is None:
        log("[lookahead] building the normal signal table...")
        full = factors.monthly_factors(con, since=str(min(months).date()), log=lambda *_: None)
    out = {}
    for m in months:
        log(f"[lookahead] {m:%Y-%m}: copying the database as it was then and rebuilding that month...")
        path = Path(workdir) / f"lookahead-{m:%Y%m}.db"
        cut_con = truncated_copy(con, path, m)
        try:
            cut = factors.monthly_factors(cut_con, since=str(m.date()), include_latest=True, log=lambda *_: None)
        finally:
            cut_con.close()
            path.unlink(missing_ok=True)
            gc.collect()
        pick = lambda d: d[d["month"] == m] if "month" in d else d.iloc[0:0]
        a, b = pick(full), pick(cut)
        if a.empty or b.empty:
            log(f"[lookahead] {m:%Y-%m}: no rows to compare")
            continue
        out[f"{m:%Y-%m}"] = compare(a, b)
    return out


def verdict(results: dict[str, tuple[pd.DataFrame, dict]]) -> tuple[bool, list[str]]:
    """(clean?, signals that differed somewhere)."""
    bad = sorted({c for table, _ in results.values() for c, r in table.iterrows() if r["differ"] > 0})
    return (not bad and bool(results)), bad


def text(results: dict[str, tuple[pd.DataFrame, dict]]) -> str:
    lines = []
    for month, (table, uni) in results.items():
        lines.append(f"\n== {month}: {uni['both']:,} stocks in both versions "
                     f"(only with all data: {uni['only_full']}, only with data up to then: {uni['only_cut']}) ==")
        for c, r in table.sort_values("differ", ascending=False).iterrows():
            mark = "OK " if r["differ"] == 0 else "!! "
            lines.append(f"  {mark}{c:<22} {int(r['stocks']):>5} stocks, {int(r['differ']):>4} differ"
                         + (f", largest gap {r['max_gap']:.4g}" if r["differ"] else ""))
    clean, bad = verdict(results)
    lines.append("\n" + ("Clean: every signal was the same with only the data known at the time. No look-ahead found."
                         if clean else
                         f"Look-ahead suspected in: {', '.join(bad)}. These signals changed when future data was "
                         "removed, so the tests may have peeked at the future through them." if bad else
                         "Nothing could be compared."))
    return "\n".join(lines)
