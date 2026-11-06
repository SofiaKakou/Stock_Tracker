"""The day account's own trades, looked at again (asked for the Nov 6 review). A study only: nothing trades.

From the trade log the account keeps (daytrade.json), per pattern: trades, average result per trade,
dollars, and a luck check (t: the average day's result over its spread; about 2 or more is unlikely to be
luck). Then two questions about our real trades:
- Breakouts: would only the day's 5 or 10 strongest signals (by the bot's own signal strength) have done
  better than all of them?
- Profit targets: with 1-minute prices from Alpaca, would selling at +1%, +2% or +3% (once the price
  touched it) have beaten what really happened? A trade that never touched its target keeps its real exit
  (the stop, or the close). Approximate: the safety stop's exact minute isn't in the log, so a target
  touched after a stop fired would be counted too generously; the results say how many trades changed.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")
NAMES = {"gap": "Fade big opening gaps", "breakout": "Breakout from the first half hour",
         "momentum": "Market's last half hour follows its first", "gapup": "Short the big gap-ups",
         "loser": "Short yesterday's big losers"}
TARGETS = (0.01, 0.02, 0.03)
TOPS = (5, 10)


def trades(state: dict) -> pd.DataFrame:
    """Finished trades from the log: one row each, with its day, pattern, side, signal, entry, exit, ret, pnl."""
    rows = [r for r in state.get("log", []) if r.get("ret") is not None and r.get("entry")]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ret"] = df["ret"].astype(float)
    df["pnl"] = df["pnl"].astype(float)
    return df


def luck(df: pd.DataFrame) -> float:
    """t of the average day's result per trade (a pattern's days are its independent bets)."""
    daily = df.groupby("date")["ret"].mean()
    if len(daily) < 3 or daily.std(ddof=1) == 0:
        return float("nan")
    return float(daily.mean() / daily.std(ddof=1) * np.sqrt(len(daily)))


def by_pattern(df: pd.DataFrame) -> list[str]:
    lines = ["== Each pattern, live (after real costs: these are filled prices) =="]
    for p, g in df.groupby("pattern"):
        lines.append(f"  {NAMES.get(p, p)}: {len(g)} trades on {g['date'].nunique()} days, "
                     f"{g['ret'].mean():+.3%} a trade, ${g['pnl'].sum():+,.0f}, right {(g['ret'] > 0).mean():.0%}, "
                     f"t = {luck(g):.1f}")
    return lines


def top_breakouts(df: pd.DataFrame) -> list[str]:
    b = df[df["pattern"] == "breakout"].copy()
    if b.empty:
        return []
    b["strength"] = pd.to_numeric(b.get("signal"), errors="coerce").abs()
    lines = ["", "== Breakouts: all of them vs only the day's strongest signals =="]
    known = b["strength"].notna().mean()
    lines.append(f"  all: {len(b)} trades, {b['ret'].mean():+.3%} a trade, ${b['pnl'].sum():+,.0f}"
                 f"  (signal strength known for {known:.0%} of them)")
    for n in TOPS:
        top = b.sort_values("strength", ascending=False, na_position="last").groupby("date").head(n)
        # The same money spread over fewer trades: each would have been len(all)/len(top) times bigger.
        scale = len(b) / max(len(top), 1)
        lines.append(f"  top {n} a day: {len(top)} trades, {top['ret'].mean():+.3%} a trade, "
                     f"${top['pnl'].sum():+,.0f} as traded (about ${top['pnl'].sum() * scale:+,.0f} with the "
                     f"same money in fewer, bigger trades), t = {luck(top):.1f}")
    return lines


def with_target(entry: float, side: int, live_exit: float, highs, lows, target: float) -> float:
    """The trade's exit price if it had been sold once it gained `target` (else its real exit)."""
    level = entry * (1 + side * target)
    hit = (np.asarray(highs) >= level) if side > 0 else (np.asarray(lows) <= level)
    return level if hit.any() else live_exit


def targets(df: pd.DataFrame, data, log=print, which: str = "all patterns") -> list[str]:
    lines = ["", f"== Selling at a profit target instead, {which} (1-minute prices from the entry to the close) =="]
    out = {t: [] for t in TARGETS}
    base, changed, missing = [], {t: 0 for t in TARGETS}, 0
    for day, g in df.groupby("date"):
        d = dt.date.fromisoformat(day)
        start = dt.datetime.combine(d, dt.time(9, 30), NY).astimezone(dt.timezone.utc)
        end = dt.datetime.combine(d, dt.time(16, 0), NY).astimezone(dt.timezone.utc)
        try:
            bars = data.bars(sorted(g["symbol"].unique()), start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             end.strftime("%Y-%m-%dT%H:%M:%SZ"), timeframe="1Min")
        except Exception as e:  # noqa: BLE001
            log(f"[day-review] {day}: no 1-minute prices ({str(e)[:80]})")
            missing += len(g)
            continue
        bars["t"] = pd.to_datetime(bars["t"], utc=True).dt.tz_convert(NY)
        for r in g.itertuples():
            hh, mm = (int(x) for x in str(r.time or "09:31")[:5].split(":"))
            after = bars[(bars["symbol"] == r.symbol) & (bars["t"] > pd.Timestamp(dt.datetime.combine(d, dt.time(hh, mm), NY)))]
            if after.empty:
                missing += 1
                continue
            side = 1 if r.side > 0 else -1
            base.append(r.ret)
            for t in TARGETS:
                px = with_target(r.entry, side, r.exit, after["high"], after["low"], t)
                if px != r.exit:
                    changed[t] += 1
                out[t].append(side * (px / r.entry - 1))
        log(f"[day-review] {day}: {len(g)} trades checked")
    if not base:
        return lines + ["  no 1-minute prices came back"]
    lines.append(f"  as traded: {len(base)} trades, {np.mean(base):+.3%} a trade"
                 + (f" ({missing} trades without prices left out)" if missing else ""))
    for t in TARGETS:
        lines.append(f"  sell at +{t:.0%}: {np.mean(out[t]):+.3%} a trade ({changed[t]} trades would have "
                     f"sold early; {np.mean(out[t]) - np.mean(base):+.3%} vs as traded)")
    return lines


def run(data, state_path: str = "daytrade.json", log=print) -> str:
    try:
        state = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        return "No day-trading results found (daytrade.json)."
    df = trades(state)
    if df.empty:
        return "No finished day trades in the log yet."
    lines = [f"The day account's own trades, {df['date'].min()} to {df['date'].max()}: {len(df)} finished trades.",
             "t is a luck check: about 2 or more is unlikely to be luck; a few weeks of day trades rarely get there.",
             ""]
    lines += by_pattern(df)
    lines += top_breakouts(df)
    if data is not None:
        lines += targets(df, data, log)
        for p in ("gap", "breakout"):
            if (df["pattern"] == p).any():
                lines += targets(df[df["pattern"] == p], data, log, NAMES[p].lower())
    else:
        lines.append("\n(No Alpaca keys: the profit-target check was skipped.)")
    return "\n".join(lines)
