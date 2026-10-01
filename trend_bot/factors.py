"""Factor study: did each fundamental signal predict next month's returns?

Every month-end since XBRL data starts (~2009), the ~1,000 most traded stocks
(over $5, no funds) are ranked by each signal, using only financials filed by
then. Two scores per signal:

- IC (information coefficient): the rank correlation between the signal and
  next month's return. Professionals consider an average IC of 0.02-0.05 useful.
- Top-minus-bottom: return of the best 10% minus the worst 10% next month.

Both are reported for 2009-2017 and 2018-now separately: a real signal should
show up in both.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from trend_bot import earnings, fundamentals, model, short_interest
from trend_bot.company_info import fund_tickers


def primary_tickers(con: sqlite3.Connection) -> dict[int, str]:
    """One ticker per company (its most traded share class), funds left out."""
    funds = fund_tickers(con)
    out: dict[int, str] = {}
    for cik, t in con.execute("""SELECT cik, ticker FROM tickers WHERE cik IS NOT NULL AND last_date IS NOT NULL
                                 ORDER BY COALESCE(dollar_vol, 0) DESC"""):
        if t not in funds:
            out.setdefault(cik, t)
    return out


def raw_close_monthly(con: sqlite3.Connection, tickers: list[str], chunk: int = 400) -> pd.DataFrame:
    """Month-end closes adjusted for splits but not dividends (for market values)."""
    cols = {}
    for i in range(0, len(tickers), chunk):
        part = tickers[i : i + chunk]
        df = pd.read_sql_query(
            f"SELECT ticker, date, close FROM prices WHERE ticker IN ({','.join('?' * len(part))})",
            con, params=part, parse_dates=["date"])
        for t, g in df.groupby("ticker"):
            s = g.set_index("date")["close"].sort_index()
            cols[t] = model._monthly_last(s)
    return pd.DataFrame(cols)


def market_caps(close: pd.Series, shares: pd.Series, shares_date: pd.Series, splits: pd.DataFrame,
                tickers: dict[int, str]) -> pd.Series:
    """Market value per company: split-adjusted price x shares x splits since the share count was reported.

    (Yahoo's prices are adjusted for every later split, so multiplying by the splits after the
    share-count date turns them back into the real price per share at that count.)
    """
    out = {}
    by_ticker = {t: g for t, g in splits.groupby("ticker")} if len(splits) else {}
    for cik, n in shares.dropna().items():
        t = tickers.get(cik)
        if t is None or t not in close.index or pd.isna(close[t]):
            continue
        factor = 1.0
        s = by_ticker.get(t)
        if s is not None:
            for d, r in zip(pd.to_datetime(s["date"]), s["ratio"]):
                if d > shares_date[cik]:
                    factor *= r
        out[cik] = close[t] * n * factor
    return pd.Series(out, dtype=float)


# Return in the month a company disappeared (no price data), by how it ended. Bought-out
# companies get the average stock's return that month (neutral; the takeover premium
# usually came earlier). -30% for other delistings is the research average (Shumway 1997).
GONE_RETURNS = {"bankrupt": -1.0, "delisted": -0.30}


def gone_companies(con: sqlite3.Connection, priced: set[int]) -> pd.DataFrame:
    """Companies with no price data that went bankrupt, were bought out or delisted: [status, date] by cik."""
    df = pd.read_sql_query("""SELECT cik, status, fate_date FROM company_fates
                              WHERE status IN ('bankrupt', 'acquired', 'delisted') AND fate_date IS NOT NULL""",
                           con, parse_dates=["fate_date"])
    return df[~df["cik"].isin(priced)].set_index("cik")


def monthly_factors(con: sqlite3.Connection, since: str = "2009-06-30", universe: int = 1000,
                    min_price: float = 5, panel: dict[str, pd.DataFrame] | None = None,
                    log=print, extras: bool = False, include_latest: bool = False) -> pd.DataFrame:
    """One row per (month, stock): every factor and next month's return.

    extras: also the price features, last month's return and market value (for the ML model).
    include_latest: also the latest month, whose next-month return isn't known yet (ret = NaN).

    Survivorship: price data only exists for companies listed today. Companies that were big
    enough for the universe (public float at least the universe's 10th percentile market value)
    and then went bankrupt, were bought out or delisted are put back for the month they
    disappeared, with the return in GONE_RETURNS. Their price-based signals are unknown (NaN).
    Column "gone" holds how they ended (empty for listed companies).
    """
    facts = fundamentals.load_facts(con)
    if facts.empty:
        return pd.DataFrame()
    tickers = primary_tickers(con)
    panel = panel or model.monthly_panel(con)
    raw = raw_close_monthly(con, list(tickers.values()))
    splits = pd.read_sql_query("SELECT * FROM splits", con)
    log("[factors] working out quarterly earnings surprises...")
    ev = earnings.events(con, facts, tickers)
    si = short_interest.load_short_interest(con)
    sv = pd.read_sql_query("SELECT * FROM short_volume", con)
    nxt = panel["close"].shift(-1) / panel["close"] - 1
    last1 = panel["close"] / panel["close"].shift(1) - 1
    month_ends = panel["close"].index
    months = [m for m in month_ends[: None if include_latest else -1] if m >= pd.Timestamp(since)]
    gone = gone_companies(con, set(tickers))
    rows = []
    for k, month in enumerate(months):
        feat = pd.DataFrame({f: panel[f].loc[month] for f in model.FEATURES})
        feat = feat[model.eligible(feat, universe, min_price)]
        f_all = fundamentals.as_of(facts, month)
        if f_all.empty or "shares" not in f_all:
            continue
        f = f_all[f_all.index.isin(list(tickers))]
        caps = market_caps(raw.loc[month] if month in raw.index else pd.Series(dtype=float),
                           f["shares"], f["shares_date"], splits, tickers)
        vals = fundamentals.factor_values(f, caps).join(earnings.latest(ev, month), how="left")
        if extras:
            vals["market_cap"] = caps
        vals.index = [tickers[c] for c in vals.index]
        if len(si) or len(sv):
            by_t = lambda col: pd.Series(f[col].to_numpy(), index=[tickers[c] for c in f.index])
            short = short_interest.factor_values(si, sv, month, by_t("shares"), by_t("shares_date"), splits)
            vals = vals.join(short, how="left")
        vals = vals[vals.index.isin(feat.index)]
        if extras:
            vals = vals.join(feat, how="left")   # close is kept for reference, not used as an input
            vals["mom1"] = last1.loc[month, vals.index]
        vals["ret"] = nxt.loc[month, vals.index]
        vals["month"] = month
        if not (include_latest and month == month_ends[-1]):
            vals = vals.dropna(subset=["ret"])
            caps_t = pd.Series(caps.to_numpy(), index=[tickers[c] for c in caps.index])
            dead = _gone_rows(gone, f_all, ev, month, month_ends, caps_t.reindex(vals.index).quantile(0.1),
                              vals["ret"].mean())
            if len(dead):
                vals = pd.concat([vals, dead])
        rows.append(vals.rename_axis("ticker").reset_index())
        if (k + 1) % 24 == 0:
            log(f"[factors] {k + 1}/{len(months)} months")
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if len(out) and "gone" not in out:
        out["gone"] = np.nan
    return out


def _gone_rows(gone: pd.DataFrame, f_all: pd.DataFrame, ev: pd.DataFrame, month: pd.Timestamp,
               month_ends: pd.DatetimeIndex, min_float: float, average: float) -> pd.DataFrame:
    """Rows for companies that disappeared during the month after `month`."""
    i = month_ends.get_loc(month)
    if i + 1 >= len(month_ends) or gone.empty or "public_float" not in f_all or pd.isna(min_float):
        return pd.DataFrame()
    g = gone[(gone["fate_date"] > month) & (gone["fate_date"] <= month_ends[i + 1])]
    fa = f_all.loc[f_all.index.intersection(g.index)]
    fa = fa[fa["public_float"] >= min_float]
    if fa.empty:
        return pd.DataFrame()
    out = fundamentals.factor_values(fa, pd.Series(dtype=float)).join(earnings.latest(ev, month), how="left")
    status = g.loc[out.index, "status"]
    out["ret"] = status.map(GONE_RETURNS).fillna(average).to_numpy()
    out["gone"] = status.to_numpy()
    out["month"] = month
    out.index = [f"CIK{c}" for c in out.index]
    return out


def composite(df: pd.DataFrame, names: list[str]) -> pd.Series:
    """Average of each factor's within-month rank, flipped so higher is always 'better'."""
    ranks = []
    for n in names:
        r = df.groupby("month")[n].rank(pct=True)
        ranks.append(r if fundamentals.EXPECTED[n] > 0 else 1 - r)
    return pd.concat(ranks, axis=1).mean(axis=1, skipna=True)


def evaluate(df: pd.DataFrame, factor: str, sign: int = 1, min_stocks: int = 50) -> dict:
    """Monthly IC and top-minus-bottom 10% spread for one factor (sign flips 'lower is better')."""
    ics, spreads, counts = [], [], []
    for _, g in df[["month", factor, "ret"]].dropna().groupby("month"):
        if len(g) < min_stocks or g[factor].nunique() < 2:
            continue
        x = g[factor] * sign
        ics.append(x.rank().corr(g["ret"].rank()))
        k = max(1, len(g) // 10)
        order = g.assign(x=x).sort_values("x")
        spreads.append(order["ret"].tail(k).mean() - order["ret"].head(k).mean())
        counts.append(len(g))
    ic = pd.Series(ics, dtype=float)
    n = len(ic)
    return {
        "months": n, "stocks": float(np.mean(counts)) if counts else np.nan,
        "mean_ic": ic.mean(), "ic_t": ic.mean() / ic.std() * np.sqrt(n) if n > 2 and ic.std() > 0 else np.nan,
        "ic_positive": (ic > 0).mean() if n else np.nan,
        "top_minus_bottom": float(np.mean(spreads)) if spreads else np.nan,
    }


QUALITY_VALUE = ["gross_profitability", "earnings_yield", "book_to_market", "accruals", "asset_growth"]
EARNINGS = ["sue", "revenue_sue", "ear"]
SHORT = ["short_ratio", "days_to_cover", "short_change"]
COMBOS = {"quality_value_combo": QUALITY_VALUE, "earnings_combo": EARNINGS, "short_combo": SHORT}


def study(df: pd.DataFrame, split_year: int = 2018) -> dict[str, pd.DataFrame]:
    df = df.copy()
    for name, parts in COMBOS.items():
        df[name] = composite(df, [p for p in parts if p in df]) if any(p in df for p in parts) else np.nan
    periods = {"all": df, f"before {split_year}": df[df["month"].dt.year < split_year],
               f"{split_year} on": df[df["month"].dt.year >= split_year]}
    names = [n for n in list(fundamentals.EXPECTED) + list(COMBOS) if n in df]
    out = {}
    for label, part in periods.items():
        rows = {}
        for n in names:
            sign = fundamentals.EXPECTED.get(n, 1)
            rows[n] = evaluate(part, n, sign)
        out[label] = pd.DataFrame(rows).T
    if "gone" in df and df["gone"].notna().any():
        listed = df[df["gone"].isna()]
        cmp = {}
        for n in names:
            sign = fundamentals.EXPECTED.get(n, 1)
            a, b = evaluate(listed, n, sign), out["all"].loc[n]
            cmp[n] = {"ic_listed_only": a["mean_ic"], "ic_with_gone": b["mean_ic"],
                      "tmb_listed_only": a["top_minus_bottom"], "tmb_with_gone": b["top_minus_bottom"]}
        out[GONE_TABLE] = pd.DataFrame(cmp).T
    return out


GONE_TABLE = "listed today only vs with companies that disappeared (all years)"
