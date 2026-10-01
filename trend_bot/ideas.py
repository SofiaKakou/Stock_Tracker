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
}


def oriented_ranks(df: pd.DataFrame, by_sector: bool = False) -> pd.DataFrame:
    """Each signal as a 0-1 rank within the month (or industry), flipped so 1 is always 'good'."""
    out = {}
    for c in ml.SIGNALS:
        if c in df and df[c].notna().any():
            r = factors.group_rank(df, c, by_sector)
            out[c] = r if fundamentals.EXPECTED[c] > 0 else 1 - r
    out["mom12"] = df.groupby("month")["mom12"].rank(pct=True)
    return pd.DataFrame(out, index=df.index)


def reasons(ranks: pd.Series, best: bool = True, n: int = 3) -> str:
    """The strongest few signals behind a stock's place on the list, in plain words."""
    r = ranks.dropna().sort_values(ascending=not best)
    r = r[r >= 0.8] if best else r[r <= 0.2]
    words = GOOD if best else BAD
    return ", ".join(words.get(k, k) for k in r.index[:n]) or "a bit of everything"


def rank_month(df: pd.DataFrame, by_sector: bool = False) -> pd.DataFrame:
    """Score every stock in the table's latest month; best first. Columns: score, pct, close, sector, why."""
    df = df[df["gone"].isna()] if "gone" in df else df
    df = df.assign(**{c: np.nan for c in ml.FEATURES if c not in df})  # e.g. no short data loaded yet
    month = df["month"].max()
    now = df[df["month"] == month].copy()
    ranks = oriented_ranks(now, by_sector)
    now["score"] = ml.simple_mix(now, by_sector)
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
    df = factors.monthly_factors(con, since=since, universe=universe, log=log, extras=True, include_latest=True)
    if df.empty:
        return pd.DataFrame(), ""
    date = con.execute("SELECT MAX(date) FROM prices WHERE ticker IN (SELECT ticker FROM tickers "
                       "WHERE last_date IS NOT NULL)").fetchone()[0][:10]
    return rank_month(df, by_sector=method(con) == "industry_mix"), date


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
    out = {"method": method, "from": f"{preds['month'].min():%Y}", "to": f"{preds['month'].max():%Y}"}
    a = card["all"].loc[method]
    for k in ("months", "mean_ic", "ic_t", "ic_positive", "top10_per_year", "average_stock_per_year"):
        out[k] = float(a[k]) if k in a and pd.notna(a[k]) else None
    out["halves_positive"] = all(ic(method, h) > 0 for h in halves)
    out["other_ic"] = float(card["all"].loc["simple_mix" if better else "industry_mix", "mean_ic"]) \
        if "industry_mix" in card["all"].index else None
    set_meta(con, "mix_scorecard", json.dumps(out))
    con.commit()


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
    months = f", {s['months']:.0f} months" if s.get("months") else ""
    return (f"{how}Tested {s['from']}-{s['to']}{months} (nothing is fitted, so there's no hindsight in how it adds up the "
            f"signals): the top 10% returned "
            f"{s['top10_per_year']:+.1%} a year after costs vs {s['average_stock_per_year']:+.1%} for the average "
            f"stock, and the ranking pointed the right way in {s['ic_positive']:.0%} of months, {steady}. "
            f"{backtest_note(con) + ' ' if backtest_note(con) else ''}"
            "A small edge on average in the past, not a promise for any one stock.")


# --- Would it have made money? A portfolio test ---------------------------------------------------

def scores(df: pd.DataFrame, by_sector: bool = False) -> pd.Series:
    """The mix score for every row of a monthly table (NaN where too few signals are known)."""
    df = df.assign(**{c: np.nan for c in ml.FEATURES if c not in df})
    known = oriented_ranks(df, by_sector).notna().sum(axis=1)
    return ml.simple_mix(df, by_sector).where(known >= MIN_SIGNALS)


def backtest(df: pd.DataFrame, spy: pd.Series, hold: int = 20, buffer: int = 40, cost_bps: float = 10,
             cash_rate: float = 2.0) -> pd.DataFrame:
    """Monthly returns of holding the top `hold` ideas (equal weight), for both mixes, with and without
    the weather filter (cash when the S&P 500 is below its 200-day average), against SPY.

    A holding is kept while it stays in the top `buffer` (fewer trades). 0.1% per trade each way.
    Companies that disappeared are included for their last month, at their fate's return.
    """
    from trend_bot import model

    df = df.dropna(subset=["ret"]).reset_index(drop=True)
    weather = model._monthly_last(model.spy_weather(spy))
    spy_m = model._monthly_last(spy)
    cash_m = (1 + cash_rate / 100) ** (1 / 12) - 1
    cost = cost_bps / 10_000 * 2
    out = {}
    for name, by_sector in (("mix", False), ("industry_mix", True)):
        s = scores(df, by_sector)
        held: list[str] = []
        rows = {}
        for month, g in df.assign(score=s).dropna(subset=["score"]).groupby("month"):
            if len(g) < hold * 3:
                continue
            ranked = g.set_index("ticker")["score"].sort_values(ascending=False)
            new, buys, _ = model.rebalance(ranked, held, hold, buffer)
            r = g.set_index("ticker")["ret"].reindex(new).dropna()
            gross = (r.mean() if len(r) else 0.0) - cost * len(buys) / hold
            invest = bool(weather.get(month, True))
            rows[month + pd.offsets.MonthEnd(1)] = {name: gross if invest else cash_m, f"{name}_always": gross,
                                                    f"{name}_turnover": len(buys) / hold}
            held = new
        out[name] = pd.DataFrame(rows).T
    res = out["mix"].join(out["industry_mix"], how="outer")
    months = res.index - pd.offsets.MonthEnd(1)
    spy_r = (spy_m.shift(-1) / spy_m - 1).reindex(months).to_numpy()
    invest = weather.reindex(months).fillna(True).astype(bool).to_numpy()
    res["SPY"] = spy_r
    res["SPY_weather"] = np.where(invest, spy_r, cash_m)
    res["average_stock"] = df.groupby("month")["ret"].mean().reindex(months).to_numpy()
    res["invested"] = invest
    return res.sort_index()


NAMES = {"mix": "Top ideas + weather filter", "mix_always": "Top ideas, always invested",
         "industry_mix": "Industry top ideas + weather", "industry_mix_always": "Industry top ideas, always invested",
         "SPY": "S&P 500 (buy & hold)", "SPY_weather": "S&P 500 + weather filter",
         "average_stock": "Average stock (equal weight)"}


def save_backtest(con: sqlite3.Connection, monthly: pd.DataFrame, hold: int) -> None:
    """Keep the headline numbers of the portfolio test for the reliability note."""
    from trend_bot import model

    col = "industry_mix" if method(con) == "industry_mix" else "mix"
    s = model.summarize(monthly, [col, "SPY"])
    if col not in s.index or "SPY" not in s.index:
        return
    set_meta(con, "ideas_backtest", json.dumps({
        "hold": hold, "from": f"{monthly.index.min():%Y}", "to": f"{monthly.index.max():%Y}",
        "cagr": float(s.loc[col, "cagr"]), "max_drawdown": float(s.loc[col, "max_drawdown"]),
        "spy_cagr": float(s.loc["SPY", "cagr"]), "spy_max_drawdown": float(s.loc["SPY", "max_drawdown"])}))
    con.commit()


def backtest_note(con: sqlite3.Connection) -> str:
    raw = get_meta(con, "ideas_backtest")
    if not raw:
        return ""
    b = json.loads(raw)
    return (f"Holding the top {b['hold']} with the weather filter, {b['from']}-{b['to']}: {b['cagr']:+.1%} a year "
            f"(worst drop {b['max_drawdown']:.0%}) vs the S&P 500's {b['spy_cagr']:+.1%} "
            f"(worst drop {b['spy_max_drawdown']:.0%}).")
