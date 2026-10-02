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

import hashlib
import sqlite3

import numpy as np
import pandas as pd

from trend_bot import earnings, events, fundamentals, insiders, model, sectors, short_interest, text_changes
from trend_bot.db import get_meta
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


def net_issuance(f: pd.DataFrame, splits: pd.DataFrame, tickers: dict[int, str]) -> pd.Series:
    """log(shares now / shares a year earlier), with stock splits in between taken out.

    Positive: the company issued shares (deals, stock pay, raising money); negative: it bought
    shares back. Moves of more than 10x either way are treated as data errors."""
    if "shares_prior" not in f or "shares_prior_date" not in f:
        return pd.Series(np.nan, index=f.index)
    by_ticker = {t: g for t, g in splits.groupby("ticker")} if len(splits) else {}
    out = {}
    for cik, r in f[["shares", "shares_prior", "shares_date", "shares_prior_date"]].dropna().iterrows():
        if r["shares"] <= 0 or r["shares_prior"] <= 0:
            continue
        factor = 1.0
        s = by_ticker.get(tickers.get(cik))
        if s is not None:
            for d, ratio in zip(pd.to_datetime(s["date"]), s["ratio"]):
                if r["shares_prior_date"] < d <= r["shares_date"]:
                    factor *= ratio
        x = np.log(r["shares"] / (r["shares_prior"] * factor))
        if abs(x) <= np.log(10):
            out[cik] = x
    return pd.Series(out, dtype=float).reindex(f.index)


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
    industry = sectors.ticker_sectors(con)
    report_changes = text_changes.changes(con)
    red_flags = events.load(con)
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
        vals["net_issuance"] = net_issuance(f, splits, tickers)
        for sig in text_changes.SIGNALS:
            vals[sig] = text_changes.latest(report_changes, month, column=sig).reindex(vals.index)
        # No event in the window means 0 events, not unknown.
        if len(red_flags):
            vals = vals.join(events.counts(red_flags, month), how="left").fillna({s: 0 for s in events.SIGNALS})
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
        vals["sector"] = industry.reindex(vals.index).to_numpy()
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


MIN_PEERS = 10  # fewer companies than this in an industry that month: compare with the whole market


def group_rank(df: pd.DataFrame, col: str, by_sector: bool = False) -> pd.Series:
    """0-1 rank within each month; with by_sector, within the company's industry that month
    (falling back to the whole market when the industry is unknown or has under MIN_PEERS companies)."""
    r = df.groupby("month")[col].rank(pct=True)
    if not by_sector or "sector" not in df or df["sector"].isna().all():
        return r
    keys = [df["month"], df["sector"]]
    rs = df[col].groupby(keys).rank(pct=True)
    n = df[col].groupby(keys).transform("count")
    return rs.where((n >= MIN_PEERS) & df["sector"].notna(), r)


# --- Build the table once, reuse it --------------------------------------------------------------

TABLE_START = "2009-06-30"  # XBRL filings start in 2009
TABLE_VERSION = 5           # bump when monthly_factors changes what it computes


def _table_key(con: sqlite3.Connection, universe: int, min_price: float) -> str:
    """Changes whenever the data the table is built from changes."""
    q = lambda sql: con.execute(sql).fetchone()[0]
    parts = [TABLE_VERSION, con.execute("PRAGMA database_list").fetchone()[2],
             q("SELECT MAX(last_date) FROM tickers"), q("SELECT COUNT(*) FROM tickers WHERE last_date IS NOT NULL"),
             q("SELECT COUNT(*) FROM tickers WHERE sic IS NOT NULL"), get_meta(con, "facts_updated"),
             q("SELECT COUNT(*) FROM short_interest_files"), get_meta(con, "short_volume_last"),
             q("SELECT COUNT(*) FROM company_fates WHERE status IN ('bankrupt', 'acquired', 'delisted')"),
             q("SELECT COUNT(*) FROM doc_vectors"), q("SELECT SUM(tone_version) FROM doc_vectors"), q("SELECT COUNT(*) FROM events"),
             universe, min_price]
    return hashlib.sha1(repr(parts).encode()).hexdigest()[:16]


def table(con: sqlite3.Connection, since: str = TABLE_START, universe: int = 1000, min_price: float = 5,
          log=print, build: bool = True) -> pd.DataFrame | None:
    """monthly_factors with every column and the latest month, built once per data version.

    The monthly cloud step runs several studies on the same table (factors, ml, ideas); building it
    takes ~10 minutes, so it's saved and reused until prices, financials, short data, fates or
    industry codes change. build=False: return None instead of building it.
    """
    folder = insiders.CACHE_DIR.parent / "factors"
    path = folder / f"table_{_table_key(con, universe, min_price)}.pkl"
    if path.exists():
        log("[factors] reusing the monthly table built earlier from the same data")
        df = pd.read_pickle(path)
    elif not build:
        return None
    else:
        df = monthly_factors(con, since=TABLE_START, universe=universe, min_price=min_price, log=log,
                             extras=True, include_latest=True)
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("table_*.pkl"):
            old.unlink()
        tmp = path.with_suffix(".tmp")
        df.to_pickle(tmp)
        tmp.replace(path)
    return df[df["month"] >= pd.Timestamp(since)].reset_index(drop=True) if len(df) else df


def composite(df: pd.DataFrame, names: list[str], by_sector: bool = False) -> pd.Series:
    """Average of each factor's within-month rank, flipped so higher is always 'better'."""
    ranks = []
    for n in names:
        r = group_rank(df, n, by_sector)
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
    if "sector" in df and df["sector"].notna().any():
        cmp = {}
        for n in [n for n in fundamentals.EXPECTED if n in df]:
            sign = fundamentals.EXPECTED[n]
            a = out["all"].loc[n]
            b = evaluate(df.assign(**{n: group_rank(df, n, by_sector=True)}), n, sign)
            cmp[n] = {"ic_whole_market": a["mean_ic"], "ic_within_industry": b["mean_ic"],
                      "t_within_industry": b["ic_t"], "tmb_within_industry": b["top_minus_bottom"]}
        out[INDUSTRY_TABLE] = pd.DataFrame(cmp).T
    return out


INDUSTRY_TABLE = "ranked against the whole market vs within each industry (all years)"


GONE_TABLE = "listed today only vs with companies that disappeared (all years)"
