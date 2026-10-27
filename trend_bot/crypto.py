"""Crypto day trading, tested before any money goes in. A study only: nothing here trades.

Why crypto: no US day-trading limit (stocks under $25,000: 3 day trades in 5 days), it trades all day
and night, and tiny fractional orders are normal, so a $100 account could trade many times a day.
What it costs: Alpaca charges a fee on every crypto trade (0.15% a side for a limit order that waits
in the book, 0.25% for one that takes the price now), much more than stocks, and it can't be shorted
there. So only buying is tested, and each idea has to beat a 0.30%-0.50% round trip.

Tested on 15-minute bars of the biggest coins since 2021 (Alpaca's crypto data), one position per coin
at a time, in at the next bar's open after the signal, out at a bar's open a fixed time later:
- momentum: the coin rose 2%+ in the last hour: does it keep rising?
- dip: it fell 2%+ (or 5%+) in the last hour: does it bounce?
- breakout: it closed above its highest price of the last 24 hours: does it keep going?
Each held 15 minutes, 1 hour, 4 hours or a day. The luck check here is different from the stock
studies (a coin can't be shorted, so flipping directions doesn't apply): the same number of trades at
random times in the same coins, held as long; crypto rose a lot since 2021, so 'buy at any time' is
the bar to beat, not zero.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

URL = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "XRP/USD", "LTC/USD", "LINK/USD", "AVAX/USD"]
START = "2021-01-01"
TIMEFRAME = "15Min"
BARS_PER_HOUR = 4
FEES = {"limit": 0.0030, "market": 0.0050}     # round trip: 0.15% / 0.25% a side
HOLDS = {"15 minutes": 1, "1 hour": 4, "4 hours": 16, "1 day": 96}
SPLIT = "2023-07-01"                            # the two halves of the data, judged separately
DRAWS = 200


def fetch(data, symbols: list[str] = SYMBOLS, start: str = START, end: str | None = None,
          timeframe: str = TIMEFRAME, pause: float = 0.3) -> pd.DataFrame:
    """[symbol, t, open, high, low, close, volume] from Alpaca's crypto bars, following every page."""
    import time

    end = end or pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    rows, token = [], None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": f"{start}T00:00:00Z",
                  "end": end, "limit": 10_000}
        if token:
            params["page_token"] = token
        resp = data.http.get(URL, params=params, timeout=60)
        if resp.status_code == 429:
            time.sleep(30)
            continue
        resp.raise_for_status()
        body = resp.json()
        for sym, bars in (body.get("bars") or {}).items():
            rows += [(sym, b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in bars]
        token = body.get("next_page_token")
        if not token:
            break
        time.sleep(pause)
    return pd.DataFrame(rows, columns=["symbol", "t", "open", "high", "low", "close", "volume"])


def signals(g: pd.DataFrame) -> dict[str, np.ndarray]:
    """Per pattern, a True on each bar (of one coin, in time order) whose close sets it off."""
    c, h = g["close"].to_numpy(float), g["high"].to_numpy(float)
    hour = np.full(len(c), np.nan)
    hour[BARS_PER_HOUR:] = c[BARS_PER_HOUR:] / c[:-BARS_PER_HOUR] - 1
    day_high = pd.Series(h).rolling(24 * BARS_PER_HOUR).max().shift(1).to_numpy()
    with np.errstate(invalid="ignore"):
        return {"momentum": hour >= 0.02, "dip": hour <= -0.02, "big dip": hour <= -0.05,
                "breakout": c > day_high}


def trades(g: pd.DataFrame, sig: np.ndarray, hold: int) -> pd.DataFrame:
    """[t, ret]: in at the next bar's open, out at the open `hold` bars later; one position at a time."""
    o, t = g["open"].to_numpy(float), g["t"].to_numpy()
    rows, free = [], 0
    for i in np.flatnonzero(sig):
        if i < free:
            continue
        a, b = i + 1, i + 1 + hold
        if b >= len(o):
            break
        rows.append((t[a], o[b] / o[a] - 1))
        free = b
    return pd.DataFrame(rows, columns=["t", "ret"])


def random_mean(g: pd.DataFrame, n: int, hold: int, rng) -> np.ndarray:
    """DRAWS averages of n trades at random times in this coin, held as long (gross)."""
    o = g["open"].to_numpy(float)
    if n == 0 or len(o) <= hold + 2:
        return np.zeros(DRAWS)
    idx = rng.integers(1, len(o) - hold, size=(DRAWS, n))
    return (o[idx + hold] / o[idx] - 1).mean(axis=1)


def evaluate(tr: pd.DataFrame, rand: np.ndarray, days: float) -> dict:
    if tr.empty:
        return {"trades": 0}
    gross = tr["ret"].to_numpy(float)
    when = pd.to_datetime(tr["t"], utc=True)
    month = when.dt.strftime("%Y-%m").to_numpy()
    out = {"trades": len(gross), "per_day": len(gross) / days, "gross": float(gross.mean()),
           "hit": float((gross > 0).mean()), "luck": float((rand >= gross.mean()).mean()),
           "random": float(rand.mean())}
    for fee, cost in FEES.items():
        net = gross - cost
        monthly = pd.Series(net).groupby(month).mean()
        early = net[(when < pd.Timestamp(SPLIT, tz="UTC")).to_numpy()]
        late = net[(when >= pd.Timestamp(SPLIT, tz="UTC")).to_numpy()]
        t = float(monthly.mean() / monthly.std(ddof=1) * np.sqrt(len(monthly))) if len(monthly) > 2 and monthly.std() > 0 else 0.0
        out[fee] = {"net": float(net.mean()), "early": float(early.mean()) if len(early) else np.nan,
                    "late": float(late.mean()) if len(late) else np.nan, "t": t}
    return out


def passes(r: dict, fee: str = "limit") -> bool:
    f = r.get(fee) or {}
    return bool(r.get("trades")) and f["early"] > 0 and f["late"] > 0 and f["t"] > 2 and r["luck"] < 0.05


def study(df: pd.DataFrame, seed: int = 0) -> tuple[dict[str, dict], dict[str, float]]:
    """{'<pattern>, held <time>': result over all coins}, and each coin's buy-and-hold over the period."""
    rng = np.random.default_rng(seed)
    df = df.sort_values(["symbol", "t"]).reset_index(drop=True)
    span = (pd.to_datetime(df["t"], utc=True).max() - pd.to_datetime(df["t"], utc=True).min()).total_seconds() / 86400
    coins = {s: g.reset_index(drop=True) for s, g in df.groupby("symbol", sort=True)}
    sigs = {s: signals(g) for s, g in coins.items()}
    out = {}
    for pattern in ("momentum", "dip", "big dip", "breakout"):
        for label, hold in HOLDS.items():
            parts, rand = [], np.zeros(DRAWS)
            for s, g in coins.items():
                tr = trades(g, sigs[s][pattern], hold)
                parts.append(tr)
                rand = rand + random_mean(g, len(tr), hold, rng) * len(tr)
            tr = pd.concat(parts, ignore_index=True)
            out[f"{pattern}, held {label}"] = evaluate(tr, rand / max(len(tr), 1), max(span, 1.0))
    hold = {s: float(g["close"].iloc[-1] / g["open"].iloc[0] - 1) for s, g in coins.items()}
    return out, hold


NAMES = {"momentum": "Rose 2%+ in the last hour: buy", "dip": "Fell 2%+ in the last hour: buy the dip",
         "big dip": "Fell 5%+ in the last hour: buy the dip", "breakout": "Above its 24-hour high: buy"}


def text(results: dict[str, dict], hold: dict[str, float], first: str, last: str) -> str:
    lines = [f"Crypto day-trading research: 15-minute bars {first} to {last}, {', '.join(sorted(hold))} "
             f"(Alpaca). Buying only (Alpaca can't short crypto). Fees per round trip: limit orders "
             f"{FEES['limit']:.2%}, market orders {FEES['market']:.2%}. Halves: before / from {SPLIT[:7]}. "
             "Luck check: the same number of trades at random times in the same coins.", "",
             "Buy and hold over the whole period: " + ", ".join(f"{s.split('/')[0]} {r:+.0%}" for s, r in sorted(hold.items())),
             ""]
    for name, r in results.items():
        pattern, held = name.split(", held ")
        lines.append(f"== {NAMES[pattern]}, sold {held} later ==")
        if not r.get("trades"):
            lines += ["  no trades", ""]
            continue
        lim, mkt = r["limit"], r["market"]
        lines += [f"  {r['trades']:,} trades ({r['per_day']:.1f} a day over all coins); per trade {r['gross']:+.3%} "
                  f"before fees (random times: {r['random']:+.3%}); right {r['hit']:.0%} before fees",
                  f"  after fees: {lim['net']:+.3%} a trade with limit orders (before {SPLIT[:7]}: {lim['early']:+.3%}, "
                  f"from: {lim['late']:+.3%}; t = {lim['t']:.1f}), {mkt['net']:+.3%} with market orders",
                  f"  luck check: random times did as well {r['luck']:.0%} of the time",
                  f"  → {'PASSES with limit orders' if passes(r) else 'does not pass'}"
                  + (" and with market orders" if passes(r, 'market') else ""), ""]
    passed = [n for n, r in results.items() if passes(r)]
    lines.append("Passed: " + ("; ".join(passed) if passed else
                               "none: no buy-only crypto pattern here beats the fees reliably") + ".")
    return "\n".join(lines)


def run(data, log=print) -> str:
    log(f"[crypto] 15-minute bars for {len(SYMBOLS)} coins since {START}...")
    df = fetch(data)
    if df.empty:
        return "No crypto bars came back from Alpaca."
    res, hold = study(df)
    return text(res, hold, str(df["t"].min())[:10], str(df["t"].max())[:10])
