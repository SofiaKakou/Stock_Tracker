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

Two more measures come from the same download:

- Tone, in the spirit of Loughran and McDonald ("When is a liability not a liability?",
  Journal of Finance 2011): the share of negative, uncertain and legal-trouble words, from
  short lists of finance word stems written for this bot (general-purpose sentiment lists
  misread reports: "liability" or "tax" aren't bad news there). More negative wording, and
  wording that turned more negative than a year earlier, has tended to come before weaker
  returns.
- The risk factors section (Item 1A) on its own: in "Lazy Prices", changes there mattered
  most. `risk_change` is 1 - its similarity with a year earlier; `risk_growth` is how much
  longer it got.

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
RECENT_DAYS = 430        # read every company's last ~5 quarters first: enough for the year-on-year signals
LIQUID_TOP = 1000        # only companies that were among the most traded in some year (the studies' universe)
MIN_RISK_WORDS = 300     # shorter "risk factors" are usually "no material changes" notes
TONE_VERSION = 1         # bump when the word lists change, so reports are re-read

# Word stems (a word counts if it starts with one), written for company reports.
TONE_STEMS = {
    "negative": """abandon abus accident advers adverse bankrupt breach burden catastroph closure collaps
        complain condemn contamin corrupt crisis damag declin decline decreas default defect
        deficien deficit delay delinquen deplet deterior detriment difficult diminish disadvant
        disappoint disaster disclaim discontinu dismiss disput disrupt doubt downgrad downturn drop
        erod erosion error fail fall fallen falling falls fines fraud harmed harmful harms hurting hurts
        idle illegal impair imped inability inadequa incorrect ineffective insolven insufficien
        interrupt investigat lack lapse layoff liquidat lose loses losing loss losses lost
        malfunction misconduct misstat negativ neglect obsolet offens outag overdue penal penalt
        poor problem protest recall recession reject restat restructur retaliat revoc revok severe
        shortag shortfall slow slowdown slowed slower stagnat strain suspend suspens terminat theft
        unable unanticipat uncollect undermin underperform unfavorab unprofitab unsuccess violat
        volatil weak weaken weakness worse worsen worst writedown writeoff""",
    "uncertainty": """almost ambigu anticipat appear approximat assum believ conceiv conditional conting could
        depend doubt exposure fluctuat hidden imprecis indefinit likelihood may maybe might nearly
        occasional pending perhaps possib predict preliminar presum probab random reassess
        recalculat reconsider revis risk roughly seldom sometime speculat sudden suggest susceptib
        tentativ turbulen uncertain unclear unconfirm undecided undetermin unexpect unforecast
        unknown unobserv unplanned unpredict unprove unquantif unsettled untested unusual variab
        vary""",
    "litigious": """acquit adjudicat affidavit alleg amicus appeal appell arbitrat attorney bailiff
        claimant codefendant complainant counterclaim court crimin defendant deposition indict
        infring injunct judicial judge jurisdic juror jury lawsuit lawyer legal litig magistrat
        plaintiff plead prosecut remand settlement statut subpoena sued suing testif tort tribunal
        verdict whistleblow""",
    "positive": """achiev advantag attain attractiv benefit best better boost breakthrough collaborat
        compliment creativ delight dependab effectiv efficien enabl encourag enhanc enjoy enthusias
        exceed excel exceptional excit exclusiv favorab gain good great happy honor ideal impress
        improv incredib innovat insight invent leader leadership lucrativ opportun optimis
        outperform perfect pleas popular positiv premier prestig proactiv profitab progress prosper
        rebound resolv reward satisf smooth solv stabil strength strong succe superior surpass
        transparen tremendous unmatch unparallel upturn valuab versatil vibrant win winner""",
}
_STEM_RE = {k: re.compile("^(?:" + "|".join(sorted(set(v.split()), key=len, reverse=True)) + ")")
            for k, v in TONE_STEMS.items()}
_EXACT_ONLY = {"may", "fines", "fall", "lost", "lose", "idle", "good", "great", "best", "better",
               "win", "happy", "ideal", "judge"}   # "judge" would also catch "judgement"

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


def tone(ws: list[str]) -> dict[str, int]:
    """How many words fall in each tone list. Short stems that are also common words ("may",
    "fall", "good"...) count only as the exact word, not as the start of a longer one."""
    from collections import Counter

    counts = Counter(ws)
    out = {k: 0 for k in TONE_STEMS}
    for w, n in counts.items():
        for k, rx in _STEM_RE.items():
            m = rx.match(w)
            if m and (m.group(0) not in _EXACT_ONLY or m.group(0) == w):
                out[k] += n
    return out


_ITEM_HEADINGS = {"unresolved", "properties", "legal", "unregistered", "defaults", "mine", "other",
                  "exhibits", "quantitative", "controls", "selected", "market", "management", "financial"}


def risk_section(ws: list[str]) -> list[str]:
    """The words of the 'Item 1A. Risk Factors' section: from an 'item risk factors' heading to the
    next item heading, taking the longest such stretch (skips the table of contents)."""
    best: list[str] = []
    for i in range(len(ws) - 2):
        if ws[i] == "item" and ws[i + 1] == "risk" and ws[i + 2] == "factors":
            j = i + 3
            while j < len(ws) - 1 and not (ws[j] == "item" and ws[j + 1] in _ITEM_HEADINGS):
                j += 1
            if j - i > len(best):
                best = ws[i + 3 : j]
    return best


# --- Downloading ----------------------------------------------------------------------------

def liquid_ciks(con: sqlite3.Connection, top: int = LIQUID_TOP, since_year: int = FIRST_YEAR) -> set[int]:
    """Companies whose stock was among the `top` most traded (average daily $ volume) in at least one
    year since `since_year`: the ones the studies and the top ideas can ever pick."""
    dv = pd.read_sql_query(
        """SELECT p.ticker, substr(p.date, 1, 4) AS year, AVG(p.close * p.volume) AS dv, t.cik
           FROM prices p JOIN tickers t ON t.ticker = p.ticker
           WHERE p.date >= ? AND t.cik IS NOT NULL
           GROUP BY p.ticker, year""", con, params=[f"{since_year}-01-01"])
    if dv.empty:
        return set()
    dv = dv.dropna(subset=["dv"])
    best = dv[dv.groupby("year")["dv"].rank(ascending=False, method="first") <= top]
    return {int(c) for c in best["cik"]}


def to_fetch(con: sqlite3.Connection, ciks: set[int] | None = None, since_year: int = FIRST_YEAR,
             recent_days: int = RECENT_DAYS) -> pd.DataFrame:
    """Reports not read yet. First every company's reports from the last `recent_days` (today's
    report and the one a year earlier, so the year-on-year signals work right away), then the
    rest newest first as the history fills in."""
    cutoff = (pd.Timestamp.today() - pd.Timedelta(days=recent_days)).strftime("%Y-%m-%d")
    df = pd.read_sql_query(
        """SELECT f.accession, f.cik, f.form, f.filed, f.primary_doc FROM filings f
           LEFT JOIN doc_vectors v ON v.accession = f.accession
           WHERE (v.accession IS NULL OR (v.words > 0 AND COALESCE(v.tone_version, 0) < ?))
             AND f.primary_doc IS NOT NULL AND f.filed >= ?
           ORDER BY (f.filed >= ?) DESC, f.filed DESC""", con, params=[TONE_VERSION, f"{since_year}-01-01", cutoff])
    return df[df["cik"].isin(ciks)].reset_index(drop=True) if ciks is not None else df


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
            t, risk = tone(ws), risk_section(ws)
            risk_vec = pack(fingerprint(risk)) if len(risk) >= MIN_RISK_WORDS else None
            con.execute("""INSERT OR REPLACE INTO doc_vectors(accession, words, vec, negative, uncertainty,
                           litigious, positive, risk_words, risk_vec, tone_version)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (r.accession, len(ws), pack(fingerprint(ws)), t["negative"], t["uncertainty"],
                         t["litigious"], t["positive"], len(risk), risk_vec, TONE_VERSION))
        else:
            con.execute("INSERT OR REPLACE INTO doc_vectors(accession, words, vec) VALUES (?, 0, NULL)",
                        (r.accession,))
        done += 1
        if done % 50 == 0:
            con.commit()
        if done % 500 == 0:
            log(f"[reports] {done:,}/{len(todo):,} ({time.time() - started:.0f}s)")
    con.commit()
    return done


# --- The signal ---------------------------------------------------------------------------------

SIGNALS = ["report_change", "tone_negative", "tone_change", "uncertainty", "litigious",
           "risk_change", "risk_growth"]


def changes(con: sqlite3.Connection) -> pd.DataFrame:
    """Every readable report's text signals, most compared with the same kind of report about a
    year earlier.

    Columns: cik, form, filed, similarity, then SIGNALS:
      report_change  1 - similarity of the whole report's wording (NaN without a year-earlier report)
      tone_negative  share of negative words
      tone_change    change in (negative - positive words) / words vs a year earlier
      uncertainty, litigious  share of uncertain and legal words
      risk_change    1 - similarity of the risk factors section vs a year earlier
      risk_growth    log of how much longer the risk factors section got
    A 10-Q is matched with the 10-Q whose period ended 330-400 days earlier (the same quarter
    last year); a 10-K with the previous 10-K.
    """
    df = pd.read_sql_query(
        """SELECT f.accession, f.cik, f.form, f.filed, COALESCE(f.period, f.filed) AS period, v.words, v.vec,
                  v.negative, v.uncertainty, v.litigious, v.positive, v.risk_words, v.risk_vec
           FROM filings f JOIN doc_vectors v ON v.accession = f.accession
           WHERE v.words > 0""", con, parse_dates=["filed", "period"])
    cols = ["cik", "form", "filed", "similarity"] + SIGNALS
    if df.empty:
        return pd.DataFrame(columns=cols)
    words = df["words"].astype(float)
    df["tone_negative"] = df["negative"] / words
    df["uncertainty"] = df["uncertainty"] / words
    df["litigious"] = df["litigious"] / words
    df["net_neg"] = (df["negative"] - df["positive"]) / words
    rows = []
    for (cik, form), g in df.groupby(["cik", "form"]):
        g = g.sort_values("period").reset_index(drop=True)
        vecs = [unpack(b) for b in g["vec"]]
        risks = [unpack(b) if b is not None else None for b in g["risk_vec"]]
        periods = g["period"].to_numpy()
        for i in range(len(g)):
            lag = (periods[i] - periods[:i]).astype("timedelta64[D]").astype(int)
            j = np.where((lag >= 330) & (lag <= 400))[0]
            sim = tone_chg = risk_chg = risk_grow = np.nan
            if len(j):
                k = j[-1]
                sim = cosine(vecs[i], vecs[k])
                tone_chg = g.at[i, "net_neg"] - g.at[k, "net_neg"]
                if risks[i] is not None and risks[k] is not None:
                    risk_chg = 1 - cosine(risks[i], risks[k])
                    risk_grow = float(np.log(g.at[i, "risk_words"] / g.at[k, "risk_words"]))
            rows.append((cik, form, g.at[i, "filed"], sim, 1 - sim, g.at[i, "tone_negative"], tone_chg,
                         g.at[i, "uncertainty"], g.at[i, "litigious"], risk_chg, risk_grow))
    return pd.DataFrame(rows, columns=cols)


def latest(ch: pd.DataFrame, when: pd.Timestamp, fresh_days: int = FRESH_DAYS,
           column: str = "report_change") -> pd.Series:
    """Per company: `column` from the most recent report filed in the `fresh_days` up to `when`
    that has a value for it."""
    recent = ch[(ch["filed"] <= when) & (ch["filed"] > when - pd.Timedelta(days=fresh_days))].dropna(subset=[column])
    return recent.sort_values("filed").drop_duplicates("cik", keep="last").set_index("cik")[column]
