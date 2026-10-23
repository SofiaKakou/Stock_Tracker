"""'Why isn't this stock ranked?' and a one-off audit of the data.

Every month the ranking keeps a note of which stocks it ranked (and where) and which it left out
for knowing too little about them. `why` combines that with what's in the database to explain any
ticker in plain words. `audit` checks the data itself for the kinds of mistakes that quietly
distort tests: weekend or holiday prices (a sign of dates filed in the wrong timezone), prices
from the future, impossible values, unexplained jumps, long gaps, and filings dated before the
period they report on. Ideas from passivbot (inclusion reasons, its data-integrity audit).
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pandas as pd

from trend_bot.db import get_meta, set_meta

STALE_DAYS = 10


def save(con: sqlite3.Connection, ranked: pd.DataFrame, date: str) -> None:
    """Keep this month's coverage: rank, percentile and measures known for each ranked stock, and the
    stocks left out for knowing too little."""
    rows = {t: [i + 1, round(float(r["pct"]), 4), int(r["signals"])] for i, (t, r) in enumerate(ranked.iterrows())}
    set_meta(con, "ideas_coverage", json.dumps({"date": date, "ranked": rows,
                                                "too_few": ranked.attrs.get("too_few", {})}))
    con.commit()


def why(con: sqlite3.Connection, ticker: str, universe: int = 1000, min_price: float = 5) -> list[str]:
    """Plain sentences: is this ticker ranked this month, and if not, why not."""
    from trend_bot import factors, ideas

    t = ticker.upper().strip()
    cov = json.loads(get_meta(con, "ideas_coverage") or "{}")
    ranked, too_few = cov.get("ranked", {}), cov.get("too_few", {})
    when = f" (ranking of {cov['date']})" if cov.get("date") else ""
    if t in ranked:
        rank, pct, n = ranked[t]
        return [f"{t} is ranked #{rank:,} of {len(ranked):,}{when}: better than {pct:.0%} of the ranked stocks, "
                f"scored on {n} measures."]
    row = con.execute("SELECT cik, name, last_date, last_close, dollar_vol, is_etf FROM tickers WHERE ticker = ?",
                      (t,)).fetchone()
    if not row:
        return [f"{t} isn't in the bot's list of US stocks (NYSE/Nasdaq listings). Check the spelling; "
                "share classes use a dash, e.g. BRK-B."]
    cik, name, last_date, last_close, dollar_vol, is_etf = row
    label = f"{t} ({name})" if name else t
    if t in factors.fund_tickers(con) or is_etf:
        return [f"{label} is a fund, not a company, so it isn't ranked (funds are only used as yardsticks)."]
    if cik is None:
        return [f"{label} has no SEC company number, so there are no financial reports to score it on."]
    primary = factors.primary_tickers(con).get(cik)
    if primary and primary != t:
        return [f"{label} is another share class of the same company as {primary}; only the most traded "
                f"class ({primary}) is ranked."]
    if t in too_few:
        return [f"{label} was among the most traded stocks{when}, but only {too_few[t]} of its measures were "
                f"known; at least {ideas.MIN_SIGNALS} are needed to score a stock fairly."]
    if not last_date:
        return [f"{label} has no prices in the database yet."]
    age = (dt.date.today() - dt.date.fromisoformat(last_date[:10])).days
    if age > STALE_DAYS:
        return [f"{label}'s latest price is from {last_date[:10]} ({age} days ago): it may have stopped trading."]
    first = con.execute("SELECT MIN(date) FROM prices WHERE ticker = ?", (t,)).fetchone()[0]
    if first and (dt.date.today() - dt.date.fromisoformat(first[:10])).days < 380:
        return [f"{label} has prices only since {first[:10]}: the price measures need about a year of history."]
    if last_close is not None and last_close < min_price:
        return [f"{label} trades at ${last_close:,.2f}, under the ${min_price:g} minimum (very cheap stocks are "
                "left out: they're hard to trade and their prices jump around)."]
    peers = [v for (v,) in con.execute(
        "SELECT dollar_vol FROM tickers WHERE dollar_vol IS NOT NULL AND last_close >= ? AND last_date >= ? "
        "AND COALESCE(is_etf, 0) = 0", (min_price, (dt.date.today() - dt.timedelta(days=STALE_DAYS)).isoformat()))]
    place = 1 + sum(v > (dollar_vol or 0) for v in peers)
    if place > universe:
        return [f"{label} trades about ${(dollar_vol or 0) / 1e6:,.1f}M a day, about #{place:,} by trading volume; only "
                f"the {universe:,} most traded stocks are ranked (enough to buy and sell easily)."]
    return [f"{label} looks eligible but wasn't in the last ranking{when}; it should appear at the next monthly "
            "update (for example if it only recently became one of the most traded)."]


# --- The data audit ------------------------------------------------------------------------------

HOLIDAYS = ["01-01", "07-04", "12-25"]     # US market closed (when on a weekday)


COVER_PAGE_ITEMS = ("shares", "public_float")   # cover-page counts are dated a few days after filing: normal


def _rankable(con: sqlite3.Connection) -> str:
    """SQL for the tickers that could ever be ranked: one per company, not a fund."""
    from trend_bot import factors

    tickers = list(factors.primary_tickers(con).values())
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _rankable (ticker TEXT PRIMARY KEY)")
    con.execute("DELETE FROM _rankable")
    con.executemany("INSERT OR IGNORE INTO _rankable VALUES (?)", [(t,) for t in tickers])
    return "SELECT ticker FROM _rankable"


def audit(con: sqlite3.Connection, examples: int = 5) -> list[dict]:
    """Checks on the stored data: [{check, ok, found, detail, handled}], each in plain words. Price checks
    only look at stocks that could be ranked (one share class per company, no funds, $5 or more), so
    the list shows what can matter. 'handled' marks problems the bot already cleans up when it reads."""
    out = []
    rankable = _rankable(con)

    def add(check, found, detail, rows=(), handled=""):
        ex = ", ".join(str(r) for r in list(rows)[:examples])
        out.append({"check": check, "ok": found == 0 or bool(handled), "found": int(found), "handled": handled,
                    "detail": detail + (f" e.g. {ex}" if found and ex else "")})

    today = dt.date.today().isoformat()
    rows = con.execute(f"SELECT ticker, date FROM prices WHERE strftime('%w', date) IN ('0', '6') "
                       f"AND ticker IN ({rankable}) LIMIT 1000").fetchall()
    add("Prices dated on a weekend", len(rows), "A weekend price usually means dates were filed in the wrong "
        "timezone (shifted by a day).", [f"{t} {d}" for t, d in rows])
    holiday = " OR ".join(f"substr(date, 6, 5) = '{h}'" for h in HOLIDAYS)
    rows = con.execute(f"SELECT ticker, date FROM prices WHERE ({holiday}) AND strftime('%w', date) NOT IN ('0','6') "
                       f"AND ticker IN ({rankable}) LIMIT 1000").fetchall()
    add("Prices dated on a market holiday", len(rows), "New Year's Day, July 4th and Christmas: the market is "
        "closed, so a price there is misdated.", [f"{t} {d}" for t, d in rows])
    rows = con.execute("SELECT ticker, date FROM prices WHERE date > ? LIMIT 1000", (today,)).fetchall()
    add("Prices from the future", len(rows), "Dates after today.", [f"{t} {d}" for t, d in rows])
    rows = con.execute(f"SELECT ticker, date FROM prices WHERE (close IS NULL OR close <= 0 OR adj_close <= 0) "
                       f"AND ticker IN ({rankable}) LIMIT 1000").fetchall()
    add("Impossible prices", len(rows), "Missing, zero or negative closing prices.", [f"{t} {d}" for t, d in rows],
        handled="treated as missing days when prices are read")
    rows = con.execute(f"""
        SELECT p.ticker, p.date, ROUND(p.adj_close / p.prev - 1, 2) FROM (
            SELECT ticker, date, adj_close, LAG(adj_close) OVER (PARTITION BY ticker ORDER BY date) AS prev
            FROM prices WHERE ticker IN ({rankable}) AND adj_close > 0) p
        WHERE p.prev > 0 AND (p.adj_close >= 5 OR p.prev >= 5)
          AND (p.adj_close / p.prev > 4 OR p.adj_close / p.prev < 0.25)
          AND NOT EXISTS (SELECT 1 FROM splits s WHERE s.ticker = p.ticker
                          AND s.date BETWEEN date(p.date, '-5 days') AND date(p.date, '+5 days'))
        LIMIT 1000""").fetchall()
    add("Unexplained one-day jumps", len(rows), "A day where the price rose more than 4x or fell more than 75% with "
        "no stock split nearby (in stocks at $5 or more).", [f"{t} {d} ({c:+.0%})" for t, d, c in rows],
        handled="one-day blips are dropped when prices are read, and the month of a lasting jump is left out of "
                "the tests")
    rows = con.execute(f"""
        SELECT ticker, prev, date FROM (
            SELECT ticker, date, LAG(date) OVER (PARTITION BY ticker ORDER BY date) AS prev FROM prices
            WHERE ticker IN ({rankable}))
        WHERE prev IS NOT NULL AND julianday(date) - julianday(prev) > 20 LIMIT 1000""").fetchall()
    add("Long gaps inside a price history", len(rows), "More than 20 days with no price while the stock was "
        "listed: trading halts, or missing data that would stretch returns over the gap.",
        [f"{t} {a}..{b}" for t, a, b in rows])
    skip = ",".join(f"'{i}'" for i in COVER_PAGE_ITEMS)
    rows = con.execute(f"SELECT cik, item, end, filed FROM facts WHERE filed < end AND item NOT IN ({skip}) "
                       "LIMIT 1000").fetchall()
    add("Financials filed before their period ended", len(rows), "A report can't be filed before the period it "
        "covers ends: a date mix-up that would leak future numbers. (Cover-page share counts, which are dated "
        "a few days after filing, are normal and not counted.)", [f"cik {c} {i} {e}/{f}" for c, i, e, f in rows])
    rows = con.execute("SELECT accession, filed FROM filings WHERE filed > ? UNION ALL "
                       "SELECT accession, filed FROM events WHERE filed > ? LIMIT 1000", (today, today)).fetchall()
    add("Filings from the future", len(rows), "Filing dates after today.", rows)
    rows = con.execute("SELECT ticker, settle FROM short_interest WHERE settle > ? LIMIT 1000", (today,)).fetchall()
    add("Short-selling data from the future", len(rows), "Settlement dates after today.", rows)
    return out


def save_audit(con: sqlite3.Connection, results: list[dict]) -> None:
    set_meta(con, "data_audit", json.dumps({"date": dt.date.today().isoformat(),
                                            "problems": [r["check"] for r in results if not r["ok"]]}))
    con.commit()


def audit_text(results: list[dict]) -> str:
    lines = []
    for r in results:
        n = f"{r['found']:,}{'+' if r['found'] >= 1000 else ''} found"
        if r["found"] == 0:
            lines.append(f"  OK {r['check']}: none found.")
        elif r.get("handled"):
            lines.append(f"  OK {r['check']}: {n}, handled ({r['handled']}). {r['detail']}")
        else:
            lines.append(f"  !! {r['check']}: {n}. {r['detail']}")
    bad = [r["check"] for r in results if not r["ok"]]
    lines.append("\n" + ("Clean: no problems found in the stored data." if not bad else
                         f"To look into: {', '.join(bad)}. These don't change anything by themselves; each one is "
                         "checked by hand before deciding whether it matters."))
    return "\n".join(lines)
