"""Swing trades (hold a day to a few weeks), tested on 20 years of daily prices. A study only:
nothing here trades. Each idea is judged like the day-trading patterns: after costs, in both
halves of history, and against luck.

- Big movers: every morning, the stocks that open far above (or below) yesterday's close. Do the
  ones that jump keep running (buy at the open), or give it back? Held to that day's close, or
  for 5 days. Also: how often a +10% opener goes on to gain another 10% that day, and how often
  it falls 10%.
- Earnings drift: after a big earnings surprise (this quarter's profit far above or below the
  same quarter a year earlier, for this company), does the stock keep drifting the same way?
  Bought at the close of the first trading day after the filing, held 2, 5, 10 or 20 trading
  days, measured against the S&P 500 over the same days.
- Big news days: a stock that closes 8%+ (or 15%+) up or down on at least 3 times its usual volume
  (the price trace of big news). Too late to catch the first move (bots react in seconds); does the
  move carry on? Bought (after an up day) or sold short (after a down day) at the NEXT morning's open,
  held 1, 5 or 20 trading days, measured against the S&P 500 over the same days.

Only stocks still listed today have prices, so companies that collapsed and were delisted are
missing: that flatters the "buy" sides a little (most of all for big movers).
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

MIN_PRICE, MIN_DOLLAR_VOL = 5.0, 5e6    # yesterday's close and 20-day average $ traded a day
COST = {"movers": 0.0010, "earnings": 0.0005}   # round trip; jumpy stocks have wider spreads
GAPS = [(0.05, 0.10), (0.10, 0.20), (0.20, 0.60)]   # opening gaps checked (a bigger one is a data error)
HOLDS = (2, 5, 10, 20)                  # trading days, earnings drift
SURPRISE = 2.0                          # |sue| at least this: a big beat or miss
NEWS_MOVES = (0.08, 0.15)               # the day's close vs the day before: a big news day
NEWS_VOLUME_X = 3.0                     # ... on at least 3x the usual volume
NEWS_HOLDS = (1, 5, 20)
SPLIT = "2016-01-01"                    # the two halves of history, judged separately
SINCE = "2006-01-01"


def _daily(con: sqlite3.Connection, tickers: list[str], since: str) -> pd.DataFrame:
    q = (f"SELECT ticker, date, open, high, close, adj_close, volume FROM prices WHERE ticker IN "
         f"({','.join('?' * len(tickers))}) AND date >= ? ORDER BY ticker, date")
    df = pd.read_sql_query(q, con, params=[*tickers, since])
    df = df.dropna(subset=["open", "close", "adj_close"])
    df = df[(df["open"] > 0) & (df["close"] > 0)]
    f = df["adj_close"] / df["close"]                 # splits and dividends, applied to the open too
    df["o"], df["c"], df["h"] = df["open"] * f, df["adj_close"], df["high"].fillna(df["open"]) * f
    g = df.groupby("ticker", sort=False)
    prev_raw = g["close"].shift(1)
    df["prev"] = g["c"].shift(1)

    def avg(col: pd.Series) -> pd.Series:
        return col.groupby(df["ticker"]).transform(lambda s: s.rolling(20).mean().shift(1))

    df["dvol"] = avg(df["close"] * df["volume"])          # average $ traded a day, the 20 days before
    df["vol_x"] = df["volume"] / avg(df["volume"])        # today's volume vs normal (a real gap is busy)
    df["liquid"] = (prev_raw >= MIN_PRICE) & (df["dvol"] >= MIN_DOLLAR_VOL)
    # ~25 million rows for all stocks: keep it lean
    return df[["ticker", "date", "o", "h", "c", "prev", "liquid", "dvol", "vol_x"]]


def mover_trades(df: pd.DataFrame, hold: int = 1) -> pd.DataFrame:
    """[date, ticker, gap, ret]: bought at the open of every liquid stock that gapped up or down at
    least GAPS[0][0], sold at the close `hold` trading days later (1 = the same day)."""
    df = df.copy()
    df["gap"] = df["o"] / df["prev"] - 1
    df["exit"] = df.groupby("ticker", sort=False)["c"].shift(1 - hold)
    m = df["liquid"] & (df["gap"].abs() >= GAPS[0][0]) & (df["gap"].abs() < GAPS[-1][1]) & df["exit"].notna()
    out = df.loc[m, ["date", "ticker", "gap", "o", "exit"]].copy()
    out["ret"] = out["exit"] / out["o"] - 1
    return out[["date", "ticker", "gap", "ret"]].reset_index(drop=True)


FADE = [   # (smallest gap, trading days held, stop above the entry, min $ traded a day, borrow fee a year)
    (0.10, 1, 0.10, 5e6, 0.30), (0.10, 1, 0.10, 20e6, 0.30), (0.20, 1, 0.15, 5e6, 0.30),
    (0.20, 1, 0.15, 20e6, 0.30), (0.10, 5, 0.15, 20e6, 0.30), (0.20, 5, 0.20, 20e6, 0.50),
]
MIN_VOLUME_X = 2.0     # a real gap trades at least twice its usual volume (a price error usually doesn't)
SHORT_SLIPPAGE = 0.002   # extra cost when a stop is hit (jumpy stocks move past the stop price)


def fade_trades(df: pd.DataFrame, min_gap: float, hold: int, stop: float, min_dvol: float,
                borrow: float, min_volume_x: float = MIN_VOLUME_X) -> pd.DataFrame:
    """[date, ticker, gap, ret]: sold short at the open of every stock that opened at least `min_gap`
    up (busy volume, enough $ traded), bought back at the close `hold` trading days later, or at the
    stop (`stop` above the entry; at the open if a later day opens past it). ret includes the borrow
    fee for the nights held, and slippage when stopped out."""
    gap = df["o"] / df["prev"] - 1
    m = (df["liquid"] & (df["dvol"] >= min_dvol) & (df["vol_x"] >= min_volume_x)
         & (gap >= min_gap) & (gap < GAPS[-1][1]))
    ret = _short_from_open(df, hold, stop, borrow)
    keep = m & ret.notna()
    return pd.DataFrame({"date": df.loc[keep, "date"], "ticker": df.loc[keep, "ticker"],
                         "gap": gap[keep], "ret": ret[keep]}).reset_index(drop=True)


def _short_from_open(df: pd.DataFrame, hold: int, stop: float, borrow: float) -> pd.Series:
    """For every row: sold short at that day's open, bought back at the close `hold` trading days later,
    or at the stop (`stop` above the entry; at the open if a later day opens past it); with the borrow
    fee for the nights held and slippage when stopped out."""
    g = df.groupby("ticker", sort=False)
    entry = df["o"]
    level = entry * (1 + stop)
    ret = pd.Series(np.nan, index=df.index)
    out_ = pd.Series(False, index=df.index)
    for k in range(hold):
        o_k, h_k, c_k = (g[c].shift(-k) for c in ("o", "h", "c"))
        live = ~out_
        opened_past = live & (k > 0) & (o_k >= level)          # gapped past the stop overnight
        ret[opened_past] = -(o_k[opened_past] / entry[opened_past] - 1) - SHORT_SLIPPAGE
        hit = live & ~opened_past & (h_k >= level)
        ret[hit] = -stop - SHORT_SLIPPAGE
        out_ |= opened_past | hit
        if k == hold - 1:
            last = ~out_ & c_k.notna()
            ret[last] = -(c_k[last] / entry[last] - 1)
            out_ |= last
    return ret - borrow / 252 * (hold - 1)                     # a short held overnight pays a fee


NEWS_SHORTS = [   # (trading days held, stop above the entry, borrow fee a year): realistic short trades
    (1, 0.15, 0.0), (5, 0.20, 0.30), (20, 0.25, 0.30)]


LOSER_STOPS = (0.10, 0.20, 0.25, 0.30, 9.99)   # 9.99: no stop at all


def news_short_trades(df: pd.DataFrame, spy: pd.DataFrame, sign: int, move: float, hold: int, stop: float,
                      borrow: float) -> pd.DataFrame:
    """[date, ticker, move, ret]: sold short at the open the morning after a big news day (`sign` +1:
    closed `move`+ up, -1: down, on NEWS_VOLUME_X+ its usual volume), as a real short trade: a stop,
    borrow fees, slippage. ret is the short's result minus the S&P 500's fall over the same days (a
    short gains when the stock falls, so a market fall isn't counted as skill)."""
    g = df.groupby("ticker", sort=False)
    day_move = df["c"] / df["prev"] - 1
    news = (df["liquid"] & (sign * day_move >= move) & (day_move.abs() < GAPS[-1][1])
            & (df["vol_x"] >= NEWS_VOLUME_X))
    m = news.groupby(df["ticker"]).shift(1).fillna(False).astype(bool)    # yesterday was the news day
    ret = _short_from_open(df, hold, stop, borrow)
    exit_date = g["date"].shift(1 - hold)
    keep = m & ret.notna() & exit_date.notna()
    out = pd.DataFrame({"date": df.loc[keep, "date"], "ticker": df.loc[keep, "ticker"],
                        "move": day_move.groupby(df["ticker"]).shift(1)[keep], "ret": ret[keep],
                        "exit_date": exit_date[keep]})
    s = spy.set_index("date")
    market = s["c"].reindex(out["exit_date"]).to_numpy() / s["o"].reindex(out["date"]).to_numpy() - 1
    out["ret"] = out["ret"] + market
    return out.dropna(subset=["ret"])[["date", "ticker", "move", "ret"]].reset_index(drop=True)


def news_short_study(df: pd.DataFrame, spy: pd.DataFrame) -> dict[str, dict]:
    out = {}
    for sign, word in ((1, "up"), (-1, "down")):
        for move in NEWS_MOVES:
            for hold, stop, borrow in NEWS_SHORTS:
                tr = news_short_trades(df, spy, sign, move, hold, stop, borrow)
                held = "to the close" if hold == 1 else f"{hold} days (borrow fee {borrow:.0%} a year)"
                out[f"After a {move:.0%}+ {word} day on big volume: short next morning, held {held}, "
                    f"stop {stop:.0%} above, vs the S&P 500"] = evaluate(tr["ret"], tr["date"], COST["movers"])
    for stop in LOSER_STOPS:   # the live 'short yesterday's big losers': how wide should its stop be?
        tr = news_short_trades(df, spy, -1, 0.15, 1, stop, 0.0)
        out[f"After a 15%+ down day on big volume: short next morning, held to the close, "
            + (f"stop {stop:.0%} above" if stop < 1 else "no stop") + ", vs the S&P 500"] = evaluate(
            tr["ret"], tr["date"], COST["movers"])
    return out


def fade_study(df: pd.DataFrame) -> dict[str, dict]:
    """Each FADE line; and the live pattern's own version (20%+, same day, 15% stop, $5M+) without the
    volume filter: the gap day's volume isn't known at the open, so live trading can't use it."""
    out = {}
    for gap, hold, stop, dvol, borrow, vol_x in [(*f, MIN_VOLUME_X) for f in FADE] + [(0.20, 1, 0.15, 5e6, 0.30, 0.0)]:
        tr = fade_trades(df, gap, hold, stop, dvol, borrow, vol_x)
        held = "to the close" if hold == 1 else f"for {hold} days (borrow fee {borrow:.0%} a year)"
        name = (f"Short {gap:.0%}+ gap-ups at the open, held {held}, stop {stop:.0%} above, "
                f"stocks trading ${dvol / 1e6:g}M+ a day" + ("" if vol_x else ", no volume filter (as live)"))
        out[name] = evaluate(tr["ret"], tr["date"], COST["movers"])
        if not tr.empty:
            out[name]["stopped"] = float((tr["ret"] <= -stop - SHORT_SLIPPAGE + 1e-9).mean())
            out[name]["worst"] = float(tr["ret"].min())
    return out


def news_trades(df: pd.DataFrame, spy: pd.DataFrame, hold: int, move: float = NEWS_MOVES[0],
                volume_x: float = NEWS_VOLUME_X) -> pd.DataFrame:
    """[date, ticker, move, ret]: after a liquid stock's big news day (closed `move`+ up or down on
    `volume_x`+ its usual volume), in at the next day's open, out at the close `hold` trading days
    later. ret is the stock minus the S&P 500 over the same span (spy: [date, o, c])."""
    g = df.groupby("ticker", sort=False)
    day_move = df["c"] / df["prev"] - 1
    entry, entry_date = g["o"].shift(-1), g["date"].shift(-1)
    exit_, exit_date = g["c"].shift(-hold), g["date"].shift(-hold)
    m = (df["liquid"] & (day_move.abs() >= move) & (day_move.abs() < GAPS[-1][1]) & (df["vol_x"] >= volume_x)
         & entry.notna() & exit_.notna())
    out = pd.DataFrame({"date": entry_date[m], "ticker": df.loc[m, "ticker"], "move": day_move[m],
                        "ret": exit_[m] / entry[m] - 1, "exit_date": exit_date[m]})
    s = spy.set_index("date")
    market = s["c"].reindex(out["exit_date"]).to_numpy() / s["o"].reindex(out["date"]).to_numpy() - 1
    out["ret"] = out["ret"] - market
    return out.dropna(subset=["ret"])[["date", "ticker", "move", "ret"]].reset_index(drop=True)


def news_study(df: pd.DataFrame, spy: pd.DataFrame) -> dict[str, dict]:
    """Each size of news day, up and down, each holding time: following the move (bought after an up
    day, sold short after a down day). A clearly negative result means the move tends to reverse."""
    out = {}
    for hold in NEWS_HOLDS:
        tr = news_trades(df, spy, hold)
        for move in NEWS_MOVES:
            for sign, word, act in ((1, "up", "bought"), (-1, "down", "sold short")):
                part = tr[sign * tr["move"] >= move]
                out[f"Closed {move:.0%}+ {word} on big volume, {act} next morning, held {hold} day"
                    f"{'s' if hold > 1 else ''}, vs the S&P 500"] = evaluate(sign * part["ret"], part["date"],
                                                                         COST["movers"])
    return out


def earnings_trades(df: pd.DataFrame, events: pd.DataFrame, spy: pd.Series, hold: int) -> pd.DataFrame:
    """[date, ticker, sue, ret]: bought at the close of the first trading day after each filing with a
    surprise, held `hold` trading days; ret is the stock minus the S&P 500 over the same days."""
    rows = []
    by = {t: g.reset_index(drop=True) for t, g in df.groupby("ticker", sort=False)}
    for r in events.itertuples(index=False):
        g = by.get(r.ticker)
        if g is None:
            continue
        p = int(np.searchsorted(g["date"].to_numpy(), r.filed, side="right"))   # the day after the filing
        if p + hold >= len(g) or not g.at[p, "liquid"]:
            continue
        d0, d1 = g.at[p, "date"], g.at[p + hold, "date"]
        if d0 not in spy.index or d1 not in spy.index:
            continue
        ret = g.at[p + hold, "c"] / g.at[p, "c"] - 1 - (spy[d1] / spy[d0] - 1)
        rows.append((d0, r.ticker, r.sue, ret))
    return pd.DataFrame(rows, columns=["date", "ticker", "sue", "ret"])


def evaluate(rets: pd.Series, dates: pd.Series, cost: float, draws: int = 500, seed: int = 0) -> dict:
    """Per trade after costs; both halves; t and the luck check on monthly averages (trades in the
    same month move together, so they aren't counted as independent)."""
    if rets.empty:
        return {"trades": 0}
    net = rets.to_numpy(float) - cost
    month = pd.to_datetime(dates).dt.to_period("M").to_numpy()
    monthly = pd.Series(net).groupby(month).mean()
    t = float(monthly.mean() / monthly.std(ddof=1) * np.sqrt(len(monthly))) if len(monthly) > 2 and monthly.std() > 0 else 0.0
    early = net[pd.to_datetime(dates).to_numpy() < np.datetime64(SPLIT)]
    late = net[pd.to_datetime(dates).to_numpy() >= np.datetime64(SPLIT)]
    rng = np.random.default_rng(seed)
    raw = rets.to_numpy(float)
    better = sum(pd.Series(raw * rng.choice([-1.0, 1.0], len(raw)) - cost).groupby(month).mean().mean()
                 >= monthly.mean() for _ in range(draws))
    return {"trades": len(net), "months": len(monthly), "per_trade": float(net.mean()),
            "gross": float(raw.mean()), "hit": float((net > 0).mean()),
            "early": float(early.mean()) if len(early) else np.nan, "late": float(late.mean()) if len(late) else np.nan,
            "t": t, "luck": better / draws}


def verdict(r: dict) -> str:
    if not r.get("trades"):
        return "no trades"
    ok = r["early"] > 0 and r["late"] > 0 and r["t"] > 2 and r["luck"] < 0.05
    return ("PASSES: positive after costs in both halves, unlikely to be luck" if ok else
            "doesn't pass: " + "; ".join(x for x in (
                "loses money after costs" if r["per_trade"] <= 0 else "",
                "not positive in both halves" if not (r["early"] > 0 and r["late"] > 0) else "",
                "could easily be luck" if r["luck"] >= 0.05 or r["t"] <= 2 else "") if x))


def line(name: str, r: dict) -> list[str]:
    if not r.get("trades"):
        return [f"== {name} ==", "  no trades", ""]
    return [f"== {name} ==",
            f"  {r['trades']:,} trades in {r['months']:,} months; per trade {r['per_trade']:+.2%} after costs "
            f"({r['gross']:+.2%} before); right {r['hit']:.0%} of the time",
            f"  per trade before {SPLIT[:4]}: {r['early']:+.2%}, from {SPLIT[:4]}: {r['late']:+.2%}; "
            f"luck check: random directions did as well {r['luck']:.0%} of the time (t = {r['t']:.1f})",
            f"  → {verdict(r)}", ""]


def movers_study(df: pd.DataFrame) -> tuple[dict[str, dict], dict[str, float]]:
    """Each gap size, up and down, held the day and 5 days; 'buy' always (a gap-up bought is riding it,
    a gap-down bought is betting on a bounce). Plus how +10% openers did within the day."""
    out = {}
    for hold, label in ((1, "to the close"), (5, "for 5 days")):
        tr = mover_trades(df, hold)
        for lo, hi in GAPS:
            for sign, word in ((1, "up"), (-1, "down")):
                part = tr[(sign * tr["gap"] >= lo) & (sign * tr["gap"] < hi)]
                name = f"Opened {lo:.0%}-{hi:.0%} {word}, bought at the open, held {label}"
                out[name] = evaluate(part["ret"], part["date"], COST["movers"])
    day = mover_trades(df, 1)
    big = day[day["gap"] >= 0.10]
    dream = {"n": len(big), "up10": float((big["ret"] >= 0.10).mean()) if len(big) else np.nan,
             "down10": float((big["ret"] <= -0.10).mean()) if len(big) else np.nan,
             "median": float(big["ret"].median()) if len(big) else np.nan}
    return out, dream


def earnings_study(df: pd.DataFrame, events: pd.DataFrame, spy: pd.Series) -> dict[str, dict]:
    out = {}
    for hold in HOLDS:
        tr = earnings_trades(df, events, spy, hold)
        for sign, word in ((1, "beat"), (-1, "miss")):
            part = tr[sign * tr["sue"] >= SURPRISE]
            # a miss is a short: its result is the stock's fall vs the market
            out[f"Big earnings {word} (|surprise| ≥ {SURPRISE:g}), {'bought' if sign > 0 else 'sold short'}, "
                f"held {hold} days, vs the S&P 500"] = evaluate(sign * part["ret"], part["date"], COST["earnings"])
    return out


def run(con: sqlite3.Connection, since: str = SINCE, log=print, chunk: int = 300) -> str:
    """Both studies on every listed stock; returns the text for Discord."""
    from trend_bot import earnings, fundamentals
    from trend_bot.company_info import fund_tickers
    from trend_bot.db import load_prices
    from trend_bot.factors import primary_tickers

    funds = fund_tickers(con)
    tickers = [t for (t,) in con.execute("SELECT DISTINCT ticker FROM tickers WHERE last_date IS NOT NULL")
               if t not in funds]
    log(f"[swing] {len(tickers):,} stocks since {since}...")
    frames = [_daily(con, tickers[i:i + chunk], since) for i in range(0, len(tickers), chunk)]
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    movers, dream = movers_study(df)
    lines = [f"Swing-trade research, daily prices {df['date'].min():%Y-%m-%d} to {df['date'].max():%Y-%m-%d}, "
             f"stocks over ${MIN_PRICE:g} trading at least ${MIN_DOLLAR_VOL / 1e6:g}M a day. Costs per round trip: "
             f"big movers {COST['movers']:.2%}, earnings {COST['earnings']:.2%}. Halves: before / from {SPLIT[:4]}.",
             "", "BIG MOVERS (bought at the open)", ""]
    for name, r in movers.items():
        lines += line(name, r)
    if dream["n"]:
        lines += [f"Stocks that opened 10%+ up ({dream['n']:,} times): by that day's close {dream['up10']:.0%} had "
                  f"gained another 10%+, {dream['down10']:.0%} had lost 10%+; the typical one {dream['median']:+.1%}.", ""]
    every = dict(movers)
    fade = fade_study(df)
    every.update(fade)
    lines += [f"SHORTING BIG GAP-UPS (only gaps on at least {MIN_VOLUME_X:g}x the usual volume, so price errors "
              f"stay out; a stop hit costs {SHORT_SLIPPAGE:.1%} extra; whether the broker can lend the shares "
              "can't be known for the past: the $20M+ lines are the more borrowable stocks)", ""]
    for name, r in fade.items():
        lines += line(name, r)
        if r.get("trades"):
            lines.insert(-1, f"  stopped out {r['stopped']:.0%} of the time; worst trade {r['worst']:+.1%}")
    spy_daily = _daily(con, ["SPY"], since)
    spy_daily = spy_daily.assign(date=pd.to_datetime(spy_daily["date"]))[["date", "o", "c"]]
    log("[swing] big news days...")
    news = news_study(df, spy_daily)
    every.update(news)
    lines += [f"BIG NEWS DAYS (a close {NEWS_MOVES[0]:.0%}+ up or down on {NEWS_VOLUME_X:g}x+ the usual volume; "
              "following the move from the next morning's open, vs the S&P 500; a clearly negative result "
              "means the move tends to reverse instead)", ""]
    for name, r in news.items():
        lines += line(name, r)
    log("[swing] news shorts with stops and borrow fees...")
    shorts = news_short_study(df, spy_daily)
    every.update(shorts)
    lines += ["SHORTING AFTER BIG NEWS, AS REAL TRADES (a stop above, slippage when it's hit, a borrow fee "
              "for each night held; vs the S&P 500)", ""]
    for name, r in shorts.items():
        lines += line(name, r)
    facts = fundamentals.load_facts(con)
    if facts.empty:
        lines.append("EARNINGS DRIFT: no company financials yet (run the fundamentals refresh).")
    else:
        log("[swing] earnings surprises...")
        prim = primary_tickers(con)
        ev = earnings.events(con, facts, prim).dropna(subset=["sue"])
        ev["ticker"] = ev["cik"].map(prim)
        ev = ev.dropna(subset=["ticker"])
        ev = ev[ev["filed"] >= pd.Timestamp(since)]
        spy = load_prices(con, "SPY", since)["Close"]
        lines += ["EARNINGS DRIFT (after a big surprise; vs the S&P 500)", ""]
        drift = earnings_study(df, ev, spy)
        every.update(drift)
        for name, r in drift.items():
            lines += line(name, r)
    passed = [n for n, r in every.items() if verdict(r).startswith("PASSES")]
    lines.append("Passed: " + ("; ".join(passed) + ". Worth discussing a small test slot on the demo account."
                               if passed else "none: no swing idea here beats its costs reliably."))
    lines.append("(Companies delisted since aren't in the price data, which flatters the buy sides a little.)")
    return "\n".join(lines)
