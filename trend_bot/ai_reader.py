"""Claude reads company reports and judges the outlook (Phase 2, a paid trial).

For a sample of 10-K / 10-Q reports the bot downloads the main document from the SEC, cuts
out the management's discussion (MD&A), hides who the company is and which year it is,
and asks Claude for a score from -5 (likely to lag the market over the next year) to +5
(likely to beat it), with its reasons. Requests go through the Message Batches API, which
costs half the normal price; the bot records the real tokens used, so the cost is known.

Hindsight is the big trap: the model has read the news up to its training date, so on past
reports it might simply remember what happened. To limit that, names, tickers and years are
removed, and the model is asked which company it thinks this is. Scores where it
recognized the company are reported separately. Even so, only reports filed after the
model's training are a clean test, so the forward record is what finally counts.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import sqlite3
import time
from typing import Callable

import numpy as np
import pandas as pd

from trend_bot import db, insiders, text_changes

MODEL = "claude-opus-5-5"
PRICE_IN, PRICE_OUT = 2.0, 10.0    # $ per million tokens with the Batch API (half of $4 / $20)
MAX_CHARS = 40_000                 # about 10k tokens of report text per request
MAX_TOKENS = 6_000                 # room for the model's thinking plus the short JSON answer
SAMPLE_FROM, SAMPLE_TO = "2023-01-01", "2025-12-31"   # old enough to see what happened next
HORIZON = 63                       # trading days after the filing used to grade it (~3 months)

SYSTEM = """You are an experienced equity analyst. You will read an excerpt from a US public \
company's annual or quarterly report (usually management's discussion and analysis). The \
company's name and ticker are replaced by "the Company", and calendar years are written \
relative to the report's year: Y0 is the report's own year, Y-1 the year before, and so on.

Judge, from this text alone, how the company's stock is likely to do over the next 12 months \
compared with the overall US stock market. Look at the quality and direction of the business: \
growth, margins, cash, debt, demand, guidance, the candor and tone of management, risks that \
are new or getting worse, and anything that looks evasive.

Do not try to work out who the company is, and do not use any knowledge of what happened \
later. If you nonetheless recognize the company, say which one in "recognized"; otherwise \
write "unknown". Be calibrated: most companies deserve a score near 0."""

SCHEMA = {
    "type": "object",
    "properties": {
        "outlook": {"type": "integer", "description": "-5 = very likely to lag the market, 0 = in line, +5 = very likely to beat it"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reasons": {"type": "array", "items": {"type": "string"}, "description": "the 2-4 main reasons, one short sentence each"},
        "recognized": {"type": "string", "description": "the company you think this is, or 'unknown'"},
    },
    "required": ["outlook", "confidence", "reasons", "recognized"],
    "additionalProperties": False,
}

_HIDDEN = re.compile(r"<(ix:header|script|style)[^>]*>.*?</\1>", re.S | re.I)
_BREAKS = re.compile(r"<(br|/p|/div|/tr|/h\d|/li)[^>]*>", re.I)
_TAGS = re.compile(r"<[^>]+>")
_MDA = re.compile(r"management[’'`s]*\s+discussion\s+and\s+analysis", re.I)
_MDA_END = re.compile(r"quantitative\s+and\s+qualitative\s+disclosures?\s+about\s+market\s+risk", re.I)
_YEAR = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")


# --- Preparing the text -----------------------------------------------------------------------

def plain_text(raw: bytes) -> str:
    """Readable text of an EDGAR HTML document: line breaks kept, tags and XBRL data dropped."""
    text = raw.decode("utf-8", "replace")
    text = _HIDDEN.sub(" ", text)
    text = _TAGS.sub(" ", _BREAKS.sub("\n", text))
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def mdna(text: str, max_chars: int = MAX_CHARS) -> str:
    """The MD&A section: the heading followed by the most text before the market-risk item
    (that skips the table of contents). Falls back to the middle of the document."""
    best, best_len = None, 0
    for m in _MDA.finditer(text):
        end = _MDA_END.search(text, m.end())
        length = (end.start() if end else len(text)) - m.start()
        if length > best_len:
            best, best_len = m.start(), length
    if best is None or best_len < 2000:
        mid = max(0, len(text) // 2 - max_chars // 2)
        return text[mid : mid + max_chars]
    return text[best : best + min(best_len, max_chars)]


def anonymize(text: str, names: list[str], tickers: list[str], year: int) -> str:
    """Hide who and when: company names and tickers become 'the Company', years become Y0, Y-1, ..."""
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        words = re.sub(r"[,.]|\b(inc|corp|corporation|co|ltd|plc|llc|holdings?|group|company)\b", " ", name, flags=re.I).split()
        variants = {name, " ".join(words)}
        if words and len(words[0]) >= 4:
            variants.add(words[0])   # "Apple" from "Apple Inc."
        for v in sorted((v for v in variants if len(v) >= 3), key=len, reverse=True):
            text = re.sub(rf"(?<!\w){re.escape(v)}(?:[’']s)?(?!\w)", "the Company", text, flags=re.I)
    for t in tickers:
        if t and len(t) >= 2:
            text = re.sub(rf"\b{re.escape(t)}\b", "the Company", text)

    def rel(m: re.Match) -> str:
        d = int(m.group(1)) - year
        return "Y0" if d == 0 else f"Y{d:+d}"

    return _YEAR.sub(rel, text)


# --- Choosing and sending ---------------------------------------------------------------------

def sample(con: sqlite3.Connection, n: int, seed: int = 7, since: str = SAMPLE_FROM, until: str = SAMPLE_TO) -> pd.DataFrame:
    """A random sample of readable reports of tracked companies not scored yet, spread over time."""
    from trend_bot import factors

    universe = factors.primary_tickers(con)
    df = pd.read_sql_query(
        """SELECT f.accession, f.cik, f.form, f.filed, f.primary_doc FROM filings f
           JOIN doc_vectors v ON v.accession = f.accession
           LEFT JOIN ai_scores s ON s.accession = f.accession
           WHERE v.words > 0 AND s.accession IS NULL AND f.filed BETWEEN ? AND ?""",
        con, params=[since, until])
    df = df[df["cik"].isin(universe)]
    if df.empty:
        return df
    df["ticker"] = df["cik"].map(universe)
    df["quarter"] = df["filed"].str[:4] + "Q" + ((df["filed"].str[5:7].astype(int) - 1) // 3 + 1).astype(str)
    # Same number from every quarter, so no single period dominates.
    per = max(1, -(-n // df["quarter"].nunique()))
    picked = df.groupby("quarter", group_keys=False).apply(lambda g: g.sample(min(len(g), per), random_state=seed))
    return picked.sample(min(n, len(picked)), random_state=seed).sort_values("filed").reset_index(drop=True)


def build_request(accession: str, excerpt: str) -> dict:
    return {
        "custom_id": accession,
        "params": {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "system": SYSTEM,
            "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
            "messages": [{"role": "user", "content": f"<report_excerpt>\n{excerpt}\n</report_excerpt>"}],
        },
    }


def prepare(con: sqlite3.Connection, picks: pd.DataFrame, fetch: Callable[[str], bytes] | None = None,
            log: Callable[[str], None] = print) -> list[dict]:
    """Download, cut and anonymize each picked report; returns the batch requests."""
    import requests as rq

    fetch = fetch or insiders._get
    reqs = []
    for r in picks.itertuples(index=False):
        url = text_changes.ARCHIVE_URL.format(cik=r.cik, acc=r.accession.replace("-", ""), doc=r.primary_doc)
        try:
            text = plain_text(fetch(url))
        except rq.HTTPError:
            continue
        names = [x for (x,) in con.execute("SELECT DISTINCT name FROM tickers WHERE cik = ?", (int(r.cik),))]
        tickers = [x for (x,) in con.execute("SELECT ticker FROM tickers WHERE cik = ?", (int(r.cik),))]
        excerpt = anonymize(mdna(text), names, tickers, int(r.filed[:4]))
        if len(excerpt) >= 2000:
            reqs.append(build_request(r.accession, excerpt))
    log(f"[ai] {len(reqs)} of {len(picks)} reports prepared "
        f"(~{sum(len(q['params']['messages'][0]['content']) for q in reqs) / 4 / 1e6:.2f}M tokens of text)")
    return reqs


def submit(con: sqlite3.Connection, client, reqs: list[dict]) -> str:
    batch = client.messages.batches.create(requests=reqs)
    con.execute("INSERT OR REPLACE INTO ai_batches VALUES (?, ?, ?, 'submitted')",
                (batch.id, dt.datetime.now().isoformat(timespec="seconds"), len(reqs)))
    con.commit()
    return batch.id


def collect(con: sqlite3.Connection, client, batch_id: str, wait_seconds: int = 0,
            log: Callable[[str], None] = print) -> bool:
    """Store a finished batch's scores. Waits up to `wait_seconds`; False if it isn't done yet."""
    deadline = time.time() + wait_seconds
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            break
        if time.time() >= deadline:
            log(f"[ai] batch {batch_id} still running; the next run collects it")
            return False
        time.sleep(60)
    now = dt.datetime.now().isoformat(timespec="seconds")
    failed = 0
    for res in client.messages.batches.results(batch_id):
        if res.result.type != "succeeded":
            failed += 1
            continue
        msg = res.result.message
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            out = json.loads(text)
        except json.JSONDecodeError:
            failed += 1
            continue
        con.execute("INSERT OR REPLACE INTO ai_scores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (res.custom_id, MODEL, int(np.clip(out["outlook"], -5, 5)), out["confidence"],
                     json.dumps(out["reasons"]), out["recognized"], msg.usage.input_tokens,
                     msg.usage.output_tokens, now))
    con.execute("UPDATE ai_batches SET status = 'collected' WHERE batch_id = ?", (batch_id,))
    con.commit()
    if failed:
        log(f"[ai] {failed} request(s) failed or gave no usable answer")
    return True


def pending_batches(con: sqlite3.Connection) -> list[str]:
    return [b for (b,) in con.execute("SELECT batch_id FROM ai_batches WHERE status = 'submitted' ORDER BY created")]


# --- Grading ----------------------------------------------------------------------------------

def graded(con: sqlite3.Connection, horizon: int = HORIZON, benchmark: str = "SPY") -> pd.DataFrame:
    """Every score with the stock's return vs the benchmark over `horizon` trading days after filing."""
    from trend_bot import factors

    df = pd.read_sql_query(
        """SELECT s.*, f.cik, f.form, f.filed FROM ai_scores s JOIN filings f ON f.accession = s.accession""",
        con, parse_dates=["filed"])
    if df.empty:
        return df
    universe = factors.primary_tickers(con)
    df["ticker"] = df["cik"].map(universe)
    prices = db.load_many(con, sorted(set(df["ticker"].dropna())) + [benchmark])
    bench = prices.get(benchmark)

    def ret(close: pd.Series, when: pd.Timestamp) -> float:
        after = close[close.index > when]
        return after.iloc[horizon] / after.iloc[0] - 1 if len(after) > horizon else np.nan

    excess = []
    for r in df.itertuples(index=False):
        p = prices.get(r.ticker)
        if p is None or bench is None:
            excess.append(np.nan)
            continue
        excess.append(ret(p["Close"], r.filed) - ret(bench["Close"], r.filed))
    df["excess"] = excess
    ch = text_changes.changes(con)[["cik", "form", "filed", "report_change"]]
    return df.merge(ch, on=["cik", "form", "filed"], how="left")


def summary(con: sqlite3.Connection) -> str:
    df = graded(con)
    if df.empty:
        return "No AI scores yet."
    cost = (df["input_tokens"].sum() * PRICE_IN + df["output_tokens"].sum() * PRICE_OUT) / 1e6
    lines = [
        f"AI report reader ({MODEL}, Batch API): {len(df)} reports scored",
        f"Cost: ${cost:.2f} in total, ${cost / len(df):.3f} per report "
        f"(avg {df['input_tokens'].mean():,.0f} tokens in, {df['output_tokens'].mean():,.0f} out incl. thinking)",
        f"  -> {len(df) / max(cost, 1e-9) * 100:,.0f} reports per $100; every tracked company's latest report "
        f"(~4,000 per quarter) would cost about ${cost / len(df) * 4000:,.0f}",
        "",
        "Scores given: " + ", ".join(f"{k:+d}: {v}" for k, v in df["outlook"].value_counts().sort_index().items()),
        "Confidence: " + ", ".join(f"{k} {v}" for k, v in df["confidence"].value_counts().items()),
    ]
    known = df["recognized"].str.strip().str.lower().ne("unknown")
    lines.append(f"Recognized the company despite the hiding: {known.sum()} of {len(df)} ({known.mean():.0%})")
    lines += ["", f"How the scores lined up with the next {HORIZON} trading days (~3 months) vs the S&P 500:"]
    for label, part in [("all", df), ("company not recognized", df[~known])]:
        g = part.dropna(subset=["excess"])
        if len(g) < 10:
            lines.append(f"  {label}: too few graded ({len(g)})")
            continue
        ic = g["outlook"].corr(g["excess"], method="spearman")
        pos, neg = g[g["outlook"] > 0]["excess"], g[g["outlook"] < 0]["excess"]
        lines.append(f"  {label}: IC {ic:+.3f} on {len(g)} reports; positive scores {pos.mean():+.1%} vs market "
                     f"({len(pos)}), negative {neg.mean():+.1%} ({len(neg)})")
    rc = df.dropna(subset=["excess", "report_change"])
    if len(rc) >= 10:
        lines.append(f"  free 'report changed' signal on the same reports: IC "
                     f"{-rc['report_change'].corr(rc['excess'], method='spearman'):+.3f} ({len(rc)}; sign flipped so + is good)")
    lines += ["", "A sample this small is noisy (an IC needs hundreds of reports to mean much), and past reports",
              "can leak hindsight even when hidden. The real test is reports filed from now on."]
    examples = df.sort_values("outlook").iloc[[0, -1]]
    lines += ["", "Examples:"]
    for r in examples.itertuples(index=False):
        lines.append(f"  {r.ticker} {r.form} {r.filed:%Y-%m-%d}: {r.outlook:+d} ({r.confidence}) "
                     f"{'; '.join(json.loads(r.reasons))[:300]}")
    return "\n".join(lines)
