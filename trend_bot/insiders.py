"""Insider trades from SEC Form 4 filings (free, official, no API key).

Company insiders (officers, directors, 10%+ owners) must report trades in
their own company's stock within two business days on Form 4.

What matters most:
- Open-market PURCHASES (code "P"): an insider spending their own money.
  Several insiders buying within a few weeks ("cluster buying") is the
  signal with the best track record in academic studies.
- Open-market SALES (code "S") are much weaker: insiders sell for taxes,
  diversification, houses... and many sales are pre-scheduled 10b5-1 plans.
- Awards, option exercises, tax withholding and gifts are ignored by default.

The SEC asks every automated client to identify itself. Put this in .env:

    SEC_USER_AGENT=Your Name your.email@example.com
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import requests

CACHE_DIR = Path("data_cache") / "sec"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
REQUEST_GAP = 0.15  # seconds between requests; SEC allows at most 10 per second

CODE_NAMES = {
    "P": "BUY", "S": "SELL", "A": "AWARD", "M": "OPTION EXERCISE", "F": "TAX WITHHOLDING",
    "G": "GIFT", "C": "CONVERSION", "D": "DISPOSED TO ISSUER", "X": "OPTION EXERCISE",
}
MARKET_CODES = ("P", "S")  # open-market purchases and sales


@dataclass
class InsiderTrade:
    ticker: str
    filed: dt.date
    date: dt.date
    insider: str
    role: str
    code: str
    shares: float
    price: float | None
    owned_after: float | None
    planned: bool          # made under a pre-scheduled 10b5-1 plan
    accession: str

    @property
    def kind(self) -> str:
        return CODE_NAMES.get(self.code, self.code)

    @property
    def value(self) -> float:
        return self.shares * (self.price or 0.0)


# --- HTTP with identification, pacing and caching -----------------------------

_last_request = 0.0


def user_agent(env_file: str | Path = ".env") -> str:
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua and Path(env_file).exists():
        for line in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "SEC_USER_AGENT":
                ua = value.strip().strip('"').strip("'")
    if not ua:
        raise RuntimeError(
            "The SEC requires a name and email with each request. Add a line like\n"
            "  SEC_USER_AGENT=Your Name your.email@example.com\n"
            "to the .env file in the Stock_Tracker folder."
        )
    return ua


def _get(url: str) -> bytes:
    global _last_request
    wait = REQUEST_GAP - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    resp = requests.get(url, headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"}, timeout=20)
    _last_request = time.time()
    if resp.status_code == 403:
        raise RuntimeError("SEC refused the request (403). Check SEC_USER_AGENT in .env has a name and an email.")
    resp.raise_for_status()
    return resp.content


def _cached(path: Path, url: str, max_age_hours: float | None) -> bytes:
    """Fetch `url`, reusing `path` if it's younger than max_age_hours (None = forever)."""
    if path.exists():
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if max_age_hours is None or age_hours < max_age_hours:
            return path.read_bytes()
    data = _get(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


# --- Lookups -------------------------------------------------------------------

def ticker_to_cik(ticker: str) -> int | None:
    raw = _cached(CACHE_DIR / "company_tickers.json", TICKERS_URL, max_age_hours=24 * 7)
    for entry in json.loads(raw).values():
        if entry["ticker"].upper() == ticker.upper().replace(".", "-"):
            return int(entry["cik_str"])
    return None


def recent_form4s(cik: int, since: dt.date) -> list[dict]:
    """Form 4 filings (accession, filing date, document) on or after `since`, newest first."""
    raw = _cached(CACHE_DIR / f"submissions_{cik}.json", SUBMISSIONS_URL.format(cik=cik), max_age_hours=1)
    recent = json.loads(raw)["filings"]["recent"]
    out = []
    for form, acc, filed, doc in zip(recent["form"], recent["accessionNumber"],
                                     recent["filingDate"], recent["primaryDocument"]):
        filed_date = dt.date.fromisoformat(filed)
        if form == "4" and filed_date >= since:
            # primaryDocument points at a styled HTML view ("xslF345X05/x.xml");
            # the raw XML has the same name without the folder.
            out.append({"accession": acc, "filed": filed_date, "document": doc.split("/")[-1]})
    return out


# --- Form 4 XML parsing ----------------------------------------------------------

def _text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path)
    if found is None:
        return None
    # Most Form 4 fields wrap their content in <value>.
    value = found.find("value")
    text = (value.text if value is not None else found.text) or ""
    return text.strip() or None


def _num(node: ET.Element, path: str) -> float | None:
    text = _text(node, path)
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def _flag(node: ET.Element | None, path: str) -> bool:
    return (_text(node, path) or "").lower() in ("1", "true")


def _role(owner: ET.Element) -> str:
    rel = owner.find("reportingOwnerRelationship")
    parts = []
    if _flag(rel, "isOfficer"):
        parts.append(_text(rel, "officerTitle") or "Officer")
    if _flag(rel, "isDirector"):
        parts.append("Director")
    if _flag(rel, "isTenPercentOwner"):
        parts.append("10% owner")
    if _flag(rel, "isOther"):
        parts.append(_text(rel, "otherText") or "Other")
    return ", ".join(parts) or "Insider"


def parse_form4(xml: bytes | str, ticker: str, filed: dt.date, accession: str) -> list[InsiderTrade]:
    """All non-derivative (common stock) transactions in one Form 4."""
    root = ET.fromstring(xml)
    owners = root.findall("reportingOwner")
    insider = " & ".join(_text(o, "reportingOwnerId/rptOwnerName") or "?" for o in owners) or "?"
    role = _role(owners[0]) if owners else "Insider"
    doc_planned = _flag(root, "aff10b5One")

    trades = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding/transactionCode")
        date = _text(tx, "transactionDate")
        shares = _num(tx, "transactionAmounts/transactionShares")
        if not code or not date or shares is None:
            continue
        trades.append(InsiderTrade(
            ticker=ticker.upper(),
            filed=filed,
            date=dt.date.fromisoformat(date[:10]),
            insider=insider.title(),
            role=role,
            code=code,
            shares=shares,
            price=_num(tx, "transactionAmounts/transactionPricePerShare"),
            owned_after=_num(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction"),
            planned=doc_planned,
            accession=accession,
        ))
    return trades


def fetch_trades(ticker: str, days: int = 90, market_only: bool = True,
                 today: dt.date | None = None, max_filings: int = 150) -> list[InsiderTrade]:
    """Insider trades in `ticker` filed in the last `days` days, newest first."""
    cik = ticker_to_cik(ticker)
    if cik is None:
        return []  # not an SEC-registered operating company (e.g. many ETFs, foreign tickers)
    since = (today or dt.date.today()) - dt.timedelta(days=days)
    trades: list[InsiderTrade] = []
    for f in recent_form4s(cik, since)[:max_filings]:
        acc = f["accession"].replace("-", "")
        cache = CACHE_DIR / "form4" / f"{acc}.xml"
        url = ARCHIVE_URL.format(cik=cik, accession=acc, document=f["document"])
        try:
            xml = _cached(cache, url, max_age_hours=None)  # filings never change
            trades.extend(parse_form4(xml, ticker, f["filed"], f["accession"]))
        except (ET.ParseError, requests.RequestException) as e:
            print(f"[skip] {ticker} filing {f['accession']}: {e}")
    if market_only:
        trades = [t for t in trades if t.code in MARKET_CODES]
    return sorted(trades, key=lambda t: (t.date, t.filed), reverse=True)


# --- Signals ---------------------------------------------------------------------

@dataclass
class InsiderSummary:
    buys: int
    sells: int
    buy_value: float
    sell_value: float
    buyers: list[str]

    @property
    def short(self) -> str:
        if not self.buys and not self.sells:
            return "none"
        parts = []
        if self.buys:
            parts.append(f"{self.buys} buy ${_money(self.buy_value)}")
        if self.sells:
            parts.append(f"{self.sells} sell ${_money(self.sell_value)}")
        return " / ".join(parts)


def _money(v: float) -> str:
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= size:
            return f"{v / size:.1f}{unit}"
    return f"{v:,.0f}"


def summarize(trades: list[InsiderTrade]) -> InsiderSummary:
    buys = [t for t in trades if t.code == "P"]
    sells = [t for t in trades if t.code == "S"]
    return InsiderSummary(
        buys=len(buys), sells=len(sells),
        buy_value=sum(t.value for t in buys), sell_value=sum(t.value for t in sells),
        buyers=sorted({t.insider for t in buys}),
    )


def cluster_buy(trades: list[InsiderTrade], window_days: int = 30, min_buyers: int = 2) -> list[InsiderTrade] | None:
    """Open-market buys by at least `min_buyers` different insiders within `window_days`.

    Returns the buys in the most recent such cluster, or None.
    """
    buys = sorted((t for t in trades if t.code == "P"), key=lambda t: t.date, reverse=True)
    for i, latest in enumerate(buys):
        start = latest.date - dt.timedelta(days=window_days)
        group = [t for t in buys[i:] if t.date >= start]
        if len({t.insider for t in group}) >= min_buyers:
            return group
    return None
