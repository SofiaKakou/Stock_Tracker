"""Faster dip-spotting for the crypto account, tested before it goes live. A study only: nothing trades.

Live now: every 15 minutes, a coin whose last 15-minute bar closed 3%+ below its price an hour earlier is
bought and sold a day later. Two ideas to find more dips:
- look every 5 minutes instead of every 15 (a dip that bounces within the quarter hour is missed now)
- count fast falls too: 3%+ in 15 or 30 minutes, not only over a full hour
And the user's question: with $500 instead of $100, could the account make 30-40 trades a day?

Tested on 5-minute bars of every coin Alpaca trades (except the stable ones) since START: at each bar's
close, a rule that fires buys at the next bar's open and sells at the open a day later (one position per
coin at a time). Fees: our orders take the asking price, so 0.25% a side (0.50% a round trip); 0.30% for
orders that wait in the book. Luck check: the same number of trades at random times in the same coins.
Then the money: each account can hold only so many coins at once ($16 a trade), so a busy hour's extra
dips are skipped. How many trades a day each account really takes, and what they'd make, is counted too.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from trend_bot import crypto

START = "2024-01-01"
TIMEFRAME = "5Min"
BAR = pd.Timedelta(minutes=5)
FEES = {"taker": 0.0050, "maker": 0.0030}   # round trip: what our orders pay / orders that wait in the book
HOLDS = {"1 day": 288, "4 hours": 48}
DRAWS = 200
AMOUNT = 16.0
ACCOUNTS = {"$100 (5 coins at once, $16 each: now)": 5, "$500 (30 coins at once, $16 each)": 30}
# name: ([(window in 5-minute bars, fall)], only at the quarter hour?)
RULES = {
    "now: 3% in an hour, looked at every 15 min": ([(12, 0.03)], True),
    "3% in an hour, every 5 min": ([(12, 0.03)], False),
    "3% in 30 min, every 5 min": ([(6, 0.03)], False),
    "3% in 15 min, every 5 min": ([(3, 0.03)], False),
    "3% in 15, 30 or 60 min, every 5 min": ([(3, 0.03), (6, 0.03), (12, 0.03)], False),
    "2% in 15, 30 or 60 min, every 5 min": ([(3, 0.02), (6, 0.02), (12, 0.02)], False),
    "4% in 15, 30 or 60 min, every 5 min": ([(3, 0.04), (6, 0.04), (12, 0.04)], False),
}


def signal(t: np.ndarray, c: np.ndarray, rule: tuple) -> np.ndarray:
    """True on each bar whose close sets the rule off (a gap in the bars never counts as a fall)."""
    windows, quarter = rule
    out = np.zeros(len(c), bool)
    for w, fall in windows:
        if len(c) <= w:
            continue
        ok = np.zeros(len(c), bool)
        ok[w:] = (t[w:] - t[:-w]) == np.timedelta64(5 * w, "m")
        move = np.full(len(c), np.nan)
        move[w:] = c[w:] / c[:-w] - 1
        with np.errstate(invalid="ignore"):
            out |= ok & (move <= -fall)
    if quarter:
        ends = pd.DatetimeIndex(t + np.timedelta64(5, "m"))
        out &= (ends.minute % 15 == 0)
    return out


def trades(t: np.ndarray, o: np.ndarray, sig: np.ndarray, hold: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(entry times, exit times, returns): in at the next bar's open, out `hold` bars later; one at a time."""
    ins, outs, rets, free = [], [], [], 0
    for i in np.flatnonzero(sig):
        if i < free:
            continue
        a, b = i + 1, i + 1 + hold
        if b >= len(o):
            break
        ins.append(t[a]), outs.append(t[b]), rets.append(o[b] / o[a] - 1)
        free = b
    return np.array(ins, "datetime64[ns]"), np.array(outs, "datetime64[ns]"), np.array(rets, float)


def coin_results(g: pd.DataFrame, rng) -> dict:
    """{(rule, hold): (entries, exits, returns, random means)} for one coin's bars in time order."""
    t = pd.to_datetime(g["t"], utc=True).dt.tz_localize(None).to_numpy("datetime64[ns]")
    o, c = g["open"].to_numpy(float), g["close"].to_numpy(float)
    out = {}
    for name, rule in RULES.items():
        sig = signal(t, c, rule)
        for label, hold in HOLDS.items():
            ins, outs, rets = trades(t, o, sig, hold)
            rand = crypto.random_mean(g, len(rets), hold, rng) if len(rets) else np.zeros(DRAWS)
            out[(name, label)] = (ins, outs, rets, rand)
    return out


def capacity(ins: np.ndarray, outs: np.ndarray, rets: np.ndarray, slots: int) -> np.ndarray:
    """Which trades an account holding at most `slots` coins at once would take (first come, first served)."""
    order = np.argsort(ins, kind="stable")
    taken = np.zeros(len(ins), bool)
    busy: list = []
    for k in order:
        busy = [e for e in busy if e > ins[k]]
        if len(busy) < slots:
            busy.append(outs[k])
            taken[k] = True
    return taken


def evaluate(ins: np.ndarray, outs: np.ndarray, rets: np.ndarray, rand: float | np.ndarray, days: float,
             split: pd.Timestamp) -> dict:
    if not len(rets):
        return {"trades": 0}
    when = pd.DatetimeIndex(ins)
    month = when.strftime("%Y-%m")
    r = {"trades": len(rets), "per_day": len(rets) / days, "gross": float(rets.mean()), "hit": float((rets > 0).mean()),
         "luck": float((np.asarray(rand) >= rets.mean()).mean()), "random": float(np.mean(rand))}
    for fee, cost in FEES.items():
        net = rets - cost
        monthly = pd.Series(net).groupby(month).mean()
        early, late = net[when < split], net[when >= split]
        r[fee] = {"net": float(net.mean()), "early": float(early.mean()) if len(early) else np.nan,
                  "late": float(late.mean()) if len(late) else np.nan,
                  "t": float(monthly.mean() / monthly.std(ddof=1) * np.sqrt(len(monthly)))
                  if len(monthly) > 2 and monthly.std() > 0 else 0.0}
    r["accounts"] = {}
    for acct, slots in ACCOUNTS.items():
        take = capacity(ins, outs, rets, slots)
        net = rets[take] - FEES["taker"]
        r["accounts"][acct] = {"per_day": take.sum() / days, "dollars_day": float(net.sum() * AMOUNT / days),
                               "net": float(net.mean()) if len(net) else np.nan}
    return r


def passes(r: dict, fee: str = "taker") -> bool:
    f = r.get(fee) or {}
    return bool(r.get("trades")) and f["early"] > 0 and f["late"] > 0 and f["t"] > 2 and r["luck"] < 0.05


def study(frames, log=print, seed: int = 0) -> tuple[dict, str, str, int]:
    """frames: (symbol, bars) one coin at a time (5-minute bars in time order). Returns (results, first, last, coins)."""
    rng = np.random.default_rng(seed)
    parts: dict = {}
    first, last, n = None, None, 0
    for sym, g in frames:
        if len(g) < 400:
            continue
        n += 1
        t0, t1 = str(g["t"].iloc[0])[:10], str(g["t"].iloc[-1])[:10]
        first, last = min(first or t0, t0), max(last or t1, t1)
        for k, v in coin_results(g.reset_index(drop=True), rng).items():
            parts.setdefault(k, []).append(v)
        log(f"[crypto-fast] {sym}: {len(g):,} bars")
    if not parts:
        return {}, "", "", 0
    days = max((pd.Timestamp(last) - pd.Timestamp(first)).days, 1)
    split = pd.Timestamp(first) + (pd.Timestamp(last) - pd.Timestamp(first)) / 2
    out = {}
    for k, vs in parts.items():
        ins = np.concatenate([v[0] for v in vs])
        outs = np.concatenate([v[1] for v in vs])
        rets = np.concatenate([v[2] for v in vs])
        rand = sum(v[3] * len(v[2]) for v in vs) / max(len(rets), 1)
        out[k] = evaluate(ins, outs, rets, rand, days, split)
    return out, first, last, n


def text(results: dict, first: str, last: str, coins: int) -> str:
    lines = [f"Faster dip-spotting, crypto: 5-minute bars {first} to {last}, {coins} coins (Alpaca), bought at the "
             f"next bar's open after the rule fires, one at a time per coin. Fees per round trip: "
             f"{FEES['taker']:.2%} for our orders (they take the asking price), {FEES['maker']:.2%} for orders that "
             "wait in the book. Halves: the first and second half of the period. Luck check: the same number of "
             "trades at random times in the same coins.", ""]
    for (name, held), r in results.items():
        lines.append(f"== {name}, sold {held} later ==")
        if not r.get("trades"):
            lines += ["  no trades", ""]
            continue
        tk, mk = r["taker"], r["maker"]
        lines += [f"  {r['trades']:,} trades ({r['per_day']:.1f} a day over all coins); per trade {r['gross']:+.3%} "
                  f"before fees (random times: {r['random']:+.3%}); right {r['hit']:.0%} before fees",
                  f"  after our fees: {tk['net']:+.3%} a trade (first half {tk['early']:+.3%}, second half "
                  f"{tk['late']:+.3%}; t = {tk['t']:.1f}); with waiting orders {mk['net']:+.3%}",
                  f"  luck check: random times did as well {r['luck']:.0%} of the time"]
        for acct, a in r["accounts"].items():
            lines.append(f"  {acct}: takes {a['per_day']:.1f} a day, {a['net']:+.2%} each after fees, "
                         f"about ${a['dollars_day']:+.2f} a day")
        lines += [f"  → {'PASSES' if passes(r) else 'does not pass'} (after our fees, both halves, t > 2, luck < 5%)", ""]
    passed = [f"{n}, {h}" for (n, h), r in results.items() if passes(r)]
    lines.append("Passed: " + ("; ".join(passed) if passed else "none") + ".")
    return "\n".join(lines)


def run(data, symbols: list[str], log=print, start: str = START) -> str:
    log(f"[crypto-fast] 5-minute bars for {len(symbols)} coins since {start}, one coin at a time...")

    def frames():
        for s in symbols:
            try:
                g = crypto.fetch(data, [s], start=start, timeframe=TIMEFRAME)
            except Exception as e:  # noqa: BLE001 - one coin's prices failing doesn't stop the study
                log(f"[crypto-fast] {s}: no bars ({str(e)[:80]})")
                continue
            yield s, g.sort_values("t")

    results, first, last, n = study(frames(), log)
    if not results:
        return "No crypto bars came back from Alpaca."
    return text(results, first, last, n)
