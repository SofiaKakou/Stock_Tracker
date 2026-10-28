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


# --- Orders that wait in the book (0.15% a side instead of 0.25%) ------------------------------------
# A waiting buy only fills if the price comes down to it; a dip that bounces straight away is missed.
# Checked as the live account would run (every 15 minutes): when the rule fires, a buy at the last price
# (or a little below) waits up to `wait` minutes; it fills only if a later bar trades below it. A day
# after the fill, a sell at that moment's price waits 15 minutes; if the price doesn't trade above it,
# the coin is sold at the market (0.25%) at the end of the wait.
MAKER, TAKER = 0.0015, 0.0025
MAKER_RULES = {"3% in an hour (now)": ([(12, 0.03)], True), "4% in an hour": ([(12, 0.04)], True)}
OFFSETS = (0.0, 0.0025, 0.005)
WAITS = {"15 min": 3, "30 min": 6}
EXIT_WAIT = 3
TICK = 0.0005           # the price must trade this far through a waiting order to count as filled


def maker_trades(t, o, h, lo, c, sig, hold: int, offset: float, wait: int) -> tuple:
    """(entry times, net returns after fees, fired, filled, missed dips' returns) for one coin."""
    ins, nets, missed, fired, free = [], [], [], 0, 0
    for i in np.flatnonzero(sig):
        if i < free:
            continue
        fired += 1
        limit = c[i] * (1 - offset)
        j = next((k for k in range(i + 1, min(i + 1 + wait, len(c))) if lo[k] < limit * (1 - TICK)), None)
        if j is None:                       # never came down to it: no trade (what it would have made, for the record)
            if i + 1 + hold < len(o):
                missed.append(o[i + 1 + hold] / o[i + 1] - 1)
            free = i + 1
            continue
        b = j + 1 + hold
        if b + EXIT_WAIT >= len(o):
            break
        ask = o[b]
        if any(h[k] > ask * (1 + TICK) for k in range(b, b + EXIT_WAIT)):
            out, fee_out = ask, MAKER
        else:
            out, fee_out = c[b + EXIT_WAIT - 1], TAKER
        ins.append(t[j])
        nets.append(out / limit - 1 - MAKER - fee_out)
        free = b
    return np.array(ins, "datetime64[ns]"), np.array(nets, float), fired, len(nets), np.array(missed, float)


def maker_study(frames, log=print) -> tuple[dict, str, str, int]:
    parts: dict = {}
    first, last, n = None, None, 0
    for sym, g in frames:
        if len(g) < 400:
            continue
        n += 1
        g = g.reset_index(drop=True)
        t0, t1 = str(g["t"].iloc[0])[:10], str(g["t"].iloc[-1])[:10]
        first, last = min(first or t0, t0), max(last or t1, t1)
        t = pd.to_datetime(g["t"], utc=True).dt.tz_localize(None).to_numpy("datetime64[ns]")
        o, h, lo, c = (g[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        for name, rule in MAKER_RULES.items():
            sig = signal(t, c, rule)
            ins, outs, rets = trades(t, o, sig, HOLDS["1 day"])          # the same rule with our orders today
            parts.setdefault((name, "taking the price now (today)"), []).append(
                (ins, rets - FEES["taker"], len(rets), len(rets), np.array([])))
            for off in OFFSETS:
                for wl, w in WAITS.items():
                    label = f"waiting {wl} at {off:.2%} below the last price"
                    parts.setdefault((name, label), []).append(maker_trades(t, o, h, lo, c, sig, HOLDS["1 day"], off, w))
        log(f"[crypto-maker] {sym}: {len(g):,} bars")
    if not parts:
        return {}, "", "", 0
    days = max((pd.Timestamp(last) - pd.Timestamp(first)).days, 1)
    split = pd.Timestamp(first) + (pd.Timestamp(last) - pd.Timestamp(first)) / 2
    out = {}
    for k, vs in parts.items():
        ins = np.concatenate([v[0] for v in vs])
        net = np.concatenate([v[1] for v in vs])
        fired, filled = sum(v[2] for v in vs), sum(v[3] for v in vs)
        missed = np.concatenate([v[4] for v in vs])
        if not len(net):
            out[k] = {"trades": 0}
            continue
        when = pd.DatetimeIndex(ins)
        monthly = pd.Series(net).groupby(when.strftime("%Y-%m")).mean()
        early, late = net[when < split], net[when >= split]
        out[k] = {"trades": len(net), "per_day": len(net) / days, "fill": filled / max(fired, 1),
                  "net": float(net.mean()), "early": float(early.mean()) if len(early) else np.nan,
                  "late": float(late.mean()) if len(late) else np.nan, "hit": float((net > 0).mean()),
                  "t": float(monthly.mean() / monthly.std(ddof=1) * np.sqrt(len(monthly)))
                  if len(monthly) > 2 and monthly.std() > 0 else 0.0,
                  "missed": float(missed.mean()) if len(missed) else None, "dollars_day": float(net.sum() * AMOUNT / days)}
    return out, first, last, n


def maker_passes(r: dict) -> bool:
    return bool(r.get("trades")) and r["early"] > 0 and r["late"] > 0 and r["t"] > 2


def maker_text(results: dict, first: str, last: str, coins: int) -> str:
    lines = [f"Crypto dips with orders that wait in the book: 5-minute bars {first} to {last}, {coins} coins, sold a "
             f"day later. Fees: {MAKER:.2%} a side for a waiting order that fills, {TAKER:.2%} for taking the price. "
             "A waiting buy fills only if the price later trades below it; if it doesn't within the wait, the dip is "
             "missed. Results are after fees, per trade; every coin held at once (no limit on how many).", ""]
    for name in MAKER_RULES:
        lines.append(f"== {name}, looked at every 15 minutes ==")
        for (n, label), r in results.items():
            if n != name:
                continue
            if not r.get("trades"):
                lines.append(f"  {label}: no trades")
                continue
            miss = f"; the missed dips would have made {r['missed']:+.2%} before fees" if r.get("missed") is not None else ""
            lines.append(f"  {label}: {r['trades']:,} trades ({r['per_day']:.1f} a day, {r['fill']:.0%} of dips filled{miss})")
            lines.append(f"    {r['net']:+.3%} a trade (first half {r['early']:+.3%}, second half {r['late']:+.3%}; "
                         f"t = {r['t']:.1f}), right {r['hit']:.0%}, about ${r['dollars_day']:+.2f} a day at ${AMOUNT:.0f} a trade"
                         f" → {'PASSES' if maker_passes(r) else 'does not pass'}")
        lines.append("")
    passed = [f"{n}: {lb}" for (n, lb), r in results.items() if maker_passes(r)]
    lines.append("Passed (both halves after fees, t > 2): " + ("; ".join(passed) if passed else "none") + ".")
    return "\n".join(lines)


def run_maker(data, symbols: list[str], log=print, start: str = START) -> str:
    log(f"[crypto-maker] 5-minute bars for {len(symbols)} coins since {start}, one coin at a time...")

    def frames():
        for s in symbols:
            try:
                g = crypto.fetch(data, [s], start=start, timeframe=TIMEFRAME)
            except Exception as e:  # noqa: BLE001
                log(f"[crypto-maker] {s}: no bars ({str(e)[:80]})")
                continue
            yield s, g.sort_values("t")

    results, first, last, n = maker_study(frames(), log)
    return maker_text(results, first, last, n) if results else "No crypto bars came back from Alpaca."
