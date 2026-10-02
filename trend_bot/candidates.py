"""The scoreboard for signals on probation (ml.CANDIDATES).

New signals start as candidates: measured, but kept out of the simple mix and the model. Once
a month the bot checks each one against the same bar every signal has had to clear:

1. On its own, it ranked stocks the expected way (positive IC) in **both halves** of its
   history, and the whole-history IC is unlikely to be luck (t-stat of 2 or more).
2. Added to the simple mix, it made the mix rank better in **both halves**.

Halves are split at the middle of the candidate's own history, since some signals start
later than others (short selling data from 2018, reports as they are read). A candidate
that passes is flagged in Discord; it joins the mix only after the user agrees.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import numpy as np
import pandas as pd

from trend_bot import factors, fundamentals, ml
from trend_bot.db import get_meta, set_meta

MIN_MONTHS = 24          # per half: less than two years each way is too little to judge
MIN_T = 2.0


def _mix_ic(df: pd.DataFrame, signals: list[str]) -> float:
    return factors.evaluate(df.assign(_mix=ml.simple_mix(df, signals=signals)), "_mix")["mean_ic"]


def scoreboard(df: pd.DataFrame) -> pd.DataFrame:
    """One row per candidate: its IC overall and in each half, what it adds to the mix, and a verdict."""
    rows = {}
    for c in sorted(ml.CANDIDATES):
        if c not in df or df[c].notna().sum() == 0:
            rows[c] = {"verdict": "no data yet"}
            continue
        sign = fundamentals.EXPECTED.get(c, 1)
        months = pd.DatetimeIndex(sorted(df.loc[df[c].notna(), "month"].unique()))
        mid = months[len(months) // 2]
        part = df[df["month"].isin(months)]
        halves = {"first": part[part["month"] < mid], "second": part[part["month"] >= mid]}
        whole = factors.evaluate(part, c, sign)
        row = {"from": f"{months[0]:%Y-%m}", "months": whole["months"], "ic": whole["mean_ic"], "ic_t": whole["ic_t"]}
        for name, h in halves.items():
            row[f"ic_{name}"] = factors.evaluate(h, c, sign)["mean_ic"]
            row[f"mix_gain_{name}"] = _mix_ic(h, ml.SIGNALS + [c]) - _mix_ic(h, ml.SIGNALS)
            row[f"months_{name}"] = h["month"].nunique()
        if min(row["months_first"], row["months_second"]) < MIN_MONTHS:
            row["verdict"] = "not enough history yet"
        elif (row["ic_first"] > 0 and row["ic_second"] > 0 and row["ic_t"] >= MIN_T
              and row["mix_gain_first"] > 0 and row["mix_gain_second"] > 0):
            row["verdict"] = "PASSES"
        else:
            row["verdict"] = "not proven"
        rows[c] = row
    return pd.DataFrame(rows).T


def save(con: sqlite3.Connection, board: pd.DataFrame) -> None:
    payload = {"date": dt.date.today().isoformat(),
               "rows": json.loads(board.replace({np.nan: None}).to_json(orient="index"))}
    set_meta(con, "candidates_scoreboard", json.dumps(payload))
    con.commit()


def load(con: sqlite3.Connection) -> dict | None:
    raw = get_meta(con, "candidates_scoreboard")
    return json.loads(raw) if raw else None


def text(board: pd.DataFrame) -> str:
    fmt = lambda v, f: "" if v is None or (isinstance(v, float) and np.isnan(v)) else f.format(v)
    lines = []
    for name, r in board.iterrows():
        r = r.to_dict()
        if r.get("verdict") == "no data yet":
            lines.append(f"  {name:<16} no data yet")
            continue
        lines.append(f"  {name:<16} {r['verdict']:<24} IC {fmt(r['ic'], '{:+.3f}')} (t {fmt(r['ic_t'], '{:+.1f}')}), "
                     f"halves {fmt(r['ic_first'], '{:+.3f}')} / {fmt(r['ic_second'], '{:+.3f}')}, "
                     f"adds to mix {fmt(r['mix_gain_first'], '{:+.4f}')} / {fmt(r['mix_gain_second'], '{:+.4f}')}, "
                     f"since {r['from']}")
    return "\n".join(lines)
