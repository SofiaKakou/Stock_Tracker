"""The bot's office: every part of the bot is a little pixel-art worker at a desk, so you can see at a glance
what works (in the style of Pixel Agents).

A self-contained web page (like the report), read from the same database the bot uses every night.
What a worker is doing shows how that part of the bot is (from the nightly health check):

  typing at the desk, green screen            working
  "!" bubble, yellow screen                   needs a look
  standing up next to a red, smoking screen   broken
  empty desk with a sticky note               not set up yet

  Boss                   the bot's overall health (the same checks as the ⚠️ Bot health message);
                         walks over to whoever has a problem
  Price collector        prices from Yahoo Finance
  SEC reader             company financials, filings and insider trades from the SEC
  Short-selling watcher  short selling data from FINRA
  News crawler           news headlines (Alpaca) and the economy (FRED)
  Researcher             the monthly top-ideas list and the data audit
  Demo trader            the demo accounts (fake money)
  Day trader             the day-trading demo account
  Safety guard           the nightly safety check (emergency exits)
  Journal clerk          the trading journal in SQL Server
  Logbook keeper         the logbook: every command run and whether it finished

Live, like Pixel Agents: the page asks GitHub every minute or two what the bot is running right now (the
public Actions API: no key, no server, works on the published page and from a downloaded file). The worker
whose step is running types fast (in the nightly run, step by step: 'Collect news' is the news crawler);
a worker whose desk was empty walks in through the door when their job starts; a run that just failed
puts its worker up next to a smoking screen; a run waiting to start shows a "…" bubble; the boss walks
round to whoever is busy or has a problem. A strip above the office says what's running, and the recent
runs are listed below it. Between runs the workers rest, and those who are fine take the odd coffee break.
GitHub allows ~60 such requests an hour per network, so the page paces itself and pauses while hidden.

Click a worker for the details. Nothing here changes anything; it only reads.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import numpy as np

from trend_bot import health
from trend_bot.db import get_meta

OK, WARN, BAD, OFF = "ok", "warn", "bad", "off"
RANK = {OK: 0, OFF: 0, WARN: 1, BAD: 2}
LABEL = {OK: "Working", WARN: "Needs a look", BAD: "Broken", OFF: "Not set up"}

# Words in reports/problems.txt (steps of tonight's run that didn't finish) -> the worker they belong to.
STEP_WORDS = {"prices": ("data update", "price retry", "saved database"),
              "demo": ("demo account", "crypto account", "demo accounts"),
              "news": ("collecting news",), "ideas": ("ranking snapshot", "study", "audit", "ideas"),
              "safety": ("safety",), "journal": ("trading journal",)}


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


def _worker(key: str, name: str, role: str, status: str, headline: str, details: list[str],
              stat: str = "") -> dict:
    return {"key": key, "name": name, "role": role, "status": status, "headline": headline,
            "details": details, "stat": stat}


def _failed(failed_steps: list[str], key: str) -> list[str]:
    return [s for s in failed_steps if any(w in s.lower() for w in STEP_WORDS.get(key, ()))]


def survey(con: sqlite3.Connection, today: dt.date | None = None, failed_steps: list[str] | None = None,
           job_status: str | None = None, daytrade_path: str | Path | None = "daytrade.json") -> list[dict]:
    """Every worker with its status, a one-line headline and the details (the boss first)."""
    today = today or dt.date.today()
    failed_steps = failed_steps or []
    out = []

    # Price collector: Yahoo prices.
    stocks, last = _one(con, "SELECT COUNT(*), MAX(last_date) FROM tickers") or (0, None)
    behind = _busdays(last, today)
    status = BAD if behind is None or behind > 5 else WARN if behind > 2 else OK
    steps = _failed(failed_steps, "prices")
    status = _worst(status, WARN if steps else OK)
    out.append(_worker(
        "prices", "Price collector", "Prices from Yahoo Finance, every weekday evening", status,
        "No prices yet" if last is None else f"Prices up to {last[:10]}"
        + ("" if not behind or behind <= 1 else f" ({behind} trading days behind)"),
        [f"{stocks:,} stocks in the database", f"Last price update: {_ago(_age(get_meta(con, 'prices_updated'), today))}"]
        + [f"Tonight: {s}" for s in steps], f"{stocks:,} stocks"))

    # SEC reader: financials, the bulk file, insider trades, and whether the SEC asked us to slow down.
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
    out.append(_worker("sec", "SEC reader", "Financials, filings and insider trades from the SEC", status,
                       "Paused: the SEC asked us to slow down" if paused
                       else "Financials not downloaded yet" if facts is None else f"Financials updated {_ago(facts)}",
                       details, f"filed to {newest[:10]}" if newest else ""))

    # Short-selling watcher: FINRA short selling.
    age = _age(get_meta(con, "short_volume_last"), today)
    status = BAD if age is None or age > 14 else WARN if age > 7 else OK
    out.append(_worker("finra", "Short-selling watcher", "Short selling data from FINRA", status,
                       "No short selling data yet" if age is None else f"Short selling data from {_ago(age)}",
                       [f"Latest short selling day: {get_meta(con, 'short_volume_last') or 'never'}"]))

    # News crawler: Alpaca headlines and FRED economy (optional: off until the keys exist).
    news_n, news_last = _one(con, "SELECT COUNT(*), MAX(created) FROM news") or (0, None)
    macro_last = (_one(con, "SELECT MAX(date) FROM macro") or [None])[0]
    steps = _failed(failed_steps, "news")
    if not news_n and not macro_last and not steps:
        status = OFF
        headline = "Not collecting yet (needs the Alpaca and FRED keys)"
    else:
        news_age = _busdays(news_last, today)
        status = WARN if steps or news_age is None or news_age > 3 else OK
        headline = f"{news_n:,} headline{'s' * (news_n != 1)}, newest {_ago(_age(news_last, today))}"
    out.append(_worker("news", "News crawler", "News headlines (Alpaca) and the economy (FRED)", status, headline,
                       [f"Headlines collected: {news_n:,}", f"Newest headline: {news_last[:16] if news_last else 'none'}",
                        f"Newest economy figure: {macro_last or 'none'}"] + [f"Tonight: {s}" for s in steps],
                       f"{news_n:,} headline{'s' * (news_n != 1)}" if news_n else ""))

    # Researcher: the monthly top-ideas list, the data audit, and stocks whose figures went stale.
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
    steps = _failed(failed_steps, "ideas")
    status = _worst(status, WARN if steps else OK)
    out.append(_worker("ideas", "Researcher", "The monthly top-ideas list (~20 measures averaged)", status,
                       f"Top ideas from {ranking['date']}" if ranking else "No top-ideas list yet",
                       details + [f"Tonight: {s}" for s in steps]))

    # Demo trader: the demo account following the portfolio (fake money), off until the Alpaca keys exist.
    # (The $100 account trades crypto now, every 15 minutes: crypto.yml, shown live at the day trader's desk.)
    accounts = [(n, json.loads(get_meta(con, f"{p}_status") or "{}")) for n, p in (("Demo account", "paper"),)]
    accounts = [(n, s) for n, s in accounts if s.get("date") and s.get("equity") is not None]
    steps = _failed(failed_steps, "demo")
    if not accounts and not steps:
        out.append(_worker("demo", "Demo trader", "The demo accounts (fake money on Alpaca)", OFF,
                           "No demo account yet (needs the Alpaca paper keys)", []))
    else:
        late = [n for n, s in accounts if (_busdays(s["date"], today) or 0) > 2]
        status = WARN if late or steps else OK
        out.append(_worker(
            "demo", "Demo trader", "The demo accounts (fake money on Alpaca)", status,
            f"{len(accounts)} account{'s' * (len(accounts) != 1)} following the portfolio",
            [f"{n}: ${s['equity']:,.0f}, checked {_ago(_age(s['date'], today))}" for n, s in accounts]
            + [f"Tonight: {s}" for s in steps],
            f"${sum(s['equity'] for _, s in accounts):,.0f}" if accounts else ""))

    # Day trader: the day-trading demo account (its running totals live in daytrade.json).
    state = {}
    if daytrade_path and Path(daytrade_path).exists():
        try:
            state = json.loads(Path(daytrade_path).read_text())
        except (OSError, ValueError):
            state = {}
    days = sorted(state.get("days", {}))
    if not days:
        out.append(_worker("daytrade", "Day trader", "The day-trading demo account (fake money)", OFF,
                           "No day trading yet (needs the second Alpaca paper account)", []))
    else:
        total = sum(r.get("pnl", 0) for d in state["days"].values() for r in d.values())
        trades = sum(r.get("trades", 0) for d in state["days"].values() for r in d.values())
        late = _busdays(days[-1], today)
        status = WARN if late is not None and late > 2 else OK
        out.append(_worker("daytrade", "Day trader", "The day-trading demo account (fake money)", status,
                           f"Last trading day {days[-1]}",
                           [f"{len(days)} trading day{'s' * (len(days) != 1)}, {trades:,} trades",
                            f"Result so far: {'+' if total >= 0 else '-'}${abs(total):,.0f}"],
                           f"{'+' if total >= 0 else '-'}${abs(total):,.0f}"))

    # Safety guard: the nightly safety check of the portfolio (emergency exits, warnings).
    safety = json.loads(get_meta(con, "safety_last") or "{}")
    excluded = json.loads(get_meta(con, "paper_excluded") or "{}")
    recent = [t for t, r in excluded.items() if (_age(r.get("date"), today) or 99) <= 3]
    steps = _failed(failed_steps, "safety")
    age = _age(safety.get("date"), today)
    status = BAD if steps else OFF if age is None else WARN if recent or _busdays(safety["date"], today) > 2 else OK
    out.append(_worker("safety", "Safety guard", "The nightly safety check: emergency exits and warnings", status,
                       f"Emergency exit: {', '.join(recent)}" if recent
                       else "The safety check hasn't run yet" if age is None else f"Safety check ran {_ago(age)}",
                       [f"Last safety check: {safety.get('date') or 'never'}"]
                       + ([f"Recent emergency exits: {', '.join(recent)}"] if recent else [])
                       + [f"Tonight: {s}" for s in steps]))

    # Journal clerk: the trading journal in SQL Server (optional).
    run = _last_run(con, "journal")
    steps = _failed(failed_steps, "journal")
    if not run and not steps:
        status, headline = OFF, "Not set up (needs the Azure SQL secrets)"
    else:
        status = BAD if steps or (run and not run["ok"]) else OK
        headline = ("The last journal write failed" if status == BAD
                    else f"Journal written {_ago(_age(run['started'], today))}")
    out.append(_worker("journal", "Journal clerk", "The trading journal in SQL Server (for SSMS)", status, headline,
                       ([f"Last write: {run['started'].replace('T', ' ')}"] if run else [])
                       + ([f"Error: {run['error']}"] if run and run.get("error") else [])
                       + [f"Tonight: {s}" for s in steps]))

    # Logbook keeper: the logbook.
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
    out.append(_worker("logbook", "Logbook keeper", "The logbook: every command run and whether it finished", status,
                       "No runs logged this week" if not rows else
                       f"{len(rows)} run{'s' * (len(rows) != 1)} this week" + (f", {len(bad)} failed" if bad else ", all finished"),
                       details))

    # The boss: the same checks as the ⚠️ Bot health message, plus the worst desk.
    problems = health.check(con, today=today, failed_steps=failed_steps, job_status=job_status)
    worst = _worst(*(b["status"] for b in out))
    status = _worst(worst, WARN if problems else OK)
    out.insert(0, _worker("boss", "Boss", "The bot's overall health (the ⚠️ Bot health checks)", status,
                          "Everything is fine" if not problems else
                          f"{len(problems)} thing{'s' * (len(problems) != 1)} to look at",
                          problems or ["Every health check passed: fresh prices, data up to date, no SEC slow-down."]))
    return out




# --- Drawing ---------------------------------------------------------------------------------------------
# The office is drawn pixel by pixel on a small canvas (352 x 240) and scaled up without smoothing.
# Desks: the boss at the top, then two rows of five. Nameplates are the short names below.

DESKS = ["prices", "sec", "finra", "news", "ideas", "demo", "daytrade", "safety", "journal", "logbook"]
PLATE = {"boss": "BOSS", "prices": "PRICES", "sec": "SEC", "finra": "FINRA", "news": "NEWS", "ideas": "IDEAS",
         "demo": "DEMO", "daytrade": "DAY TRADE", "safety": "SAFETY", "journal": "JOURNAL", "logbook": "LOGBOOK"}
# Live: which worker a GitHub Actions workflow belongs to ("steps": the nightly run, read step by step).
LIVE_WORKFLOWS = {"nightly.yml": "steps", "daytrade.yml": "daytrade", "daytrade-safety.yml": "safety", "crypto.yml": "daytrade",
                  "demo.yml": "demo", "demo-morning.yml": "demo", "research.yml": "ideas", "research-parallel.yml": "ideas",
                  "why.yml": "ideas", "ai.yml": "ideas", "journal.yml": "journal", "market-mirror.yml": "journal",
                  "import-db.yml": "logbook", "tests.yml": "logbook", "office.yml": "logbook"}
# Steps of the nightly run -> the workers doing them (the first match wins; anything else: the logbook keeper).
LIVE_STEPS = [("restore", ["logbook"]), ("save database", ["logbook"]), ("backup", ["logbook"]), ("keep the report", ["logbook"]),
              ("office", ["logbook"]), ("update data", ["prices", "sec", "finra"]), ("retry prices", ["prices"]),
              ("sec", ["sec"]), ("monthly honesty", ["ideas"]), ("discord alerts", ["boss"]), ("portfolio safety", ["safety"]),
              ("account", ["demo"]), ("collect news", ["news"]), ("trading journal", ["journal"]), ("report", ["boss"]),
              ("bot health", ["boss"]), ("make it the home page", ["logbook"]), ("publish", ["logbook"])]
LIVE_WAIT_STEPS = ["retry prices later"]   # a step that mostly waits (an hour's pause before trying again)


def step_workers(name: str) -> list[str]:
    """The workers doing a step of the nightly run (the page does the same in JavaScript)."""
    n = name.lower()
    return next((keys for words, keys in LIVE_STEPS if words in n), ["logbook"])


def repo_name() -> str | None:
    """owner/name of the GitHub repository (GitHub provides it; else from the local checkout's remote)."""
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    try:
        url = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent), "config", "--get", "remote.origin.url"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


# Hair, skin, shirt, pants for each worker.
LOOKS = {"boss": ("#3b2a1a", "#f1c7a0", "#e8e8ef", "#2d3446"), "prices": ("#e0a030", "#f5d0b0", "#3f7fd0", "#33415c"),
         "sec": ("#2b1d14", "#c98e62", "#8a5cc2", "#2f2f3f"), "finra": ("#b03a2e", "#f1c7a0", "#2f9e6a", "#3a3a4a"),
         "news": ("#111111", "#8d5a3b", "#e2683c", "#2d3446"), "ideas": ("#f2e2b0", "#f5d0b0", "#c94f8a", "#3c3c55"),
         "demo": ("#5a3a22", "#e8b48a", "#2a9fb0", "#2b3145"), "daytrade": ("#2a2a2a", "#a86f4c", "#d6b02a", "#3a2f2f"),
         "safety": ("#7a7a7a", "#f1c7a0", "#4a5d3a", "#2b2b2b"), "journal": ("#8a4a1e", "#f5d0b0", "#7c8a9a", "#3a3a4a"),
         "logbook": ("#d9d9d9", "#c98e62", "#b0473a", "#2f2f3f")}

CSS = """
:root {
  color-scheme: light;
  --bg: #e9e4d8; --card: #fffdf7; --ink: #241c14; --ink-2: #6b5a48; --line: rgba(36,28,20,.15);
  --frame: #3a2f28; --ok: #2fa84f; --warn: #e7a91d; --bad: #e0453a; --off: #a8a294; --focus: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --bg: #14121a; --card: #211d26; --ink: #f3ecdf; --ink-2: #b9ad9a; --line: rgba(255,255,255,.12);
    --frame: #0a0910; --ok: #3ccf63; --warn: #f5c451; --bad: #ff6a5c; --off: #8a857b; --focus: #6aa8ff;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
       font: 15px/1.45 ui-monospace, "Cascadia Mono", "SF Mono", Menlo, Consolas, monospace; }
main { max-width: 1080px; margin: 0 auto; padding: 20px 16px 48px; }
header { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; justify-content: space-between; margin-bottom: 14px; }
h1 { margin: 0; font-size: 24px; letter-spacing: 1px; text-transform: uppercase; }
.sub { color: var(--ink-2); font-size: 13px; }
.tally { display: flex; gap: 8px; flex-wrap: wrap; }
.pill { background: var(--card); border: 2px solid var(--line); padding: 3px 10px; font-weight: 700; font-size: 13px;
        display: inline-flex; align-items: center; gap: 6px; }
.dot { width: 10px; height: 10px; display: inline-block; flex: none; }
.dot.ok { background: var(--ok); } .dot.warn { background: var(--warn); } .dot.bad { background: var(--bad); }
.dot.off { background: var(--off); }
.screen { background: var(--frame); padding: 8px; box-shadow: 0 6px 0 rgba(0,0,0,.2); }
canvas { display: block; width: 100%; height: auto; image-rendering: pixelated; image-rendering: crisp-edges; cursor: pointer; }
canvas:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
.legend { display: flex; flex-wrap: wrap; gap: 8px 16px; margin: 12px 0 0; font-size: 13px; color: var(--ink-2); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.list { margin-top: 22px; background: var(--card); border: 2px solid var(--line); padding: 6px 14px 10px; }
.list h2 { font-size: 16px; margin: 10px 0 4px; }
button.row { all: unset; box-sizing: border-box; width: 100%; display: grid; grid-template-columns: 14px 190px 1fr;
             gap: 8px; align-items: baseline; padding: 7px 4px; border-top: 1px solid var(--line); font-size: 14px; cursor: pointer; }
button.row:first-of-type { border-top: 0; }
button.row:hover { background: var(--line); }
button.row:focus-visible { outline: 2px solid var(--focus); }
button.row b { white-space: nowrap; }
dialog { border: 4px solid var(--frame); background: var(--card); color: var(--ink); padding: 0;
         max-width: min(540px, calc(100vw - 32px)); width: 100%; box-shadow: 0 8px 0 rgba(0,0,0,.25); }
dialog::backdrop { background: rgba(10,10,20,.5); }
.dlg { padding: 16px 18px 18px; }
.dlg h2 { margin: 0 0 2px; font-size: 19px; display: flex; align-items: center; gap: 8px; }
.dlg .role { color: var(--ink-2); font-size: 13px; margin-bottom: 8px; }
.dlg .head { font-weight: 700; margin: 6px 0 8px; }
.dlg ul { margin: 0; padding-left: 18px; } .dlg li { margin: 3px 0; overflow-wrap: anywhere; }
.dlg .close { float: right; border: 2px solid var(--line); background: var(--bg); color: var(--ink); font: inherit;
              font-weight: 800; padding: 1px 9px; cursor: pointer; }
.live { background: var(--card); border: 2px solid var(--line); border-bottom: 0; padding: 6px 10px; font-size: 13px;
        min-height: 34px; overflow-wrap: anywhere; }
.live a { color: inherit; } .live b { margin-right: 6px; }
.beat { display: inline-block; width: 9px; height: 9px; background: var(--off); margin-right: 6px; }
.beat.on { background: var(--ok); }
@media (prefers-reduced-motion: no-preference) { .beat.on { animation: beat 1s steps(2) infinite; } }
@keyframes beat { 50% { opacity: .25; } }
.runs { margin-top: 18px; } .runs h2 { font-size: 15px; margin: 0 0 6px; }
#feed { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 6px; }
#feed a { display: flex; gap: 8px; align-items: baseline; background: var(--card); border: 2px solid var(--line);
          padding: 4px 8px; color: inherit; text-decoration: none; font-size: 13px; }
#feed a:hover { border-color: var(--ink-2); }
#feed .i { width: 12px; font-weight: 800; } #feed .t { margin-left: auto; color: var(--ink-2); white-space: nowrap; font-size: 12px; }
#feed .run .i { color: var(--ok); } #feed .bad .i { color: var(--bad); } #feed .wait .i { color: var(--warn); }
.dlg .now { margin: 6px 0; font-weight: 700; } .dlg .now.run { color: var(--ok); } .dlg .now.bad { color: var(--bad); }
.dlg .now a { color: inherit; }
footer { margin-top: 14px; color: var(--ink-2); font-size: 12px; text-align: center; }
footer a { color: inherit; }
@media (max-width: 640px) {
  .screen { padding: 4px; margin: 0 -8px; }
  button.row { grid-template-columns: 14px 1fr; } button.row span:last-child { grid-column: 2; }
}
"""

JS = r"""
const DATA = JSON.parse(document.getElementById('office-data').textContent);
const W = 352, H = 240, cv = document.getElementById('office');
let g = cv.getContext('2d');
const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
const rnd = (a, b) => a + Math.random() * (b - a);
const rect = (x, y, w, h, c) => { g.fillStyle = c; g.fillRect(Math.round(x), Math.round(y), w, h); };

// --- a 3x5 pixel font for nameplates, the whiteboard and bubbles
const F = {A:'010101111101101',B:'110101110101110',C:'011100100100011',D:'110101101101110',E:'111100110100111',
  F:'111100110100100',G:'011100101101011',H:'101101111101101',I:'111010010010111',J:'001001001101010',
  K:'101101110101101',L:'100100100100111',M:'101111111101101',N:'110101101101101',O:'010101101101010',
  P:'110101110100100',Q:'010101101110011',R:'110101110101101',S:'011100010001110',T:'111010010010010',
  U:'101101101101111',V:'101101101101010',W:'101101111111101',X:'101101010101101',Y:'101101010010010',
  Z:'111001010100111','0':'111101101101111','1':'010110010010111','2':'110001010100111','3':'110001010001110',
  '4':'101101111001001','5':'111100110001110','6':'011100111101111','7':'111001010010010','8':'111101111101111',
  '9':'111101111001110',' ':'000000000000000','-':'000000111000000','!':'010010010000010','?':'110001010000010',
  '.':'000000000000010',':':'000010000010000','$':'011110010011110','+':'000010111010000'};
function text(s, x, y, c) {
  g.fillStyle = c;
  [...s].forEach((ch, i) => { const b = F[ch] || F['?'];
    for (let k = 0; k < 15; k++) if (b[k] === '1') g.fillRect(x + i * 4 + k % 3, y + (k / 3 | 0), 1, 1); });
}

// --- sprites: '.' clear, H hair, S skin, E eyes, T shirt, D shirt shade, P pants, B shoes, R tie
const HEAD = ['...HHHHHH...','..HHHHHHHH..','..HSSSSSSH..','..SESSSSES..','..SSSSSSSS..','...SSSSSS...',
              '....SSSS....','..TTTTRTTT..','.TTTTTRTTTT.','.TTDTTRTDTT.'];
const SEAT = HEAD.concat(['.TTDTTTTDTT.']);
const STAND = HEAD.concat(['.STTTTTTTTS.','.STTTTTTTTS.','..TTTTTTTT..','...PPPPPP...','...PPPPPP...']);
const LEGS = [['...PP..PP...','...PP..PP...','...PP..PP...','..BBB..BBB..'],
              ['..PP....PP..','..PP....PP..','...PP..PP...','..BBB..BBB..'],
              ['...PP..PP...','...PP..PP...','....PP.PP...','...BBB.BBB..']];
function sprite(rows, x, y, look, boss) {
  const pal = {H: look[0], S: look[1], E: '#1b1b24', T: look[2], D: shade(look[2]), P: look[3], B: '#1d1a17',
               R: boss ? '#c0392b' : look[2]};
  rows.forEach((r, j) => [...r].forEach((ch, i) => { if (ch !== '.') rect(x + i, y + j, 1, 1, pal[ch]); }));
}
function shade(hex) {
  const n = parseInt(hex.slice(1), 16), f = v => Math.max(0, Math.round(v * .72));
  return `rgb(${f(n >> 16)},${f(n >> 8 & 255)},${f(n & 255)})`;
}

// --- the room (drawn once)
const hour = new Date().getHours(), night = hour < 7 || hour >= 19;
const room = document.createElement('canvas'); room.width = W; room.height = H;
function drawRoom() {
  const keep = g; g = room.getContext('2d');
  for (let y = 36; y < H; y += 8) for (let x = 0; x < W; x += 32) {
    const off = (y / 8) % 2 ? 16 : 0;
    rect(x + off - 32, y, 32, 8, ((x + y) / 8) % 3 ? '#b98b5a' : '#b28455'); rect(x + off - 32, y, 1, 8, '#9a7046');
    rect(x + off, y, 32, 8, ((x + y) / 8) % 3 ? '#b28455' : '#b98b5a'); rect(x + off, y, 1, 8, '#9a7046');
    rect(0, y + 7, W, 1, '#a57a4c');
  }
  rect(0, 0, W, 34, '#6d7f99'); rect(0, 30, W, 2, '#56657c'); rect(0, 32, W, 4, '#4a3a2c');
  for (const wx of [56, 260]) {
    rect(wx, 6, 36, 20, '#3a3a46'); rect(wx + 2, 8, 32, 16, night ? '#151d3b' : '#9fd3f5');
    rect(wx + 17, 8, 2, 16, '#3a3a46'); rect(wx + 2, 15, 32, 2, '#3a3a46');
    if (night) [[6, 10], [24, 19], [12, 20], [29, 11]].forEach(([a, b]) => rect(wx + a, b, 1, 1, '#fff6c2'));
    else rect(wx + 6, 10, 6, 2, '#ffffff');
  }
  rect(124, 4, 104, 24, '#e9edf2'); rect(124, 4, 104, 1, '#9aa3ad'); rect(124, 27, 104, 1, '#9aa3ad');
  rect(124, 4, 1, 24, '#9aa3ad'); rect(227, 4, 1, 24, '#9aa3ad'); text('TREND BOT', 132, 9, '#2a3a5a');
  [[170, 22], [176, 19], [182, 20], [188, 15], [194, 16], [200, 11], [206, 12], [212, 8], [218, 9]].forEach(([x, y], i, a) => {
    if (i) { const [px, py] = a[i - 1]; for (let k = 0; k <= 6; k++) rect(px + k, py + (y - py) * k / 6, 1, 1, '#2fa84f'); } });
  text('UP', 134, 18, '#c0392b');
  rect(16, 38, 26, 30, '#2a2c34'); rect(18, 40, 22, 26, '#383b45');                 // server rack
  for (let k = 0; k < 4; k++) rect(20, 42 + k * 6, 18, 4, '#23252c');
  rect(318, 42, 22, 26, '#565c66'); rect(320, 44, 18, 8, '#2d3036'); rect(326, 56, 6, 6, '#e8e1d4');   // coffee machine
  rect(323, 62, 12, 2, '#3a3d44'); rect(330, 46, 2, 2, '#ff5a3c');
  rect(296, 52, 14, 16, '#c9d8e6'); rect(298, 40, 10, 12, '#9fd3f5'); rect(296, 66, 14, 2, '#7f8c99');    // water cooler
  plant(6, 196); plant(338, 196); plant(108, 56); plant(236, 56);
  rect(0, 138, 4, 28, '#5a3a22'); rect(1, 140, 2, 24, '#7a5232'); rect(4, 142, 8, 20, '#6a4a6a');   // the door
  rect(126, 36, 100, 30, '#8f3b3b'); rect(128, 38, 96, 26, '#a54646');                // rug under the boss
  g = keep;
}
function plant(x, y) {
  rect(x, y + 10, 10, 8, '#a0522d'); rect(x + 1, y + 10, 8, 1, '#7a3d20');
  [[4, 0], [1, 3], [7, 3], [3, 5], [5, 2], [0, 7], [8, 6]].forEach(([a, b]) => rect(x + a, y + b, 3, 4, '#3f8f3a'));
}

// --- desks and people
const STATUS = {ok: '#2fa84f', warn: '#e7a91d', bad: '#e0453a', off: '#8a857b'};
const LIVE = DATA.live || {};
const live = {busy: {}, wait: {}, failed: {}, state: LIVE.repo ? 'starting' : 'off', runs: [], nextAt: 0};
const statusOf = k => live.failed[k] ? 'bad' : DATA.workers[k].status;
const busy = k => !!live.busy[k];
const people = {};
const desks = DATA.desks.map((key, i) => ({key, cx: 40 + (i % 5) * 68, top: i < 5 ? 104 : 170, row: i < 5 ? 0 : 1}));
desks.unshift({key: 'boss', cx: 176, top: 54, row: -1});
const AISLE = [86, 152], COFFEE = {x: 326, y: 80}, DOOR = {x: -10, y: AISLE[1]};
const seat = d => ({x: d.cx - 4, y: d.top - 1}), side = d => ({x: d.cx - 28, y: d.top + 10});

function want(p) {        // where a worker belongs right now: at the desk, up next to it (broken), or out
  const k = p.d.key, st = statusOf(k);
  if (k === 'boss' || busy(k) || live.wait[k]) return 'seat';
  return st === 'off' ? 'away' : st === 'bad' ? 'side' : 'seat';
}
function place(p) { const q = p.at === 'side' ? side(p.d) : p.at === 'away' ? DOOR : seat(p.d); p.x = q.x; p.y = q.y; }
desks.forEach(d => {
  d.w = DATA.workers[d.key];
  const p = people[d.key] = {d, look: d.w.look, at: 'seat', mode: 'rest', path: [], wait: 0, next: rnd(6, 30),
                             phase: rnd(0, 1000), steps: 0, out: false};
  p.at = want(p); place(p);
});
const boss = people.boss;
boss.next = 2;

function doorPath(d) {    // from the door (left wall) to a desk
  const pts = [{x: DOOR.x, y: AISLE[1]}, {x: 8, y: AISLE[1]}];
  if (d.row === 1) pts.push({x: d.cx - 4, y: AISLE[1]});
  else pts.push({x: 8, y: AISLE[0]}, {x: d.cx - 4, y: AISLE[0]});
  return pts.concat([seat(d)]);
}
function route(from, to, d) {
  if (from === to) return [];
  if (from === 'seat' && to === 'side') return [{x: d.cx - 28, y: d.top - 1}, side(d)];
  if (from === 'side' && to === 'seat') return [{x: d.cx - 28, y: d.top - 1}, seat(d)];
  if (from === 'away' && to === 'seat') return doorPath(d);
  if (from === 'seat' && to === 'away') return doorPath(d).reverse().slice(1);
  return route(from, 'seat', d).concat(route('seat', to, d));
}
function exitPath(d) {    // from a desk to the top walkway (second row: up the lane on its right)
  const pts = [seat(d), {x: d.cx - 4, y: d.row === 1 ? AISLE[1] : AISLE[0]}];
  if (d.row === 1) pts.push({x: d.cx + 34, y: AISLE[1]}, {x: d.cx + 34, y: AISLE[0]});
  if (d.row === -1) { pts.length = 1; pts.push({x: d.cx - 30, y: d.top - 1}, {x: d.cx - 30, y: AISLE[0]}); }
  return pts;
}
function visitPath(d) {   // from the top walkway to the right side of a worker's desk
  if (d.row === 0) return [{x: d.cx + 26, y: AISLE[0]}, {x: d.cx + 26, y: d.top + 10}];
  return [{x: d.cx + 34, y: AISLE[0]}, {x: d.cx + 34, y: AISLE[1]}, {x: d.cx + 26, y: AISLE[1]}, {x: d.cx + 26, y: d.top + 10}];
}

let visitIdx = 0, onBreak = null;
function go(p, pts, then) { p.mode = 'walk'; p.path = pts.slice(); p.then = then; }
function trip(p, there, stay, after) {   // walk somewhere from the desk, stay a while, come back
  const out = exitPath(p.d), path = out.concat(there);
  p.out = true;
  go(p, path, () => {
    p.wait = stay; p.after = () => {
      after && after();
      go(p, path.slice().reverse(), () => {
        p.out = false; p.visiting = null; p.next = p === boss ? 4 : rnd(20, 50);
        if (onBreak === p) onBreak = null;
      });
    };
  });
}
function update(p, dt) {
  if (p.mode === 'walk') {
    const t = p.path[0];
    if (!t) { p.mode = 'rest'; const f = p.then; p.then = null; f && f(); return; }
    const dx = t.x - p.x, dy = t.y - p.y, dist = Math.hypot(dx, dy), step = 34 * dt;
    if (dist <= step) { p.x = t.x; p.y = t.y; p.path.shift(); } else { p.x += dx / dist * step; p.y += dy / dist * step; }
    p.steps += step;
    return;
  }
  if (p.wait > 0) {
    p.wait = p.coffee && busy(p.d.key) ? 0 : p.wait - dt;   // work came in: cut the coffee break short
    if (p.wait <= 0 && p.after) { const f = p.after; p.after = null; f(); }
    return;
  }
  if (p.out) return;
  const w = want(p);
  if (w !== p.at) {
    if (p.at === 'away') { p.x = DOOR.x; p.y = DOOR.y; }
    go(p, route(p.at, w, p.d), () => { p.at = w; });
    return;
  }
  p.next -= dt;
  if (p.next > 0) return;
  if (p === boss) {       // the boss checks on whoever has a problem, then whoever is busy
    const problems = desks.filter(d => d.key !== 'boss' && ['bad', 'warn'].includes(statusOf(d.key)));
    const working = desks.filter(d => d.key !== 'boss' && busy(d.key) && !problems.includes(d));
    const todo = problems.concat(working);
    if (!todo.length) { p.next = 3; return; }
    const d = todo[visitIdx++ % todo.length];
    trip(p, visitPath(d), 4, null);
    p.visiting = d;
    return;
  }
  if (onBreak || p.at !== 'seat' || busy(p.d.key) || statusOf(p.d.key) !== 'ok') { p.next = rnd(5, 20); return; }
  onBreak = p;
  p.coffee = true;
  trip(p, [{x: COFFEE.x, y: AISLE[0]}, COFFEE], 3, () => { p.coffee = false; });
}
const seated = p => p.at === 'seat' && !p.out && p.mode !== 'walk';
const visible = p => !(p.at === 'away' && p.mode !== 'walk');

function drawDesk(d, t) {
  const {cx, top} = d, st = statusOf(d.key), big = d.key === 'boss', b = busy(d.key);
  const half = big ? 26 : 20;
  rect(cx - half, top, half * 2, 6, big ? '#7a4a2a' : '#c8945a'); rect(cx - half, top + 6, half * 2, 8, big ? '#5a341c' : '#8a5a32');
  rect(cx - half, top + 14, 2, 3, '#4a2f1a'); rect(cx + half - 2, top + 14, 2, 3, '#4a2f1a');
  rect(cx - 9, top + 1, 10, 3, '#5c6470'); rect(cx - 8, top + 2, 8, 1, '#8b95a3');          // keyboard
  rect(cx - half + 3, top + 1, 3, 3, '#e8e1d4');                                        // mug
  rect(cx + 3, top - 12, 14, 11, '#24242e'); rect(cx + 9, top - 1, 2, 2, '#24242e');     // monitor
  const sx = cx + 4, sy = top - 11, blink = Math.floor(t * 2 + d.cx) % 2;
  if (b) {                // working right now: code scrolling fast
    rect(sx, sy, 12, 8, '#0e3b22');
    for (let k = 0; k < 4; k++) { const len = 2 + ((k * 5 + Math.floor(t * 9) + d.cx) % 9); rect(sx + 1, sy + 1 + k * 2, Math.min(len, 10), 1, '#8affb0'); }
  } else if (st === 'ok') {   // fine and idle: a screensaver
    rect(sx, sy, 12, 8, '#0b2618');
    const k = Math.floor(t * 3 + d.cx) % 20, x = k < 10 ? k : 19 - k;
    rect(sx + 1 + x, sy + 3 + (Math.floor(t + d.cx) % 3), 2, 1, '#3fae66');
  } else if (st === 'warn') {
    rect(sx, sy, 12, 8, '#4a3a0a'); if (blink) text('!', sx + 4, sy + 1, '#f5c451');
  } else if (st === 'bad') {
    rect(sx, sy, 12, 8, blink ? '#e03030' : '#7a1010'); text('X', sx + 4, sy + 1, '#ffffff');
    if (!still) for (let k = 0; k < 3; k++) {
      const age = (t * 0.8 + k / 3) % 1;
      rect(cx + 9 + Math.sin(age * 6 + k) * 3, top - 14 - age * 14, 3, 3, `rgba(90,90,90,${0.7 * (1 - age)})`);
    }
  } else {
    rect(sx, sy, 12, 8, '#111116'); rect(cx + 12, top - 13, 6, 5, '#f7e37a');               // sticky note
  }
  const label = DATA.plates[d.key], w = label.length * 4 + 7, px = cx - Math.round(w / 2);
  rect(px, top + 18, w, 9, '#2a211b'); rect(px + 2, top + 21, 3, 3, b && blink ? '#8affb0' : STATUS[st]);
  text(label, px + 7, top + 20, st === 'off' && !b ? '#9b9387' : '#f3e7cf');
}
function drawChair(d) {
  rect(d.cx - 12, d.top - 8, 16, 8, '#3b3f4a'); rect(d.cx - 11, d.top - 7, 14, 6, '#4b505d');
}
function drawSeated(p, t) {
  const idle = !busy(p.d.key) && statusOf(p.d.key) !== 'warn';
  const bob = idle && Math.floor((t * 1000 + p.phase) / 900) % 5 === 0 ? 1 : 0;
  sprite(SEAT, p.d.cx - 10, p.d.top - 11 + bob, p.look, p.d.key === 'boss');
}
function drawHands(p, t) {
  const skin = p.look[1], y = p.d.top + 1;
  if (busy(p.d.key) && !still) {
    const f = Math.floor((t * 1000 + p.phase) / 110) % 2;
    rect(p.d.cx - 9 + f, y - f, 2, 2, skin); rect(p.d.cx - 3 - f, y - (1 - f), 2, 2, skin);
  } else { rect(p.d.cx - 8, y + 1, 2, 2, skin); rect(p.d.cx - 2, y + 1, 2, 2, skin); }
}
function drawStanding(p, t) {
  const moving = p.mode === 'walk', f = moving ? 1 + Math.floor(p.steps / 5) % 2 : 0;
  const x = Math.round(p.x) - 6, y = Math.round(p.y) - 19 - (moving && f === 1 ? 1 : 0);
  rect(x + 2, Math.round(p.y) - 1, 9, 2, 'rgba(0,0,0,.18)');
  sprite(STAND.concat(LEGS[f]), x, y, p.look, p.d.key === 'boss');
  if (p.coffee && !moving) { rect(x + 10, y + 11, 3, 3, '#ffffff'); if (!still && Math.floor(t * 3) % 2) rect(x + 11, y + 8, 1, 2, '#cfcfcf'); }
}
const BUBBLE = {warn: ['#f5c451', '!', '#3a2a00'], bad: ['#e0453a', '!', '#ffffff'], visit: ['#ffffff', '?', '#2a211b'],
                fine: ['#2fa84f', '+', '#ffffff'], wait: ['#ffffff', '', '#2a211b']};
function bubble(x, y, kind, t) {
  const [bg, ch, fg] = BUBBLE[kind], b = still ? 0 : Math.round(Math.sin(t * 4) * 1);
  x = Math.round(x); y = Math.round(y) + b;
  rect(x - 1, y - 1, 11, 11, '#1b1b24'); rect(x, y, 9, 9, bg); rect(x + 3, y + 10, 3, 2, '#1b1b24'); rect(x + 4, y + 10, 1, 1, bg);
  if (ch) text(ch, x + 3, y + 2, fg);
  else for (let k = 0; k < 3; k++) rect(x + 2 + k * 2, y + 4 - (!still && Math.floor(t * 3) % 3 === k ? 1 : 0), 1, 1, fg);
}

function frame(t) {
  g.drawImage(room, 0, 0);
  const blink = Math.floor(t * 2) % 2, fast = Math.floor(t * 8) % 2;
  ['prices', 'sec', 'finra', 'news'].forEach((k, i) => {   // the server rack: one light per data source
    const st = statusOf(k);
    rect(34, 43 + i * 6, 2, 2, busy(k) ? (fast ? '#8affb0' : '#1f5f35') : st === 'ok' ? '#3fae66' : st === 'off' ? '#444' : blink ? STATUS[st] : '#333');
  });
  const items = [];
  desks.forEach(d => {
    const p = people[d.key];
    items.push({y: d.top - 1.5, f: () => drawChair(d)});
    if (seated(p)) items.push({y: d.top - 0.5, f: () => drawSeated(p, t)});
    items.push({y: d.top, f: () => { drawDesk(d, t); seated(p) && drawHands(p, t); }});
  });
  Object.values(people).forEach(p => { if (visible(p) && !seated(p)) items.push({y: p.y, f: () => drawStanding(p, t)}); });
  items.sort((a, b) => a.y - b.y).forEach(i => i.f());
  Object.values(people).forEach(p => {
    if (!visible(p)) return;
    const k = p.d.key, st = statusOf(k), s = seated(p), hx = s ? p.d.cx - 4 : p.x, hy = s ? p.d.top - 24 : p.y - 32;
    if (p === boss) {
      if (p.out && p.mode !== 'walk' && p.visiting && ['bad', 'warn'].includes(statusOf(p.visiting.key))) bubble(hx - 4, hy, 'visit', t);
      else if (s && st === 'ok' && !Object.keys(live.busy).length) bubble(hx - 4, hy, 'fine', t);
      return;
    }
    if (live.wait[k] && !busy(k)) bubble(hx - 4, hy, 'wait', t);
    else if (st === 'warn' || st === 'bad') bubble(hx - 4, hy, st, t);
  });
  const now = new Date(), hh = now.getHours(), mm = now.getMinutes(), cx = 108, cy = 17;   // the wall clock
  rect(cx - 6, cy - 6, 13, 13, '#2a211b'); rect(cx - 5, cy - 5, 11, 11, '#fbf6e9');
  for (let k = 0; k <= 4; k++) {
    const a = (mm / 60) * 2 * Math.PI, b = ((hh % 12) / 12 + mm / 720) * 2 * Math.PI;
    rect(cx + Math.sin(a) * k, cy - Math.cos(a) * k, 1, 1, '#2a211b');
    if (k <= 3) rect(cx + Math.sin(b) * k, cy - Math.cos(b) * k, 1, 1, '#c0392b');
  }
}

drawRoom();
let last = performance.now();
function loop(now) {
  const dt = Math.min(0.1, (now - last) / 1000); last = now;
  Object.values(people).forEach(p => update(p, dt));
  frame(now / 1000);
  requestAnimationFrame(loop);
}
if (still) { const tick = () => { Object.values(people).forEach(p => { p.at = want(p); place(p); }); frame(0); };
             tick(); setInterval(tick, 5000); }
else requestAnimationFrame(loop);

// --- live: what's running on GitHub Actions right now (the public API: no key needed, ~60 requests an hour)
const API = 'https://api.github.com/repos/' + LIVE.repo;
const WAITING = ['queued', 'waiting', 'requested', 'pending'], FAILED = ['failure', 'timed_out', 'startup_failure'];
let quota = null, timer = null, nightlyRunning = false;
function stepWorkers(name) {
  const n = name.toLowerCase();
  for (const [words, keys] of LIVE.steps) if (n.includes(words)) return keys;
  return ['logbook'];
}
async function get(url) {
  const r = await fetch(API + url, {headers: {Accept: 'application/vnd.github+json'}});
  const left = r.headers.get('x-ratelimit-remaining'), reset = r.headers.get('x-ratelimit-reset');
  if (left !== null) quota = {left: +left, reset: +reset};
  if (!r.ok) { const e = new Error('GitHub ' + r.status); e.status = r.status; throw e; }
  return r.json();
}
async function poll() {
  if (document.hidden) { live.nextAt = 0; return; }   // paused while the page isn't visible
  let calls = 0, next = 120;
  try {
    const runs = (await get('/actions/runs?per_page=30')).workflow_runs; calls++;
    const nowBusy = {}, nowWait = {}, nowFailed = {}, seen = new Set();
    let nightly = false;
    for (const r of runs) {
      const wf = r.path.split('/').pop(), who = LIVE.workflows[wf];
      if (!who) continue;
      const since = new Date(r.run_started_at || r.created_at), keys = who === 'steps' ? null : [who];
      if (r.status === 'in_progress') {
        if (keys) { keys.forEach(k => nowBusy[k] = {text: r.name, since, url: r.html_url}); continue; }
        nightly = true;
        const jobs = (await get(`/actions/runs/${r.id}/jobs`)).jobs; calls++;
        for (const j of jobs.filter(j => j.status === 'in_progress')) {
          const s = j.steps.find(s => s.status === 'in_progress') || [...j.steps].reverse().find(s => s.status === 'completed');
          const name = s ? s.name : j.name, into = LIVE.wait_steps.some(w => name.toLowerCase().includes(w)) ? nowWait : nowBusy;
          const total = j.steps.filter(s => !/^(Post |Complete job)/.test(s.name)).length;
          stepWorkers(name).forEach(k => into[k] = {text: `${r.name}: ${name}` + (s ? ` (step ${s.number} of ${total})` : ''),
                                                     since: new Date((s && s.started_at) || since), url: r.html_url});
        }
      } else if (WAITING.includes(r.status)) {
        (keys || ['boss']).forEach(k => nowWait[k] = nowWait[k] || {text: `${r.name}: waiting to start`, since, url: r.html_url});
      } else if (r.status === 'completed' && !seen.has(wf)) {
        seen.add(wf);   // only the newest finished run of each workflow counts: a later success clears a failure
        if (FAILED.includes(r.conclusion) && Date.now() - new Date(r.updated_at) < 3 * 864e5)
          (keys || ['boss']).forEach(k => nowFailed[k] = {text: `${r.name} failed`, since: new Date(r.updated_at), url: r.html_url});
      }
    }
    Object.assign(live, {busy: nowBusy, wait: nowWait, failed: nowFailed, runs, state: 'live'});
    if (nightlyRunning && !nightly && location.protocol.startsWith('http'))
      setTimeout(() => location.reload(), 6 * 60e3);   // the nightly run publishes a fresh page: pick it up
    nightlyRunning = nightly;
    const active = Object.keys(nowBusy).length + Object.keys(nowWait).length;
    next = active ? 60 : 120;
    if (quota) {          // spread what's left of the hourly allowance until it resets
      const secs = Math.max(1, quota.reset - Date.now() / 1000), polls = quota.left / calls - 1;
      next = Math.max(next, polls > 0 ? secs / polls : secs + 5);
    }
  } catch (e) {
    live.state = e.status === 403 || e.status === 429 ? 'limited' : e.status === 404 ? 'missing' : 'offline';
    next = live.state === 'limited' && quota ? Math.max(60, quota.reset - Date.now() / 1000 + 5) : live.state === 'missing' ? 900 : 120;
  }
  live.nextAt = Date.now() + next * 1000;
  renderLive();
  clearTimeout(timer); timer = setTimeout(poll, next * 1000);
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && LIVE.repo && Date.now() >= live.nextAt) { clearTimeout(timer); poll(); }
});

const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
function ago(d) {
  const s = (Date.now() - d) / 1000;
  return s < 90 ? 'just now' : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;
}
const forHow = d => ago(d).replace(' ago', '').replace('just now', 'a moment');
function renderLive() {
  const bar = document.getElementById('live'), feed = document.getElementById('feed');
  const doing = [...new Map(Object.entries(live.busy).map(([k, v]) => [v.text, {...v, who: DATA.workers[k].name}])).values()];
  let html;
  if (live.state === 'live') {
    const latest = live.runs.find(r => r.status === 'completed');
    html = doing.length
      ? `<span class="beat on"></span><b>LIVE</b> ${doing.map(v => `<a href="${esc(v.url)}">${esc(v.text)}</a> · ${forHow(v.since)}`).join(' &nbsp;|&nbsp; ')}`
      : `<span class="beat"></span><b>LIVE</b> Nothing running right now` + (latest ? ` · last: ${esc(latest.name)} ${latest.conclusion === 'success' ? '✓' : '✗'} ${ago(new Date(latest.updated_at))}` : '');
  } else html = {
    starting: 'Connecting to GitHub…',
    off: 'Live view off: the page doesn\'t know which repository to watch.',
    missing: 'Live view off: GitHub didn\'t find the repository (it has to be public).',
    offline: 'Can\'t reach GitHub right now; trying again in a couple of minutes.',
    limited: `Live view paused: GitHub's hourly limit for this network is used up` +
             (quota ? ` (back at ${new Date(quota.reset * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})})` : '') + '.',
  }[live.state];
  bar.innerHTML = html;
  feed.innerHTML = live.runs.filter(r => LIVE.workflows[r.path.split('/').pop()]).slice(0, 8).map(r => {
    const icon = r.status === 'in_progress' ? '●' : WAITING.includes(r.status) ? '…' : r.conclusion === 'success' ? '✓' : FAILED.includes(r.conclusion) ? '✗' : '–';
    const cls = r.status === 'in_progress' ? 'run' : WAITING.includes(r.status) ? 'wait' : FAILED.includes(r.conclusion) ? 'bad' : 'done';
    const when = r.status === 'in_progress' ? `running for ${forHow(new Date(r.run_started_at))}` : ago(new Date(r.updated_at));
    return `<li class="${cls}"><a href="${esc(r.html_url)}"><span class="i">${icon}</span>${esc(r.name)}<span class="t">${when}</span></a></li>`;
  }).join('');
}
if (LIVE.repo) poll();
renderLive();
setInterval(renderLive, 20000);

// --- details
const dlg = document.getElementById('dlg');
function show(key) {
  const d = DATA.workers[key], st = statusOf(key);
  const now = live.busy[key] ? `<p class="now run">● Working right now: <a href="${esc(live.busy[key].url)}">${esc(live.busy[key].text)}</a>, for ${forHow(live.busy[key].since)}</p>`
            : live.wait[key] ? `<p class="now wait">… ${esc(live.wait[key].text)} <a href="${esc(live.wait[key].url)}">open the run</a></p>`
            : live.failed[key] ? `<p class="now bad">✗ <a href="${esc(live.failed[key].url)}">${esc(live.failed[key].text)}</a> ${ago(live.failed[key].since)}</p>` : '';
  dlg.querySelector('.dlg').innerHTML =
    `<button class="close" aria-label="Close">✕</button>
     <h2><span class="dot ${st}"></span>${esc(d.name)}</h2>
     <div class="role">${esc(d.role)} · <b>${esc(DATA.labels[st])}</b></div>${now}
     <div class="head">${esc(d.headline)}</div>
     <ul>${d.details.map(x => `<li>${esc(x)}</li>`).join('')}</ul>
     <div class="role">Last nightly check: ${esc(DATA.made)}</div>`;
  dlg.querySelector('.close').onclick = () => dlg.close();
  dlg.showModal();
}
dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
document.querySelectorAll('button.row').forEach(b => b.addEventListener('click', () => show(b.dataset.key)));
function hit(e) {
  const r = cv.getBoundingClientRect(), x = (e.clientX - r.left) * W / r.width, y = (e.clientY - r.top) * H / r.height;
  const p = Object.values(people).find(p => visible(p) && !seated(p) && Math.abs(x - p.x) <= 8 && y >= p.y - 22 && y <= p.y + 2);
  if (p) return p.d.key;
  const d = desks.find(d => Math.abs(x - d.cx) <= (d.key === 'boss' ? 40 : 34) && y >= d.top - 30 && y <= d.top + 30);
  return d && d.key;
}
cv.addEventListener('click', e => { const k = hit(e); if (k) show(k); });
cv.addEventListener('mousemove', e => { cv.style.cursor = hit(e) ? 'pointer' : 'default'; });
"""


def build_office(workers: list[dict], made: dt.datetime | None = None, run_url: str | None = None,
                 repo: str | None = None) -> str:
    """The office page (one self-contained HTML file); with repo (owner/name), it also shows GitHub Actions live."""
    made = made or dt.datetime.now()
    data = {"desks": [k for k in DESKS if any(w["key"] == k for w in workers)], "plates": PLATE,
            "made": f"{made:%Y-%m-%d %H:%M}", "labels": LABEL,
            "live": {"repo": repo, "workflows": LIVE_WORKFLOWS, "steps": LIVE_STEPS, "wait_steps": LIVE_WAIT_STEPS},
            "workers": {w["key"]: {**w, "label": LABEL[w["status"]], "look": LOOKS[w["key"]]} for w in workers}}
    counts = {s: sum(w["status"] == s for w in workers[1:]) for s in (OK, WARN, BAD, OFF)}
    tally = "".join(f'<span class="pill"><span class="dot {s}"></span>{counts[s]} {LABEL[s].lower()}</span>'
                    for s in (OK, WARN, BAD, OFF) if counts[s])
    rows = "".join(f'<button class="row" data-key="{w["key"]}"><span class="dot {w["status"]}"></span>'
                   f'<b>{html.escape(w["name"])}</b><span>{html.escape(w["headline"])}</span></button>' for w in workers)
    boss = next((w for w in workers if w["key"] == "boss"), {"headline": ""})
    alt = "; ".join(f"{w['name']}: {LABEL[w['status']].lower()}" for w in workers)
    link = f' · <a href="{html.escape(run_url)}">tonight\'s run</a>' if run_url else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trend Bot Office</title>
<style>{CSS}</style></head>
<body><main>
<header>
  <div><h1>Trend Bot Office</h1>
  <div class="sub">Every part of the bot is a worker at a desk, working live as the bot runs. Click one to see how it's doing.</div></div>
  <div class="tally">{tally}</div>
</header>
<div class="live" id="live" role="status" aria-live="polite"></div>
<div class="screen"><canvas id="office" width="352" height="240" role="img" aria-label="{html.escape(alt)}"></canvas></div>
<div class="legend">
  <span><span class="dot ok"></span>Working: typing fast while its part of the bot runs, resting in between</span>
  <span><span class="dot warn"></span>Needs a look: "!" bubble</span>
  <span><span class="dot bad"></span>Broken: up from the desk, red smoking screen</span>
  <span><span class="dot off"></span>Not set up: empty desk</span>
</div>
<section class="runs"><h2>Recent runs on GitHub</h2><ol id="feed"></ol></section>
<section class="list"><h2>{html.escape(boss['headline'])}</h2>{rows}</section>
<footer>Health checked {made:%Y-%m-%d %H:%M}{link}; the workers follow GitHub Actions live.
It only reads; nothing here changes anything.</footer>
<dialog id="dlg"><div class="dlg"></div></dialog>
</main>
<script id="office-data" type="application/json">{json.dumps(data).replace("</", "<\\/")}</script>
<script>{JS}</script>
</body></html>"""
