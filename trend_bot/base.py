"""The bot's village: every part of the bot drawn as a building, so you can see at a glance what works.

A game-style web page (one self-contained file, like the report). Each building is one part of the bot,
and its flag says how it's doing, read from the same database the bot uses every night:

  🟢 working · 🟡 needs a look · 🔴 broken (smoke) · ⚪ not set up yet (scaffolding)

  Town hall      the bot's overall health (the same checks as the ⚠️ Bot health message)
  Price mine     prices from Yahoo Finance
  SEC library    company financials, filings and insider trades from the SEC
  Watchtower     short selling data from FINRA
  News crawler   news headlines (Alpaca) and the economy (FRED)
  Ideas lab      the monthly top-ideas list and the data audit
  Army camp      the demo accounts (fake money)
  Arena          the day-trading demo account
  Gate           the nightly safety check (emergency exits)
  Vault          the trading journal in SQL Server
  Clock tower    the logbook: every command run and whether it finished

Click a building for the details. Nothing here changes anything; it only reads.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import sqlite3
from pathlib import Path

import numpy as np

from trend_bot import health
from trend_bot.db import get_meta

OK, WARN, BAD, OFF = "ok", "warn", "bad", "off"
RANK = {OK: 0, OFF: 0, WARN: 1, BAD: 2}
LABEL = {OK: "Working", WARN: "Needs a look", BAD: "Broken", OFF: "Not set up"}

# Words in reports/problems.txt (steps of tonight's run that didn't finish) -> the building they belong to.
STEP_WORDS = {"mine": ("data update", "price retry", "saved database"),
              "camp": ("demo account", "$100 account", "demo accounts"),
              "crawler": ("collecting news",), "lab": ("ranking snapshot", "study", "audit", "ideas"),
              "gate": ("safety",), "vault": ("trading journal",)}


def _age(value: str | None, today: dt.date) -> int | None:
    if not value:
        return None
    return (today - dt.date.fromisoformat(str(value)[:10])).days


def _busdays(value: str | None, today: dt.date) -> int | None:
    if not value:
        return None
    return int(np.busday_count(dt.date.fromisoformat(str(value)[:10]), today))


def _worst(*states: str) -> str:
    return max(states, key=lambda s: RANK[s])


def _one(con: sqlite3.Connection, sql: str, *params):
    try:
        return con.execute(sql, params).fetchone()
    except sqlite3.Error:
        return None


def _last_run(con: sqlite3.Connection, command: str) -> dict | None:
    row = _one(con, "SELECT started, exit_code, error FROM run_log WHERE command = ? ORDER BY started DESC LIMIT 1",
               command)
    return {"started": row[0], "ok": row[1] == 0 and not row[2], "error": row[2]} if row else None


def _ago(days: int | None) -> str:
    if days is None:
        return "never"
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


def _building(key: str, name: str, role: str, status: str, headline: str, details: list[str],
              stat: str = "") -> dict:
    return {"key": key, "name": name, "role": role, "status": status, "headline": headline,
            "details": details, "stat": stat}


def _failed(failed_steps: list[str], key: str) -> list[str]:
    return [s for s in failed_steps if any(w in s.lower() for w in STEP_WORDS.get(key, ()))]


def survey(con: sqlite3.Connection, today: dt.date | None = None, failed_steps: list[str] | None = None,
           job_status: str | None = None, daytrade_path: str | Path | None = "daytrade.json") -> list[dict]:
    """Every building with its status, a one-line headline and the details (town hall first)."""
    today = today or dt.date.today()
    failed_steps = failed_steps or []
    out = []

    # Price mine: Yahoo prices.
    stocks, last = _one(con, "SELECT COUNT(*), MAX(last_date) FROM tickers") or (0, None)
    behind = _busdays(last, today)
    status = BAD if behind is None or behind > 5 else WARN if behind > 2 else OK
    steps = _failed(failed_steps, "mine")
    status = _worst(status, WARN if steps else OK)
    out.append(_building(
        "mine", "Price mine", "Prices from Yahoo Finance, every weekday evening", status,
        "No prices yet" if last is None else f"Prices up to {last[:10]}"
        + ("" if not behind or behind <= 1 else f" ({behind} trading days behind)"),
        [f"{stocks:,} stocks in the database", f"Last price update: {_ago(_age(get_meta(con, 'prices_updated'), today))}"]
        + [f"Tonight: {s}" for s in steps], f"{stocks:,} stocks"))

    # SEC library: financials, the bulk file, insider trades, and whether the SEC asked us to slow down.
    facts = _age(get_meta(con, "facts_updated"), today)
    bulk = _age(get_meta(con, "submissions_updated"), today)
    newest = (_one(con, "SELECT MAX(filed) FROM facts") or [None])[0]
    insider = (_one(con, "SELECT MAX(filed) FROM insider_trades") or [None])[0]
    paused = get_meta(con, "sec_paused_until")
    paused = paused if paused and paused > dt.datetime.now().isoformat() else None
    status = OK
    if facts is None or bulk is None:
        status = BAD
    elif facts > 30 or bulk > 30 or (_age(newest, today) or 0) > health.NO_NEW_FIGURES_DAYS:
        status = BAD
    elif facts > 10 or bulk > 10 or paused:
        status = WARN
    details = [f"Company financials updated {_ago(facts)}", f"SEC bulk file (industries, company fates) read {_ago(bulk)}",
               f"Newest company figures filed {newest[:10] if newest else 'never'}",
               f"Newest insider trade filed {insider[:10] if insider else 'never'}"]
    if paused:
        details.insert(0, f"The SEC asked us to slow down: paused until {paused[:16].replace('T', ' ')} UTC")
    out.append(_building("library", "SEC library", "Financials, filings and insider trades from the SEC", status,
                         "Paused: the SEC asked us to slow down" if paused
                         else "Financials not downloaded yet" if facts is None else f"Financials updated {_ago(facts)}",
                         details, f"filed to {newest[:10]}" if newest else ""))

    # Watchtower: FINRA short selling.
    age = _age(get_meta(con, "short_volume_last"), today)
    status = BAD if age is None or age > 14 else WARN if age > 7 else OK
    out.append(_building("tower", "Watchtower", "Short selling data from FINRA", status,
                         "No short selling data yet" if age is None else f"Short selling data from {_ago(age)}",
                         [f"Latest short selling day: {get_meta(con, 'short_volume_last') or 'never'}"]))

    # News crawler: Alpaca headlines and FRED economy (optional: off until the keys exist).
    news_n, news_last = _one(con, "SELECT COUNT(*), MAX(created) FROM news") or (0, None)
    macro_last = (_one(con, "SELECT MAX(date) FROM macro") or [None])[0]
    steps = _failed(failed_steps, "crawler")
    if not news_n and not macro_last and not steps:
        status = OFF
        headline = "Not collecting yet (needs the Alpaca and FRED keys)"
    else:
        news_age = _busdays(news_last, today)
        status = WARN if steps or news_age is None or news_age > 3 else OK
        headline = f"{news_n:,} headline{'s' * (news_n != 1)}, newest {_ago(_age(news_last, today))}"
    out.append(_building("crawler", "News crawler", "News headlines (Alpaca) and the economy (FRED)", status, headline,
                         [f"Headlines collected: {news_n:,}", f"Newest headline: {news_last[:16] if news_last else 'none'}",
                          f"Newest economy figure: {macro_last or 'none'}"] + [f"Tonight: {s}" for s in steps],
                         f"{news_n:,} headline{'s' * (news_n != 1)}" if news_n else ""))

    # Ideas lab: the monthly top-ideas list, the data audit, and stocks whose figures went stale.
    raw = get_meta(con, "mix_ranking")
    ranking = json.loads(raw) if raw else {}
    age = _age(ranking.get("date"), today)
    status = BAD if age is None or age > 60 else WARN if age > 40 else OK
    details = [f"Top-ideas list made {ranking['date']} ({ranking.get('stocks', '?')} stocks ranked)" if ranking
               else "The top-ideas list hasn't been made yet"]
    audit = json.loads(get_meta(con, "data_audit") or "{}")
    if audit.get("problems") and (_age(audit.get("date"), today) or 0) <= 40:
        status = _worst(status, WARN)
        details.append(f"Data audit ({audit['date']}) found: {', '.join(audit['problems'])}")
    stale = health.stale_earnings(con, today)
    if stale:
        status = _worst(status, WARN)
        details.append(f"No new quarterly figures for {len(stale)} followed stock(s): "
                       + ", ".join(t for t, _ in stale[:6]))
    steps = _failed(failed_steps, "lab")
    status = _worst(status, WARN if steps else OK)
    out.append(_building("lab", "Ideas lab", "The monthly top-ideas list (~20 measures averaged)", status,
                         f"Top ideas from {ranking['date']}" if ranking else "No top-ideas list yet",
                         details + [f"Tonight: {s}" for s in steps]))

    # Army camp: the demo accounts (fake money), off until the Alpaca keys exist.
    accounts = [(n, json.loads(get_meta(con, f"{p}_status") or "{}")) for n, p in
                (("Demo account", "paper"), ("$100 account", "small"))]
    accounts = [(n, s) for n, s in accounts if s.get("date") and s.get("equity") is not None]
    steps = _failed(failed_steps, "camp")
    if not accounts and not steps:
        out.append(_building("camp", "Army camp", "The demo accounts (fake money on Alpaca)", OFF,
                             "No demo account yet (needs the Alpaca paper keys)", []))
    else:
        late = [n for n, s in accounts if (_busdays(s["date"], today) or 0) > 2]
        status = WARN if late or steps else OK
        out.append(_building(
            "camp", "Army camp", "The demo accounts (fake money on Alpaca)", status,
            f"{len(accounts)} account{'s' * (len(accounts) != 1)} following the portfolio",
            [f"{n}: ${s['equity']:,.0f}, checked {_ago(_age(s['date'], today))}" for n, s in accounts]
            + [f"Tonight: {s}" for s in steps],
            f"${sum(s['equity'] for _, s in accounts):,.0f}" if accounts else ""))

    # Arena: the day-trading demo account (its running totals live in daytrade.json).
    state = {}
    if daytrade_path and Path(daytrade_path).exists():
        try:
            state = json.loads(Path(daytrade_path).read_text())
        except (OSError, ValueError):
            state = {}
    days = sorted(state.get("days", {}))
    if not days:
        out.append(_building("arena", "Arena", "The day-trading demo account (fake money)", OFF,
                             "No day trading yet (needs the second Alpaca paper account)", []))
    else:
        total = sum(r.get("pnl", 0) for d in state["days"].values() for r in d.values())
        trades = sum(r.get("trades", 0) for d in state["days"].values() for r in d.values())
        late = _busdays(days[-1], today)
        status = WARN if late is not None and late > 2 else OK
        out.append(_building("arena", "Arena", "The day-trading demo account (fake money)", status,
                             f"Last trading day {days[-1]}",
                             [f"{len(days)} trading day{'s' * (len(days) != 1)}, {trades:,} trades",
                              f"Result so far: {'+' if total >= 0 else '-'}${abs(total):,.0f}"],
                             f"{'+' if total >= 0 else '-'}${abs(total):,.0f}"))

    # Gate: the nightly safety check of the portfolio (emergency exits, warnings).
    safety = json.loads(get_meta(con, "safety_last") or "{}")
    excluded = json.loads(get_meta(con, "paper_excluded") or "{}")
    recent = [t for t, r in excluded.items() if (_age(r.get("date"), today) or 99) <= 3]
    steps = _failed(failed_steps, "gate")
    age = _age(safety.get("date"), today)
    status = BAD if steps else OFF if age is None else WARN if recent or _busdays(safety["date"], today) > 2 else OK
    out.append(_building("gate", "Gate", "The nightly safety check: emergency exits and warnings", status,
                         f"Emergency exit: {', '.join(recent)}" if recent
                         else "The safety check hasn't run yet" if age is None else f"Safety check ran {_ago(age)}",
                         [f"Last safety check: {safety.get('date') or 'never'}"]
                         + ([f"Recent emergency exits: {', '.join(recent)}"] if recent else [])
                         + [f"Tonight: {s}" for s in steps]))

    # Vault: the trading journal in SQL Server (optional).
    run = _last_run(con, "journal")
    steps = _failed(failed_steps, "vault")
    if not run and not steps:
        status, headline = OFF, "Not set up (needs the Azure SQL secrets)"
    else:
        status = BAD if steps or (run and not run["ok"]) else OK
        headline = ("The last journal write failed" if status == BAD
                    else f"Journal written {_ago(_age(run['started'], today))}")
    out.append(_building("vault", "Vault", "The trading journal in SQL Server (for SSMS)", status, headline,
                         ([f"Last write: {run['started'].replace('T', ' ')}"] if run else [])
                         + ([f"Error: {run['error']}"] if run and run.get("error") else [])
                         + [f"Tonight: {s}" for s in steps]))

    # Clock tower: the logbook.
    week = (today - dt.timedelta(days=7)).isoformat()
    rows = []
    try:
        rows = con.execute("SELECT started, command, exit_code, error, seconds FROM run_log WHERE started >= ? "
                           "ORDER BY started DESC", (week,)).fetchall()
    except sqlite3.Error:
        pass
    bad = [r for r in rows if r[2] != 0 or r[3]]
    status = OFF if not rows else WARN if bad else OK
    details = [f"{len(rows)} command run{'s' * (len(rows) != 1)} in the last 7 days, {len(bad)} didn't finish"]
    details += [f"{r[0][:16].replace('T', ' ')} · {r[1]} {'✗ ' + (r[3] or 'exit ' + str(r[2]))[:120] if r in bad else '✓'}"
                for r in rows[:8]]
    out.append(_building("clock", "Clock tower", "The logbook: every command run and whether it finished", status,
                         "No runs logged this week" if not rows else
                         f"{len(rows)} run{'s' * (len(rows) != 1)} this week" + (f", {len(bad)} failed" if bad else ", all finished"),
                         details))

    # Town hall: the same checks as the ⚠️ Bot health message, plus the worst building.
    problems = health.check(con, today=today, failed_steps=failed_steps, job_status=job_status)
    worst = _worst(*(b["status"] for b in out))
    status = _worst(worst, WARN if problems else OK)
    out.insert(0, _building("hall", "Town hall", "The bot's overall health (the ⚠️ Bot health checks)", status,
                            "Everything is fine" if not problems else
                            f"{len(problems)} thing{'s' * (len(problems) != 1)} to look at",
                            problems or ["Every health check passed: fresh prices, data up to date, no SEC slow-down."]))
    return out


# --- Drawing ---------------------------------------------------------------------------------------------

def _flag(x: float, y: float, pole: float = 26) -> str:
    return (f'<line x1="{x}" y1="{y}" x2="{x}" y2="{y - pole}" stroke="#5b3a1e" stroke-width="2.5"/>'
            f'<path class="flag" d="M{x + 1} {y - pole} l18 5 l-18 6 z"/>')


SHADOW = '<ellipse cx="60" cy="98" rx="48" ry="9" fill="rgba(0,0,0,.22)"/>'

ART = {
    "hall": SHADOW + """
      <rect x="18" y="62" width="84" height="34" rx="3" fill="#b9a68a" stroke="#6d5c45" stroke-width="2"/>
      <rect x="26" y="40" width="68" height="26" fill="#e6d3b3" stroke="#6d5c45" stroke-width="2"/>
      <path d="M20 44 L60 16 L100 44 Z" fill="#c4473a" stroke="#7a261d" stroke-width="2"/>
      <path d="M34 66 L60 50 L86 66 Z" fill="#d5574a" stroke="#7a261d" stroke-width="2"/>
      <rect x="52" y="74" width="16" height="22" rx="8" fill="#5b3a1e"/>
      <rect x="30" y="72" width="12" height="10" fill="#7fc4e8" stroke="#6d5c45"/>
      <rect x="78" y="72" width="12" height="10" fill="#7fc4e8" stroke="#6d5c45"/>
      <circle cx="60" cy="34" r="5" fill="#f4c542" stroke="#8a5a00"/>""" + _flag(60, 18, 18),
    "mine": SHADOW + """
      <path d="M12 96 Q20 50 60 44 Q100 50 108 96 Z" fill="#8e8579" stroke="#57504a" stroke-width="2"/>
      <path d="M40 96 L40 70 Q60 56 80 70 L80 96 Z" fill="#2b2622"/>
      <path d="M36 96 L36 68 L84 68 L84 96" fill="none" stroke="#7a5230" stroke-width="5"/>
      <rect x="34" y="64" width="52" height="6" fill="#8a5d35"/>
      <rect x="62" y="82" width="26" height="12" rx="2" fill="#6b6b6b" stroke="#3d3d3d"/>
      <circle cx="68" cy="81" r="4" fill="#f4c542"/><circle cx="76" cy="79" r="4.5" fill="#ffd95a"/>
      <circle cx="83" cy="81" r="3.5" fill="#f4c542"/>
      <circle cx="67" cy="95" r="3" fill="#333"/><circle cx="83" cy="95" r="3" fill="#333"/>""" + _flag(96, 70),
    "library": SHADOW + """
      <rect x="16" y="88" width="88" height="8" fill="#cfc8bb" stroke="#7c7466"/>
      <rect x="20" y="82" width="80" height="7" fill="#e2dccf" stroke="#7c7466"/>
      <g fill="#f1ece2" stroke="#7c7466" stroke-width="1.5">
        <rect x="26" y="50" width="9" height="32"/><rect x="44" y="50" width="9" height="32"/>
        <rect x="67" y="50" width="9" height="32"/><rect x="85" y="50" width="9" height="32"/></g>
      <rect x="20" y="44" width="80" height="7" fill="#e2dccf" stroke="#7c7466"/>
      <path d="M18 44 L60 22 L102 44 Z" fill="#e9e2d4" stroke="#7c7466" stroke-width="2"/>
      <circle cx="60" cy="36" r="5" fill="#3d6fb6"/>""" + _flag(60, 23, 16),
    "tower": SHADOW + """
      <path d="M44 96 L50 40 L70 40 L76 96 Z" fill="#a0703f" stroke="#5b3a1e" stroke-width="2"/>
      <path d="M46 80 L74 60 M74 80 L46 60" stroke="#5b3a1e" stroke-width="2"/>
      <rect x="38" y="34" width="44" height="10" fill="#8a5d35" stroke="#5b3a1e" stroke-width="2"/>
      <rect x="42" y="22" width="36" height="12" fill="#c69a63" stroke="#5b3a1e" stroke-width="1.5"/>
      <path d="M36 24 L60 6 L84 24 Z" fill="#3b6ea5" stroke="#244a73" stroke-width="2"/>
      <circle cx="60" cy="28" r="3" fill="#2b2622"/>""" + _flag(84, 40, 22),
    "crawler": SHADOW + """
      <g stroke="#3d3d3d" stroke-width="3" fill="none" stroke-linecap="round" class="legs">
        <path d="M44 74 L24 66 L16 92"/><path d="M44 80 L22 82 L18 98"/><path d="M48 84 L36 96"/>
        <path d="M76 74 L96 66 L104 92"/><path d="M76 80 L98 82 L102 98"/><path d="M72 84 L84 96"/></g>
      <ellipse cx="60" cy="76" rx="22" ry="14" fill="#6d7a86" stroke="#39434c" stroke-width="2"/>
      <circle cx="52" cy="74" r="4" fill="#9be7ff"/><circle cx="68" cy="74" r="4" fill="#9be7ff"/>
      <line x1="60" y1="62" x2="60" y2="42" stroke="#39434c" stroke-width="2.5"/>
      <path d="M48 42 Q60 30 72 42" fill="#c9d3dc" stroke="#39434c" stroke-width="2"/>
      <g class="signal" fill="none" stroke="#2a78d6" stroke-width="2" stroke-linecap="round">
        <path d="M74 30 Q80 36 74 42"/><path d="M80 24 Q90 36 80 48"/></g>""" + _flag(96, 64, 20),
    "lab": SHADOW + """
      <rect x="22" y="56" width="76" height="40" rx="3" fill="#8f6fb8" stroke="#4f3a6e" stroke-width="2"/>
      <path d="M30 58 Q60 14 90 58 Z" fill="#b89ae0" stroke="#4f3a6e" stroke-width="2"/>
      <rect x="52" y="74" width="16" height="22" rx="7" fill="#3d2a55"/>
      <path d="M78 66 L78 76 L72 90 L92 90 L86 76 L86 66 Z" fill="#9ff0b0" stroke="#2e6b3a" stroke-width="1.5"/>
      <g class="bubbles" fill="#9ff0b0"><circle cx="80" cy="60" r="2.5"/><circle cx="85" cy="52" r="2"/></g>
      <circle cx="40" cy="70" r="5" fill="#ffe27a" stroke="#4f3a6e"/>""" + _flag(60, 30, 18),
    "camp": SHADOW + """
      <path d="M10 94 L32 52 L54 94 Z" fill="#c9a76b" stroke="#6d5530" stroke-width="2"/>
      <path d="M28 94 L32 72 L36 94 Z" fill="#4a3a22"/>
      <path d="M62 94 L86 46 L110 94 Z" fill="#b8955a" stroke="#6d5530" stroke-width="2"/>
      <path d="M81 94 L86 70 L91 94 Z" fill="#4a3a22"/>
      <g transform="translate(58 92)"><path d="M-8 4 L8 -2 M-8 -2 L8 4" stroke="#5b3a1e" stroke-width="3"/>
        <path class="fire" d="M0 -14 Q6 -6 3 0 L-3 0 Q-6 -6 0 -14 Z" fill="#ff8a2a"/></g>""" + _flag(86, 47, 18),
    "arena": SHADOW + """
      <ellipse cx="60" cy="72" rx="48" ry="24" fill="#d8b98a" stroke="#7a5f3a" stroke-width="2"/>
      <ellipse cx="60" cy="66" rx="36" ry="14" fill="#c7a26e" stroke="#7a5f3a" stroke-width="1.5"/>
      <ellipse cx="60" cy="68" rx="26" ry="9" fill="#e8d5a8"/>
      <g fill="#7a5f3a"><rect x="20" y="80" width="6" height="9" rx="3"/><rect x="34" y="85" width="6" height="9" rx="3"/>
        <rect x="50" y="87" width="6" height="9" rx="3"/><rect x="66" y="87" width="6" height="9" rx="3"/>
        <rect x="82" y="85" width="6" height="9" rx="3"/><rect x="95" y="80" width="6" height="9" rx="3"/></g>
      <path d="M52 70 L60 60 L68 70" stroke="#b42f2f" stroke-width="3" fill="none"/>
      <path d="M52 62 L60 72 L68 62" stroke="#2a78d6" stroke-width="3" fill="none"/>""" + _flag(100, 62, 22),
    "gate": SHADOW + """
      <rect x="14" y="54" width="22" height="42" fill="#9a958c" stroke="#5c5850" stroke-width="2"/>
      <rect x="84" y="54" width="22" height="42" fill="#9a958c" stroke="#5c5850" stroke-width="2"/>
      <g fill="#9a958c" stroke="#5c5850" stroke-width="1.5">
        <rect x="14" y="46" width="6" height="8"/><rect x="25" y="46" width="6" height="8"/>
        <rect x="84" y="46" width="6" height="8"/><rect x="96" y="46" width="6" height="8"/></g>
      <rect x="36" y="58" width="48" height="38" fill="#7d786f" stroke="#5c5850" stroke-width="2"/>
      <path d="M42 96 L42 72 Q60 58 78 72 L78 96 Z" fill="#5b3a1e"/>
      <path d="M48 96 V72 M54 96 V67 M60 96 V65 M66 96 V67 M72 96 V72" stroke="#3a2412" stroke-width="2"/>
      <path d="M60 40 L74 46 Q74 62 60 68 Q46 62 46 46 Z" fill="#3b6ea5" stroke="#f4c542" stroke-width="2.5"/>""" + _flag(25, 46, 18),
    "vault": SHADOW + """
      <rect x="22" y="58" width="76" height="38" rx="4" fill="#a8742f" stroke="#5b3a1e" stroke-width="2"/>
      <path d="M22 62 Q22 36 60 36 Q98 36 98 62 Z" fill="#c48a3a" stroke="#5b3a1e" stroke-width="2"/>
      <rect x="22" y="56" width="76" height="8" fill="#f4c542" stroke="#8a5a00"/>
      <rect x="54" y="60" width="12" height="16" rx="2" fill="#f4c542" stroke="#8a5a00"/>
      <circle cx="60" cy="67" r="2.5" fill="#5b3a1e"/>
      <g stroke="#f4c542" stroke-width="3"><line x1="34" y1="38" x2="34" y2="96"/><line x1="86" y1="38" x2="86" y2="96"/></g>
      <g class="sparkle" fill="#fff6c2"><path d="M44 30 l2 5 l5 2 l-5 2 l-2 5 l-2 -5 l-5 -2 l5 -2 z"/></g>""" + _flag(98, 58, 22),
    "clock": SHADOW + """
      <rect x="42" y="34" width="36" height="62" fill="#c9b79a" stroke="#6d5c45" stroke-width="2"/>
      <path d="M38 36 L60 8 L82 36 Z" fill="#4c7a4a" stroke="#2c4a2b" stroke-width="2"/>
      <circle cx="60" cy="52" r="12" fill="#fffaf0" stroke="#6d5c45" stroke-width="2"/>
      <line x1="60" y1="52" x2="60" y2="44" stroke="#2b2622" stroke-width="2"/>
      <line class="hand" x1="60" y1="52" x2="66" y2="55" stroke="#b42f2f" stroke-width="2"/>
      <rect x="54" y="78" width="12" height="18" rx="6" fill="#5b3a1e"/>
      <rect x="18" y="80" width="20" height="16" rx="2" fill="#efe6d2" stroke="#6d5c45"/>
      <path d="M22 85 h12 M22 89 h12 M22 93 h8" stroke="#8d8c86"/>""" + _flag(60, 10, 0),
}

DECOR = """<svg viewBox="0 0 120 110" aria-hidden="true">
  <ellipse cx="44" cy="96" rx="22" ry="6" fill="rgba(0,0,0,.18)"/><ellipse cx="86" cy="100" rx="16" ry="5" fill="rgba(0,0,0,.18)"/>
  <rect x="41" y="70" width="6" height="26" fill="#6b4423"/><circle cx="44" cy="58" r="20" fill="var(--tree)"/>
  <circle cx="34" cy="64" r="12" fill="var(--tree-2)"/><rect x="84" y="82" width="5" height="18" fill="#6b4423"/>
  <circle cx="86" cy="76" r="13" fill="var(--tree-2)"/></svg>"""

POND = """<svg viewBox="0 0 120 110" aria-hidden="true">
  <ellipse cx="60" cy="74" rx="46" ry="22" fill="var(--water)" stroke="var(--water-edge)" stroke-width="4"/>
  <ellipse cx="46" cy="70" rx="10" ry="3" fill="rgba(255,255,255,.35)"/>
  <circle cx="22" cy="58" r="6" fill="var(--tree-2)"/><circle cx="100" cy="88" r="7" fill="var(--tree)"/></svg>"""

SMOKE = '<g class="smoke"><circle cx="30" cy="40" r="7"/><circle cx="38" cy="30" r="9"/><circle cx="30" cy="18" r="11"/></g>'
SCAFFOLD = ('<g class="scaffold" stroke="#b08850" stroke-width="2.5"><path d="M14 98 V30 M106 98 V30 M14 40 H106 '
            'M14 70 H106 M14 40 L106 70 M106 40 L14 70"/></g>')

CSS = """
:root {
  color-scheme: light;
  --sky: #bfe3ff; --grass: #7cc35a; --grass-2: #6bb24b; --path: #e3c98f; --wall: #8f8a80; --wall-edge: #5c5850;
  --card: #fffdf6; --ink: #2b2118; --ink-2: #6b5a48; --line: rgba(43,33,24,.14);
  --tree: #3f8f3a; --tree-2: #57a84a; --water: #6cc3ee; --water-edge: #3d8fbf;
  --ok: #2fa84f; --warn: #f2b632; --bad: #e0453a; --off: #b8b2a6; --gold: #f4c542;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --sky: #0f1830; --grass: #2f5a2a; --grass-2: #284f24; --path: #6e6040; --wall: #5f5b55; --wall-edge: #3a3733;
    --card: #221d17; --ink: #f6eedf; --ink-2: #c5b69f; --line: rgba(255,255,255,.12);
    --tree: #23552a; --tree-2: #2e6a33; --water: #24557a; --water-edge: #173a56;
    --ok: #3ccf63; --warn: #f5c451; --bad: #ff6a5c; --off: #8a857b;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--sky); color: var(--ink);
       font: 15px/1.45 "Trebuchet MS", -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 20px 16px 48px; }
header { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; margin-bottom: 14px; }
h1 { margin: 0; font-size: 28px; letter-spacing: .5px; text-shadow: 0 2px 0 rgba(0,0,0,.12); }
.sub { color: var(--ink-2); font-size: 13px; }
.tally { display: flex; gap: 8px; flex-wrap: wrap; }
.pill { background: var(--card); border: 2px solid var(--line); border-radius: 999px; padding: 4px 12px;
        font-weight: 700; font-size: 14px; display: inline-flex; align-items: center; gap: 6px; }
.dot { width: 11px; height: 11px; border-radius: 50%; display: inline-block; border: 1.5px solid rgba(0,0,0,.25); }
.dot.ok { background: var(--ok); } .dot.warn { background: var(--warn); } .dot.bad { background: var(--bad); }
.dot.off { background: var(--off); }
.village { position: relative; padding: 22px; border-radius: 22px;
  background: repeating-linear-gradient(45deg, var(--grass) 0 26px, var(--grass-2) 26px 52px);
  border: 12px solid var(--wall); outline: 3px solid var(--wall-edge); box-shadow: inset 0 0 0 3px var(--wall-edge), 0 10px 0 rgba(0,0,0,.15); }
.village.under-attack { border-color: #8a5a55; }
.grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
.plot { position: relative; min-width: 0; background: rgba(255,255,255,.08); border-radius: 16px; aspect-ratio: 1 / .92;
        display: flex; flex-direction: column; align-items: center; justify-content: flex-end; }
.plot.path { background: var(--path); opacity: .55; }
button.plot { border: 0; padding: 0 0 6px; cursor: pointer; font: inherit; color: inherit;
              transition: transform .12s ease; }
button.plot:hover, button.plot:focus-visible { transform: translateY(-4px) scale(1.03); outline: none; }
button.plot:focus-visible { box-shadow: 0 0 0 3px var(--gold); }
.plot.hall { grid-column: span 2; grid-row: span 2; aspect-ratio: auto; }
.plot svg { width: 100%; height: auto; display: block; overflow: visible; }
.name { background: var(--card); border: 2px solid var(--line); border-radius: 10px; padding: 1px 8px;
        font-weight: 800; font-size: 13px; white-space: nowrap; display: inline-flex; align-items: center; gap: 5px;
        max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
.hall .name { font-size: 16px; }
.stat { font-size: 11px; color: var(--ink-2); background: var(--card); border-radius: 6px; padding: 0 6px; margin-top: 3px; }
.flag { fill: var(--ok); }
.plot.warn .flag { fill: var(--warn); } .plot.bad .flag { fill: var(--bad); } .plot.off .flag { fill: var(--off); }
.plot.off svg .art { filter: grayscale(1) opacity(.45); }
.badge { position: absolute; top: 4px; right: 6px; width: 26px; height: 26px; border-radius: 50%;
         display: grid; place-items: center; font-weight: 900; color: #2b2118; border: 2px solid rgba(0,0,0,.25); }
.plot.warn .badge { background: var(--warn); } .plot.bad .badge { background: var(--bad); color: #fff; }
.smoke circle { fill: rgba(70,70,70,.55); transform-box: fill-box; transform-origin: center; }
.scaffold { opacity: .9; }
@media (prefers-reduced-motion: no-preference) {
  .flag { transform-box: fill-box; transform-origin: left center; animation: wave 1.6s ease-in-out infinite; }
  .smoke circle { animation: puff 2.4s ease-out infinite; }
  .smoke circle:nth-child(2) { animation-delay: .8s; } .smoke circle:nth-child(3) { animation-delay: 1.6s; }
  .plot.warn .badge { animation: bob 1.2s ease-in-out infinite; }
  .plot.ok .signal, .plot.ok .bubbles, .plot.ok .sparkle, .plot.ok .fire { animation: blink 1.4s ease-in-out infinite; }
  .plot.ok .legs { animation: scuttle .5s steps(2) infinite; }
  .plot.ok .hand { transform-box: view-box; transform-origin: 60px 52px; animation: spin 6s linear infinite; }
}
@keyframes wave { 50% { transform: scaleX(.82) skewY(-6deg); } }
@keyframes puff { 0% { opacity: 0; transform: translateY(10px) scale(.5); } 30% { opacity: 1; }
                  100% { opacity: 0; transform: translateY(-16px) scale(1.4); } }
@keyframes bob { 50% { transform: translateY(-4px); } }
@keyframes blink { 50% { opacity: .35; } }
@keyframes scuttle { 50% { transform: translateX(1px); } }
@keyframes spin { to { transform: rotate(360deg); } }
.legend { display: flex; flex-wrap: wrap; gap: 14px; justify-content: center; margin: 14px 0 0; font-size: 13px; color: var(--ink); }
.legend span { display: inline-flex; align-items: center; gap: 6px; background: var(--card); padding: 3px 10px; border-radius: 999px; }
dialog { border: 4px solid var(--wall-edge); border-radius: 18px; background: var(--card); color: var(--ink);
         max-width: min(520px, calc(100vw - 32px)); width: 100%; padding: 0; box-shadow: 0 12px 0 rgba(0,0,0,.25); }
dialog::backdrop { background: rgba(10,20,40,.45); }
.dlg { padding: 18px 20px 20px; }
.dlg h2 { margin: 0 0 2px; font-size: 22px; display: flex; align-items: center; gap: 8px; }
.dlg .role { color: var(--ink-2); font-size: 13px; margin-bottom: 10px; }
.dlg .head { font-weight: 700; margin: 6px 0 8px; }
.dlg ul { margin: 0; padding-left: 18px; } .dlg li { margin: 3px 0; overflow-wrap: anywhere; }
.dlg .close { float: right; border: 2px solid var(--line); background: var(--sky); color: var(--ink); border-radius: 10px;
              font: inherit; font-weight: 800; padding: 2px 10px; cursor: pointer; }
.list { margin-top: 26px; background: var(--card); border-radius: 16px; border: 2px solid var(--line); padding: 6px 16px 10px; }
.list h2 { font-size: 17px; margin: 10px 0 4px; }
.row { display: grid; grid-template-columns: 18px 130px 1fr; gap: 8px; align-items: baseline; padding: 6px 0;
       border-top: 1px solid var(--line); font-size: 14px; }
.row:first-of-type { border-top: 0; }
.row b { white-space: nowrap; }
footer { margin-top: 14px; color: var(--ink-2); font-size: 12px; text-align: center; }
@media (max-width: 640px) {
  .village { padding: 12px; border-width: 8px; }
  .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; grid-auto-flow: dense; }
  .plot.deco { display: none; }
  .row { grid-template-columns: 18px 1fr; } .row span:last-child { grid-column: 2; }
}
"""

JS = """
const data = JSON.parse(document.getElementById('base-data').textContent);
const dlg = document.getElementById('dlg');
const esc = s => s.replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
document.querySelectorAll('button.plot').forEach(b => b.addEventListener('click', () => {
  const d = data[b.dataset.key];
  dlg.querySelector('.dlg').innerHTML =
    `<button class="close" aria-label="Close">✕</button>
     <h2><span class="dot ${d.status}"></span>${esc(d.name)}</h2>
     <div class="role">${esc(d.role)} · <b>${esc(d.label)}</b></div>
     <div class="head">${esc(d.headline)}</div>
     <ul>${d.details.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`;
  dlg.querySelector('.close').onclick = () => dlg.close();
  dlg.showModal();
}));
dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
"""


def _plot(b: dict) -> str:
    extra = SMOKE if b["status"] == BAD else SCAFFOLD if b["status"] == OFF else ""
    badge = '<span class="badge" aria-hidden="true">!</span>' if b["status"] in (WARN, BAD) else ""
    stat = f'<span class="stat">{html.escape(b["stat"])}</span>' if b.get("stat") else ""
    return (f'<button class="plot {b["key"]} {b["status"]}" data-key="{b["key"]}" '
            f'aria-label="{html.escape(b["name"])}: {LABEL[b["status"]]}. {html.escape(b["headline"])}">{badge}'
            f'<svg viewBox="0 0 120 110" aria-hidden="true"><g class="art">{ART[b["key"]]}</g>{extra}</svg>'
            f'<span class="name"><span class="dot {b["status"]}"></span>{html.escape(b["name"])}</span>{stat}</button>')


# Where everything stands on the 4-wide grid (the town hall takes 2x2 in the middle).
LAYOUT = ["mine", "library", "tower", "crawler",
          "lab", "hall", "camp",
          "arena", "gate",
          "vault", "~tree", "~pond", "clock"]


def build_base(buildings: list[dict], made: dt.datetime | None = None, run_url: str | None = None) -> str:
    """The village page (one self-contained HTML file)."""
    made = made or dt.datetime.now()
    by_key = {b["key"]: b for b in buildings}
    cells = []
    for key in LAYOUT:
        if key == "~tree":
            cells.append(f'<div class="plot deco">{DECOR}</div>')
        elif key == "~pond":
            cells.append(f'<div class="plot deco">{POND}</div>')
        elif key in by_key:
            cells.append(_plot(by_key[key]))
    counts = {s: sum(b["status"] == s for b in buildings[1:]) for s in (OK, WARN, BAD, OFF)}
    tally = "".join(f'<span class="pill"><span class="dot {s}"></span>{counts[s]} {LABEL[s].lower()}</span>'
                    for s in (OK, WARN, BAD, OFF) if counts[s])
    rows = "".join(f'<div class="row"><span class="dot {b["status"]}"></span><b>{html.escape(b["name"])}</b>'
                   f'<span>{html.escape(b["headline"])}</span></div>' for b in buildings)
    data = {b["key"]: {**b, "label": LABEL[b["status"]]} for b in buildings}
    hall = by_key.get("hall", {"status": OK, "headline": ""})
    link = f' · <a href="{html.escape(run_url)}">tonight\'s run</a>' if run_url else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trend Bot Village</title>
<style>{CSS}</style></head>
<body><main>
<header>
  <div><h1>🏰 Trend Bot Village</h1>
  <div class="sub">Every part of the bot is a building. Tap one to see how it's doing.</div></div>
  <div class="tally">{tally}</div>
</header>
<section class="village{' under-attack' if hall['status'] == BAD else ''}" aria-label="The bot's village">
  <div class="grid">{''.join(cells)}</div>
</section>
<div class="legend">
  <span><span class="dot ok"></span>Working</span><span><span class="dot warn"></span>Needs a look</span>
  <span><span class="dot bad"></span>Broken (smoke)</span><span><span class="dot off"></span>Not set up (scaffolding)</span>
</div>
<section class="list"><h2>{html.escape(hall['headline'])}</h2>{rows}</section>
<footer>Made {made:%Y-%m-%d %H:%M}{link}. It only reads the bot's database; nothing here changes anything.</footer>
<dialog id="dlg"><div class="dlg"></div></dialog>
</main>
<script id="base-data" type="application/json">{json.dumps(data).replace("</", "<\\/")}</script>
<script>{JS}</script>
</body></html>"""
