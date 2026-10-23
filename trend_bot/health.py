"""Is the bot itself OK? A short Discord warning when something is wrong, silence when it isn't.

Checks how fresh each kind of data is (prices, company financials, the SEC bulk file,
short selling data, the monthly ideas list), whether the SEC has asked us to slow down,
and which steps of tonight's cloud run didn't finish. The same warning isn't repeated
every night: it's sent when the list of problems changes, or again after 3 days, and
once more ("all fine again") when everything is back to normal.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import numpy as np

from trend_bot.db import get_meta, set_meta

REPEAT_AFTER_DAYS = 3


def _age_days(value: str | None, today: dt.date) -> int | None:
    if not value:
        return None
    return (today - dt.date.fromisoformat(value[:10])).days


def check(con: sqlite3.Connection, today: dt.date | None = None, failed_steps: list[str] | None = None,
          job_status: str | None = None) -> list[str]:
    """Problems found, in plain words (empty: all fine)."""
    today = today or dt.date.today()
    problems = [f"Tonight's run: {s}" for s in failed_steps or []]
    if job_status and job_status not in ("success", ""):
        problems.append(f"Tonight's cloud run ended with status '{job_status}' (a step failed).")

    last_price = con.execute("SELECT MAX(last_date) FROM tickers").fetchone()[0]
    if not last_price:
        problems.append("No prices in the database yet.")
    else:
        behind = int(np.busday_count(dt.date.fromisoformat(last_price[:10]), today))
        if behind > 2:
            problems.append(f"Prices are {behind} trading days old (last {last_price[:10]}).")

    paused = get_meta(con, "sec_paused_until")
    if paused and paused > dt.datetime.now().isoformat():
        said = get_meta(con, "sec_last_refusal")
        problems.append(f"The SEC asked us to slow down: SEC downloads paused until {paused[:16].replace('T', ' ')} UTC."
                        + (f" ({said})" if said else ""))

    for key, label, limit in (("facts_updated", "Company financials", 10),
                              ("submissions_updated", "Industry codes and company fates (SEC bulk file)", 10),
                              ("short_volume_last", "Short selling data", 7)):
        age = _age_days(get_meta(con, key), today)
        if age is None:
            problems.append(f"{label}: never downloaded yet.")
        elif age > limit:
            problems.append(f"{label}: last updated {age} days ago.")

    audit = json.loads(get_meta(con, "data_audit") or "{}")
    if audit.get("problems") and (_age_days(audit["date"], today) or 0) <= 40:
        problems.append(f"The monthly data audit ({audit['date']}) found something to look into: "
                        f"{', '.join(audit['problems'])}. Details in that night's 'study audit' output.")

    raw = get_meta(con, "mix_ranking")
    age = _age_days(json.loads(raw)["date"], today) if raw else None
    if age is None:
        problems.append("The monthly top-ideas list hasn't been made yet.")
    elif age > 40:
        problems.append(f"The monthly top-ideas list is {age} days old (it should refresh each month).")
    return problems


def to_send(con: sqlite3.Connection, problems: list[str], today: dt.date | None = None) -> str | None:
    """'problems', 'recovered' or None (nothing new to say). Remembers what was last sent."""
    today = today or dt.date.today()
    last = json.loads(get_meta(con, "health_sent") or "{}")
    sig = sorted(problems)
    if problems:
        stale = _age_days(last.get("date"), today)
        if sig != last.get("problems") or stale is None or stale >= REPEAT_AFTER_DAYS:
            set_meta(con, "health_sent", json.dumps({"problems": sig, "date": today.isoformat()}))
            con.commit()
            return "problems"
        return None
    if last.get("problems"):
        set_meta(con, "health_sent", json.dumps({"problems": [], "date": today.isoformat()}))
        con.commit()
        return "recovered"
    return None


def embed(kind: str, problems: list[str], run_url: str | None = None) -> dict:
    from trend_bot.alerts import GREEN, RED

    if kind == "recovered":
        return {"title": "✅ Bot health: all fine again", "color": GREEN,
                "description": "**What this means:** the earlier problem is fixed. Every check passed tonight: "
                               "fresh prices, data up to date, no SEC slow-down. Nothing to do."}
    lines = ["**What this means:** something went wrong with the bot itself, not with your stocks. "
             "It usually fixes itself on a later night; nothing to do unless this keeps coming back.", ""]
    lines += [f"• {p}" for p in problems]
    lines += ["", "Until it clears, some messages or the report may be missing or a bit out of date."]
    if run_url:
        lines.append(f"[Open tonight's run]({run_url})")
    return {"title": "⚠️ Bot health", "description": "\n".join(lines)[:4000], "color": RED}


def read_problems_file(path: str | Path | None) -> list[str]:
    if not path or not Path(path).exists():
        return []
    return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]


def catchup_embed(con: sqlite3.Connection) -> dict | None:
    """'SEC data is back' once, after a full SEC update follows a gap (then forgotten)."""
    from trend_bot.alerts import GREEN

    raw = get_meta(con, "sec_catchup")
    if not raw:
        return None
    c = json.loads(raw)
    gap = f" after {c['gap_days']} days without it" if c.get("gap_days") else ""
    lines = [f"• Insider trades: {c.get('filings', 0):,} recent filing(s) read",
             f"• Company financials: {c.get('companies', 0):,} companies updated" if c.get("companies")
             else "• Company financials: already up to date",
             "• Industry codes and company fates: SEC bulk file read" if c.get("bulk")
             else "• Industry codes and company fates: bulk file not due yet (weekly)",
             f"• Company reports: {c.get('reports', 0):,} read for the text signals"]
    return {"title": "✅ SEC data is back", "color": GREEN,
            "description": f"Tonight's run downloaded from the SEC again{gap}:\n" + "\n".join(lines)}

