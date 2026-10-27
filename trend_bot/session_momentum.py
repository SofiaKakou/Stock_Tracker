"""'Session momentum', the Nasdaq strategy from a TikTok ad, tested honestly. A study only: nothing trades.

The rules as shown: on 5-minute bars, wait for the New York open. If the first 5-minute bar (9:30-9:35)
closes above the 12-bar exponential average (12 EMA), buy; below it, sell short. No fixed profit
target: the stop starts at the other end of the first bar and then follows the trend (the 12 EMA), so
winners can run. Their claim: 1,207 trades in six years, 57% winners, +508% in total.

Tested here on QQQ (the Nasdaq 100 fund) and SPY since 2016, in three versions, so we can see what
each part adds:
- as shown: the first bar's stop, then out when a 5-minute bar closes on the wrong side of the 12 EMA
  (at the next bar's open), else at the close
- the trailing EMA exit only (no first-bar stop)
- the entry only, held to the close (what the trailing exit is worth)
Each one with the usual costs (index funds), in both halves of history, against luck (the same days in
random directions), and once more at the costs our day-trading account actually measured.
"""

from __future__ import annotations

import pandas as pd

from trend_bot import intraday

SYMBOLS = ["QQQ", "SPY"]
START = "2016-01-01"
EMA = 12
TIMEFRAME = "5Min"
LIVE_COST = 0.0034      # round trip: the 0.17% a side the day-trading account paid with market orders (its first week)
VERSIONS = {"as_shown": "As shown: first bar's stop, then the 12 EMA trails",
            "ema_only": "The 12 EMA trailing exit only",
            "to_close": "Entry only, held to the close"}


def bars(data: "intraday.Data", symbols: list[str] = SYMBOLS, start: str = START, end: str | None = None) -> pd.DataFrame:
    """5-minute bars, regular hours only: [symbol, date, slot, open, high, low, close] (slot 0 = 9:30-9:35)."""
    end = end or pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    df = data.bars(symbols, start, end, timeframe=TIMEFRAME)
    return regular(df)


def regular(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    t = pd.to_datetime(df["t"], utc=True).dt.tz_convert(intraday.NY)
    minutes = t.dt.hour * 60 + t.dt.minute
    df = df.assign(date=t.dt.date.astype(str), slot=(minutes - (9 * 60 + 30)) // 5)
    return df[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)].drop(columns=["t"]).reset_index(drop=True)


def trades(df: pd.DataFrame, version: str = "as_shown") -> pd.DataFrame:
    """[date, symbol, side, ret, cost]: one trade a day per symbol (full days only)."""
    rows = []
    for sym, g in df.sort_values(["symbol", "date", "slot"]).groupby("symbol", sort=False):
        g = g.reset_index(drop=True)
        ema = g["close"].ewm(span=EMA, adjust=False).mean().to_numpy()   # carries over from day to day
        for date, d in g.groupby("date", sort=False):
            if len(d) != 78 or d["slot"].iloc[0] != 0:       # full 9:30-16:00 days only
                continue
            i0 = d.index[0]
            o, h, lo, c = (d[k].to_numpy() for k in ("open", "high", "low", "close"))
            e = ema[i0:i0 + 78]
            if c[0] == e[0]:
                continue
            side = 1.0 if c[0] > e[0] else -1.0
            entry, out = o[1], c[-1]                           # in at 9:35; by default out at the close
            stop = (lo[0] if side > 0 else h[0]) if version == "as_shown" else None
            if version != "to_close":
                for k in range(1, 78):
                    if stop is not None and (lo[k] <= stop if side > 0 else h[k] >= stop):
                        out = min(o[k], stop) if side > 0 else max(o[k], stop)   # a gap past it fills worse
                        break
                    if k < 77 and (c[k] < e[k] if side > 0 else c[k] > e[k]):    # closed across the EMA
                        out = o[k + 1]
                        break
            rows.append((date, sym, side, side * (out / entry - 1)))
    out = pd.DataFrame(rows, columns=["date", "symbol", "side", "ret"])
    return out.assign(cost=intraday.COST["etf"])


def study(df: pd.DataFrame, measured_cost: float | None = None) -> dict[str, dict]:
    """Each version per symbol, with the usual cost; plus `<version> at measured cost` if one is given."""
    out = {}
    for v in VERSIONS:
        tr = trades(df, v)
        for sym in sorted(tr["symbol"].unique()):
            part = tr[tr["symbol"] == sym]
            r = intraday.evaluate(part)
            r["win"] = float((part["ret"] > 0).mean())
            r["longs"] = float((part["side"] > 0).mean())
            r["compounded"] = float((1 + part["ret"] - part["cost"]).prod() - 1)
            out[f"{v}:{sym}"] = r
            if measured_cost:
                real = intraday.evaluate(part.assign(cost=measured_cost))
                real["compounded"] = float((1 + part["ret"] - measured_cost).prod() - 1)
                out[f"{v}:{sym}:measured"] = real
    return out


def text(results: dict[str, dict], first: str, last: str, measured_cost: float | None = None) -> str:
    lines = [f"Session momentum (the TikTok Nasdaq strategy), 5-minute bars {first} to {last}. Cost per round "
             f"trip {intraday.COST['etf']:.2%} (index funds)"
             + (f"; also at the {measured_cost:.2%} our day-trading account measured" if measured_cost else "")
             + f". Halves: before / from {intraday.SPLIT[:4]}. Their claim: 57% winners, +508% in six years.", ""]
    for v, name in VERSIONS.items():
        for key in sorted(k for k in results if k.startswith(f"{v}:") and not k.endswith(":measured")):
            r, sym = results[key], key.split(":")[1]
            lines.append(f"== {name}: {sym} ==")
            if not r.get("days"):
                lines += ["  no trades", ""]
                continue
            lines += [f"  {r['trades']:,} trades; per trade {r['per_trade']:+.3%} after costs "
                      f"({r['gross_per_trade']:+.3%} before); winners {r['win']:.0%}; long {r['longs']:.0%} of days",
                      f"  all trades one after another: {r['compounded']:+.0%} (no borrowed money); yearly "
                      f"{r['yearly']:+.1%} (before {intraday.SPLIT[:4]}: {r['early']:+.1%}, from: {r['late']:+.1%})",
                      f"  luck check: random directions did as well {r['luck']:.0%} of the time (t = {r['t']:.1f})",
                      f"  → {intraday.verdict(r)}"]
            m = results.get(f"{key}:measured")
            if m and m.get("days"):
                lines.append(f"  at our measured cost: {m['per_trade']:+.3%} a trade, all trades {m['compounded']:+.0%} "
                             f"→ {'passes' if intraday.verdict(m).startswith('PASSES') else 'does not pass'}")
            lines.append("")
    passed = [k for k, r in results.items() if not k.endswith(":measured") and intraday.verdict(r).startswith("PASSES")]
    lines.append("Passed: " + (", ".join(passed) if passed else "none") + ".")
    return "\n".join(lines)


def run(data: "intraday.Data", measured_cost: float | None = LIVE_COST, log=print) -> str:
    log(f"[session] 5-minute bars for {', '.join(SYMBOLS)} since {START}...")
    df = bars(data)
    if df.empty:
        return "No 5-minute bars came back from Alpaca."
    return text(study(df, measured_cost), df["date"].min(), df["date"].max(), measured_cost)
