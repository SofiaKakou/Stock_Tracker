"""Fast trading in the day's wildest stocks (scalping), tested before any money goes in. A study only.

The idea, as fast traders describe it: each morning find the stocks that are moving like crazy (gapped up
10%+ at the open, enough money trading in them to get in and out), then all day buy every time one breaks
above its high of the last few minutes while above its average price of the day (VWAP), and get out fast:
a small profit target, a tight stop, or after half an hour at most. Many trades a day.

Tested on 1-minute bars (Alpaca) of each day's 10 biggest gap-ups since START (from our daily prices: open
at least 10% above yesterday's close, yesterday's close $2+, $2M+ a day traded over the last 20 days; a
jump over 100% is usually a split or a data error and is skipped). Rules, on each 1-minute bar's close
from 9:35 to 15:30:
- signal: the close is above the highest high of the 5 bars before it, and above the day's VWAP
- in at the next bar's open; out at the profit target or the stop (if both are touched in the same minute,
  the stop is counted: the worse case), else after `hold` minutes at that bar's close, and by 15:55
- one position per stock at a time; after an exit, the next signal can come right away
Costs: the 0.17% a side our day-trading account measured, and 0.30% a side (wild stocks have wider gaps
between buyers and sellers) as a harder check. Luck check: the same number of trades at random minutes in
the same stocks on the same days, with the same exits.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from trend_bot import intraday

START = "2024-07-01"
TOP = 10                     # gap-ups a day
GAP_MIN, GAP_MAX = 0.10, 1.00
MIN_PRICE, MIN_DOLLAR_VOL = 2.0, 2e6
LOOKBACK = 5                 # minutes whose high the close must clear
FIRST, LAST_ENTRY, FLAT = dt.time(9, 35), dt.time(15, 30), dt.time(15, 55)
COSTS = {"measured": 0.0017, "wide": 0.0030}   # a side
POSITION = 5000.0            # dollars a trade (about 1/20 of the day-trading account)
DRAWS = 30
# name: (profit target, stop, longest hold in minutes)
VARIANTS = {"+1% target, -0.5% stop, 30 min": (0.01, 0.005, 30),
            "+2% target, -1% stop, 30 min": (0.02, 0.01, 30),
            "+3% target, -1% stop, 30 min": (0.03, 0.01, 30),
            "+3% target, -1.5% stop, 60 min": (0.03, 0.015, 60),
            "+5% target, -2% stop, 60 min": (0.05, 0.02, 60)}


def candidates(con, start: str = START, top: int = TOP) -> pd.DataFrame:
    """[date, ticker, gap]: each day's biggest gap-ups that pass the filters (known at the open)."""
    q = """
    WITH p AS (
      SELECT ticker, date, open, close, volume,
             LAG(close) OVER w AS prev,
             AVG(close * volume) OVER (PARTITION BY ticker ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS dvol
      FROM prices WHERE date >= date(?, '-45 days')
      WINDOW w AS (PARTITION BY ticker ORDER BY date))
    SELECT date, ticker, open / prev - 1 AS gap FROM p
    WHERE date >= ? AND prev >= ? AND dvol >= ? AND open > 0 AND open / prev - 1 BETWEEN ? AND ?"""
    df = pd.read_sql_query(q, con, params=(start, start, MIN_PRICE, MIN_DOLLAR_VOL, GAP_MIN, GAP_MAX))
    df = df.sort_values(["date", "gap"], ascending=[True, False])
    return df.groupby("date").head(top).reset_index(drop=True)


def day_bars(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """{ticker: regular-hours 1-minute bars with New York time and the day's VWAP}."""
    out = {}
    if df.empty:
        return out
    t = pd.to_datetime(df["t"], utc=True).dt.tz_convert(intraday.NY)
    df = df.assign(ny=t)
    mins = t.dt.hour * 60 + t.dt.minute
    df = df[(mins >= 9 * 60 + 30) & (mins < 16 * 60)]
    for sym, g in df.groupby("symbol"):
        g = g.sort_values("ny").reset_index(drop=True)
        typical = (g["high"] + g["low"] + g["close"]) / 3
        vol = g["volume"].clip(lower=0)
        g["vwap"] = (typical * vol).cumsum() / vol.cumsum().replace(0, np.nan)
        out[sym] = g
    return out


def signals(g: pd.DataFrame) -> np.ndarray:
    h, c, v = g["high"].to_numpy(float), g["close"].to_numpy(float), g["vwap"].to_numpy(float)
    prior = pd.Series(h).rolling(LOOKBACK).max().shift(1).to_numpy()
    tod = g["ny"].dt.time.to_numpy()
    window = np.array([FIRST <= x <= LAST_ENTRY for x in tod])
    with np.errstate(invalid="ignore"):
        return window & (c > prior) & (c > v)


def exit_from(g_arrays, a: int, target: float, stop: float, hold: int) -> tuple[int, float]:
    """(exit bar, gross return) for a buy at bar a's open."""
    o, h, lo, c, flat = g_arrays
    entry = o[a]
    up, down = entry * (1 + target), entry * (1 - stop)
    last = min(a + hold - 1, flat)
    for k in range(a, last + 1):
        if k > a and o[k] <= down:
            return k, o[k] / entry - 1                   # opened below the stop: fills there
        if lo[k] <= down:
            return k, -stop
        if k > a and o[k] >= up:
            return k, o[k] / entry - 1
        if h[k] >= up:
            return k, target
    return last, c[last] / entry - 1


def day_trades(g: pd.DataFrame, variant: tuple, rng=None) -> tuple[list, np.ndarray, np.ndarray]:
    """(entry times, gross returns) for one stock on one day; plus random-entry means if rng is given."""
    target, stop, hold = variant
    o, h, lo, c = (g[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    tod = g["ny"].dt.time.to_numpy()
    flat_idx = [i for i, x in enumerate(tod) if x <= FLAT]
    if not flat_idx:
        return [], np.zeros(0), np.zeros(0)
    flat = flat_idx[-1]
    arrays = (o, h, lo, c, flat)
    sig = signals(g)
    times, rets, free = [], [], 0
    for i in np.flatnonzero(sig):
        if i < free or i + 1 > flat:
            continue
        k, r = exit_from(arrays, i + 1, target, stop, hold)
        times.append(g["ny"].iloc[i + 1])
        rets.append(r)
        free = k + 1
    rand = np.zeros(0)
    if rng is not None and rets:
        ok = np.flatnonzero([FIRST <= x <= LAST_ENTRY for x in tod])
        ok = ok[ok + 1 <= flat]
        if len(ok):
            picks = rng.choice(ok, size=(DRAWS, len(rets)))
            rand = np.array([np.mean([exit_from(arrays, p + 1, target, stop, hold)[1] for p in row]) for row in picks])
    return times, np.array(rets, float), rand


def evaluate(times: list, rets: np.ndarray, rand_sum: np.ndarray, n_rand: int, days: int, split: str) -> dict:
    if not len(rets):
        return {"trades": 0}
    when = pd.DatetimeIndex(times)
    month = when.strftime("%Y-%m")
    rand = rand_sum / max(n_rand, 1)
    r = {"trades": len(rets), "per_day": len(rets) / max(days, 1), "gross": float(rets.mean()),
         "hit": float((rets > 0).mean()), "random": float(rand.mean()) if len(rand) else np.nan,
         "luck": float((rand >= rets.mean()).mean()) if len(rand) else np.nan}
    early_mask = when.strftime("%Y-%m-%d") < split
    for name, side in COSTS.items():
        net = rets - 2 * side
        monthly = pd.Series(net).groupby(month).mean()
        r[name] = {"net": float(net.mean()), "early": float(net[early_mask].mean()) if early_mask.any() else np.nan,
                   "late": float(net[~early_mask].mean()) if (~early_mask).any() else np.nan,
                   "t": float(monthly.mean() / monthly.std(ddof=1) * np.sqrt(len(monthly)))
                   if len(monthly) > 2 and monthly.std() > 0 else 0.0,
                   "dollars_day": float(net.sum() * POSITION / max(days, 1))}
    return r


def passes(r: dict, cost: str = "measured") -> bool:
    f = r.get(cost) or {}
    return bool(r.get("trades")) and f["early"] > 0 and f["late"] > 0 and f["t"] > 2 and r["luck"] < 0.05


def study(days_bars, log=print, seed: int = 0) -> tuple[dict, str, str, int]:
    """days_bars: (date, {ticker: bars}) in date order. Returns ({variant: result}, first, last, days)."""
    rng = np.random.default_rng(seed)
    acc = {v: {"times": [], "rets": [], "rand": np.zeros(DRAWS), "n": 0} for v in VARIANTS}
    dates = []
    for date, bars in days_bars:
        if not bars:
            continue
        dates.append(date)
        for name, variant in VARIANTS.items():
            for sym, g in bars.items():
                times, rets, rand = day_trades(g, variant, rng)
                if len(rets):
                    a = acc[name]
                    a["times"] += times
                    a["rets"].append(rets)
                    if len(rand):
                        a["rand"] += rand * len(rets)
                        a["n"] += len(rets)
        if len(dates) % 50 == 0:
            log(f"[scalp] {len(dates)} days done ({date})")
    if not dates:
        return {}, "", "", 0
    split = dates[len(dates) // 2]
    out = {}
    for name, a in acc.items():
        rets = np.concatenate(a["rets"]) if a["rets"] else np.zeros(0)
        out[name] = evaluate(a["times"], rets, a["rand"], a["n"], len(dates), split)
    return out, dates[0], dates[-1], len(dates)


def text(results: dict, first: str, last: str, days: int, stocks: int) -> str:
    lines = [f"Scalping the day's wildest stocks: 1-minute bars {first} to {last}, {days} trading days, the {TOP} "
             f"biggest gap-ups each day ({stocks:,} stock-days; opened {GAP_MIN:.0%}+ above yesterday's close, "
             f"${MIN_PRICE:.0f}+, ${MIN_DOLLAR_VOL / 1e6:.0f}M+ a day traded). Buy when a 1-minute close clears the "
             f"last {LOOKBACK} minutes' high above the day's VWAP; out at the target, the stop or the time limit. "
             f"Costs a side: {COSTS['measured']:.2%} (what our account measured), {COSTS['wide']:.2%} (a harder "
             f"check). Dollars at ${POSITION:,.0f} a trade.", ""]
    for name, r in results.items():
        lines.append(f"== {name} ==")
        if not r.get("trades"):
            lines += ["  no trades", ""]
            continue
        m, w = r["measured"], r["wide"]
        lines += [f"  {r['trades']:,} trades ({r['per_day']:.1f} a day); per trade {r['gross']:+.3%} before costs "
                  f"(random minutes: {r['random']:+.3%}); right {r['hit']:.0%}",
                  f"  after {COSTS['measured']:.2%} a side: {m['net']:+.3%} a trade (first half {m['early']:+.3%}, "
                  f"second half {m['late']:+.3%}; t = {m['t']:.1f}), about ${m['dollars_day']:+,.0f} a day",
                  f"  after {COSTS['wide']:.2%} a side: {w['net']:+.3%} a trade, about ${w['dollars_day']:+,.0f} a day",
                  f"  luck check: random minutes did as well {r['luck']:.0%} of the time",
                  f"  → {'PASSES' if passes(r) else 'does not pass'} at our measured costs"
                  + (" and at the wider ones" if passes(r, 'wide') else ""), ""]
    passed = [n for n, r in results.items() if passes(r)]
    lines.append("Passed: " + ("; ".join(passed) if passed else "none") + ".")
    return "\n".join(lines)


def run(con, data: "intraday.Data", log=print, start: str = START) -> str:
    cands = candidates(con, start)
    if cands.empty:
        return "No gap-ups found in the daily prices."
    log(f"[scalp] {len(cands):,} stock-days on {cands['date'].nunique()} days since {start}; 1-minute bars...")

    def days():
        for date, g in cands.groupby("date", sort=True):
            open_ = dt.datetime.combine(dt.date.fromisoformat(date), dt.time(9, 30), ZoneInfo(intraday.NY))
            s = open_.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            e = (open_ + dt.timedelta(hours=6, minutes=30)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                bars = data.bars(list(g["ticker"]), s, e, timeframe="1Min")
            except Exception as ex:  # noqa: BLE001 - one day's prices failing doesn't stop the study
                log(f"[scalp] {date}: no bars ({str(ex)[:80]})")
                continue
            yield date, day_bars(bars)

    results, first, last, n = study(days(), log)
    if not results:
        return "No 1-minute bars came back from Alpaca."
    return text(results, first, last, n, len(cands))
