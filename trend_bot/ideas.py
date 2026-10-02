"""Monthly top ideas from the simple mix: every signal ranked, then averaged, nothing fitted.

In the walk-forward test (study ml) the plain equal-weight average of all the
signals was steadier than the machine-learning model, so it's the bot's main
ideas list. Each stock in the universe (~1,000 most traded, over $5, no funds)
is ranked on every signal, each pointed the way research expects (cheap,
profitable, beating earnings, few short sellers, strong past year, ...); the
average of those ranks is its score.

Each idea comes with the signals that put it there, and every list is recorded
for forward tracking, so its real track record builds up month by month.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import numpy as np
import pandas as pd

from trend_bot import factors, fundamentals, ml
from trend_bot.db import get_meta, set_meta

MIN_SIGNALS = 8  # stocks with fewer known signals aren't listed (too little to go on)

# What a high (oriented) rank on each signal means, and what a low one means.
GOOD = {
    "gross_profitability": "very profitable", "roe": "high return on equity",
    "accruals": "profits backed by cash", "leverage": "little debt",
    "revenue_growth": "fast sales growth", "asset_growth": "not over-expanding",
    "earnings_yield": "cheap vs profits", "book_to_market": "cheap vs book value",
    "sales_to_price": "cheap vs sales", "sue": "profit beat", "revenue_sue": "sales beat",
    "ear": "market liked last report", "short_ratio": "few short sellers",
    "days_to_cover": "shorts can exit easily", "short_change": "short sellers backing off",
    "short_volume_ratio": "little short selling", "mom12": "strong past year",
    "vol": "calm stock", "high52": "near its 52-week high",
}
BAD = {
    "gross_profitability": "weak profitability", "roe": "low return on equity",
    "accruals": "profits not backed by cash", "leverage": "heavy debt",
    "revenue_growth": "shrinking sales", "asset_growth": "expanding fast",
    "earnings_yield": "expensive vs profits", "book_to_market": "expensive vs book value",
    "sales_to_price": "expensive vs sales", "sue": "profit miss", "revenue_sue": "sales miss",
    "ear": "market disliked last report", "short_ratio": "heavily shorted",
    "days_to_cover": "crowded short", "short_change": "short sellers piling in",
    "short_volume_ratio": "lots of short selling", "mom12": "weak past year",
    "vol": "jumpy stock", "high52": "far below its 52-week high",
}


def oriented_ranks(df: pd.DataFrame, by_sector: bool = False, extra: bool = False) -> pd.DataFrame:
    """Each signal as a 0-1 rank within the month (or industry), flipped so 1 is always 'good'."""
    out = {}
    for c in ml.SIGNALS:
        if c in df and df[c].notna().any():
            r = factors.group_rank(df, c, by_sector)
            out[c] = r if fundamentals.EXPECTED[c] > 0 else 1 - r
    out["mom12"] = df.groupby("month")["mom12"].rank(pct=True)
    if extra:
        for c, sign in ml.EXTRA.items():
            if c in df and df[c].notna().any():
                r = df.groupby("month")[c].rank(pct=True)
                out[c] = r if sign > 0 else 1 - r
    return pd.DataFrame(out, index=df.index)


def reasons(ranks: pd.Series, best: bool = True, n: int = 3) -> str:
    """The strongest few signals behind a stock's place on the list, in plain words."""
    r = ranks.dropna().sort_values(ascending=not best)
    r = r[r >= 0.8] if best else r[r <= 0.2]
    words = GOOD if best else BAD
    return ", ".join(words.get(k, k) for k in r.index[:n]) or "a bit of everything"


def rank_month(df: pd.DataFrame, by_sector: bool = False, extra: bool = False) -> pd.DataFrame:
    """Score every stock in the table's latest month; best first. Columns: score, pct, close, sector, why."""
    df = df[df["gone"].isna()] if "gone" in df else df
    df = df.assign(**{c: np.nan for c in ml.FEATURES if c not in df})  # e.g. no short data loaded yet
    month = df["month"].max()
    now = df[df["month"] == month].copy()
    ranks = oriented_ranks(now, by_sector, extra)
    now["score"] = ml.simple_mix(now, by_sector, extra)
    now["signals"] = ranks.notna().sum(axis=1)
    now = now[now["signals"] >= MIN_SIGNALS].dropna(subset=["score"])
    now["pct"] = now["score"].rank(pct=True)
    now = now.sort_values("score", ascending=False)
    k = max(1, len(now) // 2)
    now["why"] = [reasons(ranks.loc[i], best=j < k) for j, i in enumerate(now.index)]
    if "sector" not in now:
        now["sector"] = None
    return now.set_index("ticker")[["score", "pct", "close", "signals", "sector", "why"]]


def latest(con: sqlite3.Connection, universe: int = 1000, log=print) -> tuple[pd.DataFrame, str]:
    """Today's ranking and the date of the prices it uses."""
    since = (dt.date.today() - dt.timedelta(days=75)).isoformat()
    # Reuse the full table if a study already built it from today's data; otherwise build just
    # the last few months (much faster).
    df = factors.table(con, since=since, universe=universe, log=log, build=False)
    if df is None:
        df = factors.monthly_factors(con, since=since, universe=universe, log=log, extras=True,
                                     include_latest=True)
    if df.empty:
        return pd.DataFrame(), ""
    date = con.execute("SELECT MAX(date) FROM prices WHERE ticker IN (SELECT ticker FROM tickers "
                       "WHERE last_date IS NOT NULL)").fetchone()[0][:10]
    return rank_month(df, by_sector=method(con) == "industry_mix", extra=extra(con)), date


def save(con: sqlite3.Connection, ranked: pd.DataFrame, date: str, top: int = 15, bottom: int = 10) -> None:
    pick = lambda part: [{"ticker": t, "close": float(r["close"]) if pd.notna(r["close"]) else None,
                          "sector": r["sector"] if isinstance(r["sector"], str) else None,
                          "why": r["why"]} for t, r in part.iterrows()]
    set_meta(con, "mix_ranking", json.dumps({"date": date, "stocks": len(ranked), "top": pick(ranked.head(top)),
                                             "bottom": pick(ranked.tail(bottom).iloc[::-1])}))
    con.commit()


def load(con: sqlite3.Connection, max_age_days: int = 45) -> dict | None:
    raw = get_meta(con, "mix_ranking")
    if not raw:
        return None
    r = json.loads(raw)
    return r if (dt.date.today() - dt.date.fromisoformat(r["date"])).days <= max_age_days else None


# --- How reliable has it been? -------------------------------------------------------------------

def save_scorecard(con: sqlite3.Connection, card: dict[str, pd.DataFrame], preds: pd.DataFrame) -> None:
    """Keep the mix's walk-forward numbers from study ml (it's tested there, next to the model).

    Two versions are tested: signals ranked against the whole market (simple_mix) and within
    each industry (industry_mix). The industry version is used only if it ranked stocks better
    in both halves of the test; otherwise the plain one stays.
    """
    halves = [k for k in card if k != "all"]
    ic = lambda method, label: (card[label].loc[method, "mean_ic"]
                                if method in card[label].index else np.nan)
    better = bool(halves) and all(ic("industry_mix", h) > ic("simple_mix", h) for h in halves)
    method = "industry_mix" if better else "simple_mix"
    # Then the two extra price signals, kept only if they improve the chosen version in both halves.
    plus = bool(halves) and all(ic(f"{method}_plus", h) > ic(method, h) for h in halves)
    out = {"method": method, "extra": plus, "from": f"{preds['month'].min():%Y}", "to": f"{preds['month'].max():%Y}"}
    tested = f"{method}_plus" if plus else method
    a = card["all"].loc[tested]
    for k in ("months", "mean_ic", "ic_t", "ic_positive", "top10_per_year", "average_stock_per_year"):
        out[k] = float(a[k]) if k in a and pd.notna(a[k]) else None
    out["halves_positive"] = all(ic(tested, h) > 0 for h in halves)
    out["other_ic"] = float(card["all"].loc["simple_mix" if better else "industry_mix", "mean_ic"]) \
        if "industry_mix" in card["all"].index else None
    set_meta(con, "mix_scorecard", json.dumps(out))
    con.commit()


def extra(con: sqlite3.Connection) -> bool:
    """True if the last test kept the two extra signals (calm stocks, near the 52-week high)."""
    raw = get_meta(con, "mix_scorecard")
    return bool(json.loads(raw).get("extra")) if raw else False


def method(con: sqlite3.Connection) -> str:
    """'industry_mix' if the last test chose comparing within industries, else 'simple_mix'."""
    raw = get_meta(con, "mix_scorecard")
    return json.loads(raw).get("method", "simple_mix") if raw else "simple_mix"


def reliability_note(con: sqlite3.Connection) -> str:
    raw = get_meta(con, "mix_scorecard")
    if not raw:
        return "Not tested yet: the monthly honesty check (study ml) adds its track record."
    s = json.loads(raw)
    if s.get("top10_per_year") is None:
        return "Not enough history to test it yet."
    steady = "in both halves of the test" if s.get("halves_positive") else "but not in both halves of the test"
    how = ("Company signals are compared within each industry (that tested better than against the whole market). "
           if s.get("method") == "industry_mix" else "")
    if s.get("extra"):
        how += "It also counts calm stocks and stocks near their 52-week high (that tested better too). "
    months = f", {s['months']:.0f} months" if s.get("months") else ""
    return (f"{how}Tested {s['from']}-{s['to']}{months} (nothing is fitted, so there's no hindsight in how it adds up the "
            f"signals): the top 10% returned "
            f"{s['top10_per_year']:+.1%} a year after costs vs {s['average_stock_per_year']:+.1%} for the average "
            f"stock, and the ranking pointed the right way in {s['ic_positive']:.0%} of months, {steady}. "
            f"{backtest_note(con) + ' ' if backtest_note(con) else ''}"
            "A small edge on average in the past, not a promise for any one stock.")


# --- Would it have made money? A portfolio test ---------------------------------------------------

def scores(df: pd.DataFrame, by_sector: bool = False, extra: bool = False) -> pd.Series:
    """The mix score for every row of a monthly table (NaN where too few signals are known)."""
    df = df.assign(**{c: np.nan for c in ml.FEATURES if c not in df})
    known = oriented_ranks(df, by_sector).notna().sum(axis=1)
    return ml.simple_mix(df, by_sector, extra).where(known >= MIN_SIGNALS)


MAX_PER_INDUSTRY = 5  # at most this many of the top 20 from one industry (when the cap is used)


def rebalance_capped(ranked: pd.Series, sectors: pd.Series, held: list[str], hold: int = 20, buffer: int = 40,
                     cap: int = MAX_PER_INDUSTRY) -> tuple[list[str], list[str], list[str]]:
    """Like model.rebalance (keep holdings still in the top `buffer`, fill up from the top), but with at
    most `cap` stocks from one industry. Stocks with an unknown industry aren't capped.
    Returns (new holdings, buys, sells)."""
    from collections import Counter

    pos = {t: i for i, t in enumerate(ranked.index)}
    count: Counter = Counter()
    sector = lambda t: sectors.get(t) if isinstance(sectors.get(t), str) else None

    def room(t: str) -> bool:
        return sector(t) is None or count[sector(t)] < cap

    keep: list[str] = []
    for t in sorted((t for t in held if t in pos and pos[t] < buffer), key=pos.get):
        if len(keep) < hold and room(t):
            keep.append(t)
            count[sector(t)] += 1
    buys: list[str] = []
    for t in ranked.index:
        if len(keep) + len(buys) >= hold:
            break
        if t not in keep and room(t):
            buys.append(t)
            count[sector(t)] += 1
    new = keep + buys
    return new, buys, [t for t in held if t not in new]


def backtest(df: pd.DataFrame, spy: pd.Series, hold: int = 20, buffer: int = 40, cost_bps: float = 10,
             cash_rate: float = 2.0, cap: int = MAX_PER_INDUSTRY, extra: bool = False) -> pd.DataFrame:
    """Monthly returns of holding the top `hold` ideas (equal weight), for both mixes, with and without
    the weather filter (cash when the S&P 500 is below its 200-day average), against SPY.

    A holding is kept while it stays in the top `buffer` (fewer trades). 0.1% per trade each way.
    Companies that disappeared are included for their last month, at their fate's return.
    Each version is also run with at most `cap` stocks per industry ("..._capped").
    """
    from trend_bot import model

    df = df.dropna(subset=["ret"]).reset_index(drop=True)
    weather = model._monthly_last(model.spy_weather(spy))
    spy_m = model._monthly_last(spy)
    cash_m = (1 + cash_rate / 100) ** (1 / 12) - 1
    cost = cost_bps / 10_000 * 2
    out = {}
    if "sector" not in df:
        df = df.assign(sector=None)
    variants = [("mix", False, False), ("mix_capped", False, True),
                ("industry_mix", True, False), ("industry_mix_capped", True, True)]
    for name, by_sector, capped in variants:
        s = scores(df, by_sector, extra)
        held: list[str] = []
        rows = {}
        for month, g in df.assign(score=s).dropna(subset=["score"]).groupby("month"):
            if len(g) < hold * 3:
                continue
            g = g.set_index("ticker")
            ranked = g["score"].sort_values(ascending=False)
            if capped:
                new, buys, _ = rebalance_capped(ranked, g["sector"], held, hold, buffer, cap)
            else:
                new, buys, _ = model.rebalance(ranked, held, hold, buffer)
            r = g["ret"].reindex(new).dropna()
            gross = (r.mean() if len(r) else 0.0) - cost * len(buys) / hold
            invest = bool(weather.get(month, True))
            rows[month + pd.offsets.MonthEnd(1)] = {name: gross if invest else cash_m, f"{name}_always": gross,
                                                    f"{name}_turnover": len(buys) / hold}
            held = new
        out[name] = pd.DataFrame(rows).T
    res = pd.concat([out[name] for name, *_ in variants], axis=1)
    months = res.index - pd.offsets.MonthEnd(1)
    spy_r = (spy_m.shift(-1) / spy_m - 1).reindex(months).to_numpy()
    invest = weather.reindex(months).fillna(True).astype(bool).to_numpy()
    res["SPY"] = spy_r
    res["SPY_weather"] = np.where(invest, spy_r, cash_m)
    res["average_stock"] = df.groupby("month")["ret"].mean().reindex(months).to_numpy()
    res["invested"] = invest
    return res.sort_index()


NAMES = {"mix": "Top ideas + weather filter", "mix_always": "Top ideas, always invested",
         "mix_capped": "Top ideas, max 5/industry + weather", "mix_capped_always": "Top ideas, max 5/industry, always in",
         "industry_mix": "Industry top ideas + weather", "industry_mix_always": "Industry top ideas, always invested",
         "industry_mix_capped": "Industry ideas, max 5/industry + weather",
         "industry_mix_capped_always": "Industry ideas, max 5/industry, always in",
         "SPY": "S&P 500 (buy & hold)", "SPY_weather": "S&P 500 + weather filter",
         "average_stock": "Average stock (equal weight)"}


def save_backtest(con: sqlite3.Connection, monthly: pd.DataFrame, hold: int) -> None:
    """Keep the headline numbers of the portfolio test for the reliability note."""
    from trend_bot import model

    base = "industry_mix" if method(con) == "industry_mix" else "mix"
    # The industry cap is used only if it gave more return per unit of risk in both halves of the
    # test (compared always invested, so the weather filter doesn't blur it).
    mid = monthly.index[len(monthly) // 2]
    rpr = lambda part, c: model.summarize(part, [c]).loc[c, "return_per_risk"] if c in part else np.nan
    halves = [monthly[monthly.index < mid], monthly[monthly.index >= mid]]
    cap = f"{base}_capped_always" in monthly and all(
        len(h) > 12 and rpr(h, f"{base}_capped_always") > rpr(h, f"{base}_always") for h in halves)
    col = f"{base}_capped" if cap else base
    s = model.summarize(monthly, [col, "SPY"])
    if col not in s.index or "SPY" not in s.index:
        return
    set_meta(con, "ideas_backtest", json.dumps({
        "hold": hold, "from": f"{monthly.index.min():%Y}", "to": f"{monthly.index.max():%Y}", "cap": bool(cap),
        "cagr": float(s.loc[col, "cagr"]), "max_drawdown": float(s.loc[col, "max_drawdown"]),
        "spy_cagr": float(s.loc["SPY", "cagr"]), "spy_max_drawdown": float(s.loc["SPY", "max_drawdown"])}))
    con.commit()


def backtest_note(con: sqlite3.Connection) -> str:
    raw = get_meta(con, "ideas_backtest")
    if not raw:
        return ""
    b = json.loads(raw)
    capped = f", at most {MAX_PER_INDUSTRY} per industry," if b.get("cap") else ""
    return (f"Holding the top {b['hold']}{capped} with the weather filter, {b['from']}-{b['to']}: {b['cagr']:+.1%} a year "
            f"(worst drop {b['max_drawdown']:.0%}) vs the S&P 500's {b['spy_cagr']:+.1%} "
            f"(worst drop {b['spy_max_drawdown']:.0%}).")


# --- The portfolio to follow: monthly buys and sells -------------------------------------------------

def update_portfolio(con: sqlite3.Connection, ranked: pd.DataFrame, date: str, weather: dict,
                     hold: int = 20, buffer: int = 40) -> dict:
    """Apply this month's ranking to the held stocks, with the same rule study ideas tests.

    Buy stocks that enter the top `hold`; sell only those that drop out of the top `buffer`
    (fewer trades); keep the rest. Saved for the monthly Discord message.
    """
    from trend_bot import model

    held = [t for (t,) in con.execute("SELECT ticker FROM ideas_holdings ORDER BY rank")]
    old = {t: (since, price) for t, since, price in
           con.execute("SELECT ticker, since, entry_price FROM ideas_holdings")}
    if use_cap(con):
        new, buys, sells = rebalance_capped(ranked["score"], ranked["sector"], held, hold, buffer)
    else:
        new, buys, sells = model.rebalance(ranked["score"], held, hold, buffer)
    rank_of = {t: i for i, t in enumerate(ranked.index, 1)}
    sold = []
    for t in sells:
        since, price = old[t]
        now = ranked["close"].get(t)
        if now is None or pd.isna(now):  # no longer ranked (e.g. delisted): use its last close
            row = con.execute("SELECT close FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 1", (t,)).fetchone()
            now = row[0] if row else None
        sold.append({"ticker": t, "since": since, "rank": rank_of.get(t),
                     "return": (now / price - 1) if now and price else None})
    con.execute("DELETE FROM ideas_holdings")
    for i, t in enumerate(new, 1):
        r = ranked.loc[t]
        since, price = old.get(t, (date, float(r["close"])))
        con.execute("INSERT INTO ideas_holdings VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (t, i, since, price, float(r["score"]),
                     r["sector"] if isinstance(r["sector"], str) else None, r["why"]))
    con.executemany("INSERT OR REPLACE INTO ideas_periods VALUES (?, ?, ?)",
                    [(date, t, int(t in buys)) for t in new])
    item = lambda t: {"ticker": t, "close": float(ranked.loc[t, "close"]), "rank": rank_of[t],
                      "sector": ranked.loc[t, "sector"] if isinstance(ranked.loc[t, "sector"], str) else None,
                      "why": ranked.loc[t, "why"]}
    res = {"date": date, "invest": bool(weather.get("invest", True)), "first": not held,
           "buys": [item(t) for t in buys], "sells": sold,
           "holds": [item(t) for t in new if t not in buys]}
    set_meta(con, "ideas_portfolio", json.dumps(res))
    con.commit()
    return res


def load_portfolio(con: sqlite3.Connection) -> dict | None:
    raw = get_meta(con, "ideas_portfolio")
    return json.loads(raw) if raw else None


def use_cap(con: sqlite3.Connection) -> bool:
    """True if the last portfolio test chose the industry cap (better return per risk in both halves)."""
    raw = get_meta(con, "ideas_backtest")
    return bool(json.loads(raw).get("cap")) if raw else False


def live_record(con: sqlite3.Connection, benchmark: str = "SPY", cost_bps: float = 10) -> dict | None:
    """How the portfolio to follow has really done since its first monthly update, vs the S&P 500.

    Equal amounts in each holding, re-balanced at each monthly update, 0.1% per trade each way
    (sells and buys), always invested. Prices come from the database, so splits and dividends are
    handled the same way as everywhere else. None until there's a first update.
    """
    from trend_bot.db import load_many

    periods = pd.read_sql_query("SELECT * FROM ideas_periods ORDER BY start", con)
    if periods.empty:
        return None
    starts = sorted(periods["start"].unique())
    tickers = sorted(set(periods["ticker"]) | {benchmark})
    closes = {t: df["Close"] for t, df in load_many(con, tickers, start=starts[0]).items()}
    if benchmark not in closes:
        return None
    end = closes[benchmark].index[-1]

    def change(t: str, a: str, b: pd.Timestamp | None) -> float | None:
        c = closes.get(t)
        if c is None or c.empty:
            return None
        i = c.index.searchsorted(pd.Timestamp(a))
        if i >= len(c):
            return None
        last = c.loc[:b] if b is not None else c
        return float(last.iloc[-1] / c.iloc[i] - 1) if len(last) and last.index[-1] >= c.index[i] else 0.0

    value, cost = 1.0, cost_bps / 10_000
    for k, start in enumerate(starts):
        nxt = pd.Timestamp(starts[k + 1]) if k + 1 < len(starts) else None
        part = periods[periods["start"] == start]
        rets = [r for r in (change(t, start, nxt) for t in part["ticker"]) if r is not None]
        sold_and_bought = part["bought"].sum() / max(len(part), 1)
        trading = cost * (1 if k == 0 else 2) * sold_and_bought  # buying in; later, a sale for each buy
        value *= (1 + (np.mean(rets) if rets else 0.0)) * (1 - trading)
    spy = change(benchmark, starts[0], None)
    return {"start": starts[0], "as_of": f"{end:%Y-%m-%d}", "portfolio": value - 1, "spy": spy,
            "days": (end - pd.Timestamp(starts[0])).days, "updates": len(starts)}


def live_line(con: sqlite3.Connection) -> str:
    """One line for the messages: the portfolio's real record so far vs the S&P 500."""
    r = live_record(con)
    if not r or r["spy"] is None:
        return ""
    if r["days"] < 1:
        return f"📈 Live record starts today ({r['start']}); it's compared with the S&P 500 from here on."
    ahead = r["portfolio"] - r["spy"]
    return (f"📈 **Since {r['start']}** ({r['days']} days, {r['updates']} monthly update"
            f"{'s' if r['updates'] != 1 else ''}): portfolio **{r['portfolio']:+.1%}** vs S&P 500 "
            f"**{r['spy']:+.1%}** ({'ahead' if ahead >= 0 else 'behind'} by {abs(ahead):.1%}; equal amounts, "
            "always invested, after trading costs).")
