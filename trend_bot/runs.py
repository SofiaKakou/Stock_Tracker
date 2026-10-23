"""The logbook: every command run leaves a line, and every result carries a stamp.

The stamp says which code, which data and which settings produced a result, so an old result can
never be mistaken for a new one, and any number can be traced back to the run that made it:

  code 3f9a1c2 · data 7b41e0 (prices to 2026-10-22) · settings 5d02aa

- code: the git commit (GitHub provides it), else a hash of the bot's source files
- data: a fingerprint of what's in the database (row counts and latest dates of every source)
- settings: a hash of the command's options

Ideas from passivbot (code and data fingerprints on every result) and OpenTrader (a per-run log).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

CURRENT: dict = {}        # the run in progress: notes added with note()
_SOURCES = [("prices", "date"), ("facts", "filed"), ("filings", "filed"), ("events", "filed"),
            ("insider_trades", "filed"), ("short_interest", "settle"), ("short_volume", "month"),
            ("company_fates", "fate_date"), ("doc_vectors", None), ("splits", "date")]
_SKIP_SETTINGS = {"func"}


def _short(text: str, n: int = 7) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:n]


def code_version() -> str:
    """The git commit (from GitHub, or the local checkout), else a hash of the source files."""
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha[:7]
    root = Path(__file__).resolve().parent
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short=7", "HEAD"], capture_output=True,
                             text=True, timeout=5)
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "."], capture_output=True,
                               text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip() + ("+edits" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        pass
    files = sorted(root.rglob("*.py"))
    return "src-" + _short("".join(f.read_text(encoding="utf-8", errors="replace") for f in files))


def data_fingerprint(con: sqlite3.Connection) -> dict:
    """Row counts and latest dates of every data source, and a short hash of them."""
    parts = {}
    for table, col in _SOURCES:
        try:
            if col:
                n, last = con.execute(f"SELECT COUNT(*), MAX({col}) FROM {table}").fetchone()
            else:
                n, last = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], None
        except sqlite3.Error:
            continue
        parts[table] = [n, str(last)[:10] if last else None]
    prices_to = (parts.get("prices") or [0, None])[1]
    return {"id": _short(json.dumps(parts, sort_keys=True), 6), "prices_to": prices_to, "parts": parts}


def settings_of(args) -> dict:
    out = {}
    for k, v in sorted(vars(args).items()):
        if k in _SKIP_SETTINGS:
            continue
        try:
            json.dumps(v)
            out[k] = v
        except TypeError:
            out[k] = str(v)
    return out


def stamp(code: str, data: dict, settings: dict | None = None) -> str:
    s = f"code {code} · data {data.get('id', '?')}"
    if data.get("prices_to"):
        s += f" (prices to {data['prices_to']})"
    if settings is not None:
        s += f" · settings {_short(json.dumps(settings, sort_keys=True), 6)}"
    return s


def start(db_path: str | None, command: str, settings: dict) -> dict:
    """Begin a run: its stamp (code, data, settings), shown by the commands that make results."""
    CURRENT.clear()
    CURRENT.update(command=command, settings=settings, started=dt.datetime.now().isoformat(timespec="seconds"),
                   t0=time.time(), notes={}, code=code_version(), data={}, db=db_path)
    if db_path and Path(db_path).exists():
        try:
            from trend_bot import db

            con = db.connect(db_path)
            try:
                CURRENT["data"] = data_fingerprint(con)
            finally:
                con.close()
        except sqlite3.Error:
            pass
    CURRENT["stamp"] = stamp(CURRENT["code"], CURRENT["data"], settings)
    return CURRENT


def current_stamp() -> str:
    """The stamp of the run in progress (empty outside a run)."""
    return CURRENT.get("stamp", "")


def note(key: str, value) -> None:
    """Add something worth keeping to this run's logbook line, e.g. note('stocks', 912)."""
    if CURRENT:
        CURRENT["notes"][key] = value


def finish(exit_code: int, error: str | None = None) -> None:
    """Write this run's logbook line (never fails the command over it)."""
    if not CURRENT or not CURRENT.get("db") or not Path(CURRENT["db"]).exists():
        return
    try:
        from trend_bot import db

        con = db.connect(CURRENT["db"])
        try:
            data = CURRENT["data"]
            con.execute("INSERT INTO run_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (CURRENT["started"], dt.datetime.now().isoformat(timespec="seconds"), CURRENT["command"],
                         CURRENT["code"], json.dumps({"id": data.get("id"), "prices_to": data.get("prices_to")}),
                         json.dumps(CURRENT["settings"], sort_keys=True), json.dumps(CURRENT["notes"], default=str),
                         int(exit_code), (error or "")[:500] or None, round(time.time() - CURRENT["t0"], 1)))
            con.execute("DELETE FROM run_log WHERE rowid NOT IN (SELECT rowid FROM run_log ORDER BY started DESC LIMIT 2000)")
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass


def history(con: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = con.execute("SELECT started, command, code, data, notes, exit_code, error, seconds FROM run_log "
                       "ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for started, command, code, data, notes, exit_code, error, seconds in rows:
        out.append({"started": started, "command": command, "code": code, "data": json.loads(data or "{}"),
                    "notes": json.loads(notes or "{}"), "ok": exit_code == 0 and not error, "error": error,
                    "seconds": seconds})
    return out
