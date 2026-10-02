"""The study command: every research test (insiders, trend, momentum, model, forecast, factors, ml, ideas)."""

from __future__ import annotations

import argparse
from contextlib import closing

import pandas as pd

from trend_bot.commands.common import (
    build_strategy,
)


def _print_fates(con, study, events: pd.DataFrame, ev: pd.DataFrame, args: argparse.Namespace, pct) -> None:
    """Results again with delisted companies put back (survivorship-bias correction)."""
    gone = study.add_fates(con, events, ev, benchmark=args.benchmark)
    if gone.empty:
        return
    n = gone["status"].value_counts()
    print(f"\n== Putting back companies that no longer exist: {len(gone):,} events had no price data ==")
    print(f"   bankrupt {n.get('bankrupt', 0):,}, bought out {n.get('acquired', 0):,}, "
          f"delisted for other reasons {n.get('delisted', 0):,}, unclear {n.get('unknown', 0):,}, "
          f"not checked yet {n.get('unchecked', 0):,}")
    if n.get("unchecked", 0):
        print("   (run 'python -m trend_bot db update --insiders-only' to check the rest - it also runs nightly)")
    print("   Bankruptcies count as -100%, buyouts as matching the benchmark; the rest stay out.")
    both = pd.concat([ev, gone])
    print("\nAll clusters, bankruptcies included:\n" + pct(study.summarize(both, min_price=args.min_price)))
    worst = pd.concat([ev[ev["trend"] == "UP"], gone[gone["status"] == "bankrupt"]])
    print("\nTrend UP, WORST CASE (as if every bankruptcy above had been in an uptrend):\n"
          + pct(study.summarize(worst, min_price=args.min_price)))


def _study_factors(con, args: argparse.Namespace) -> None:
    from trend_bot import factors, fundamentals

    if not con.execute("SELECT 1 FROM facts LIMIT 1").fetchone():
        print("No company financials yet. Run:  python -m trend_bot db update --fundamentals-only")
        return
    since = args.since if args.since != "2006-01-01" else "2009-06-30"  # XBRL filings start in 2009
    print("Ranking stocks by each signal at every month-end, using only financials filed by then...")
    df = factors.table(con, since=since, universe=args.universe)
    if df.empty:
        print("Not enough data.")
        return
    tables = factors.study(df, split_year=args.split_year)
    fmt = {"months": "{:.0f}".format, "stocks": "{:.0f}".format, "mean_ic": "{:+.3f}".format,
           "ic_t": "{:+.1f}".format, "ic_positive": "{:.0%}".format, "top_minus_bottom": "{:+.2%}".format,
           "ic_listed_only": "{:+.3f}".format, "ic_with_gone": "{:+.3f}".format,
           "tmb_listed_only": "{:+.2%}".format, "tmb_with_gone": "{:+.2%}".format,
           "ic_whole_market": "{:+.3f}".format, "ic_within_industry": "{:+.3f}".format,
           "t_within_industry": "{:+.1f}".format, "tmb_within_industry": "{:+.2%}".format}
    print(_gone_note(df))
    print(f"\n{df['month'].min():%Y-%m} to {df['month'].max():%Y-%m}, up to {args.universe:,} most traded stocks a month")
    for label, table in tables.items():
        print(f"\n== {label} ==\n" + table.to_string(formatters=fmt))
    print("\nmean_ic: rank correlation with next month's return (0.02-0.05 is useful, signs already flipped so"
          " positive = worked as expected). ic_t: above about 2 means unlikely to be luck. top_minus_bottom:"
          " best 10% minus worst 10%, per month.")
    print("\nWhat each signal is:")
    for n, d in fundamentals.DESCRIPTIONS.items():
        print(f"  {n:<20} {d}")
    for name, parts in factors.COMBOS.items():
        print(f"  {name:<20} average rank of {', '.join(parts)}")


def _gone_note(df: pd.DataFrame) -> str:
    """One line on the companies that disappeared and were put back into the study."""
    gone = df["gone"].dropna() if "gone" in df else pd.Series(dtype=str)
    if gone.empty:
        return ("Note: only companies listed today have prices, so ones that went bankrupt or were bought out "
                "are missing (their fates are looked up a little each night; results improve as that fills in).")
    c = gone.value_counts()
    return (f"Companies that disappeared, put back for their last month: {c.get('bankrupt', 0):,} bankrupt (-100%), "
            f"{c.get('delisted', 0):,} delisted (-30%), {c.get('acquired', 0):,} bought out (average stock). "
            "Price-based signals can't be measured for them.")


def _study_ml(con, args: argparse.Namespace) -> None:
    from trend_bot import ml, track

    if not con.execute("SELECT 1 FROM facts LIMIT 1").fetchone():
        print("No company financials yet. Run:  python -m trend_bot db update --fundamentals-only")
        return
    since = args.since if args.since != "2006-01-01" else "2009-06-30"  # XBRL filings start in 2009
    print("Building the monthly table of every signal (takes a while)...")
    df = ml.dataset(con, since=since, universe=args.universe, include_latest=True)
    if df.empty:
        print("Not enough data.")
        return
    print(_gone_note(df))
    first = max(args.first_year, df["month"].dt.year.min() + 2)
    preds = ml.walk_forward(df, first_year=first, trees=args.trees)
    if preds.empty:
        print("Not enough history before the first test year.")
        return
    card = ml.scorecard(preds, split_year=args.split_year)
    fmt = {"months": "{:.0f}".format, "stocks": "{:.0f}".format, "mean_ic": "{:+.3f}".format,
           "ic_t": "{:+.1f}".format, "ic_positive": "{:.0%}".format, "top_minus_bottom": "{:+.2%}".format,
           "top10_per_year": "{:+.1%}".format, "average_stock_per_year": "{:+.1%}".format,
           "turnover": "{:.0%}".format}
    print(f"\nWalk-forward test {preds['month'].min():%Y-%m} to {preds['month'].max():%Y-%m}: each year predicted "
          f"by a model trained only on earlier years ({len(preds):,} predictions).")
    for label, table in card.items():
        cols = [c for c in fmt if c in table]
        print(f"\n== {label} ==\n" + table[cols].to_string(formatters=fmt))
    print("\nmodel: the machine-learning model. simple_mix: equal-weight average rank of every signal plus momentum "
          "(no fitting). industry_mix: the same, with company signals ranked within each industry. "
          "..._plus: also low volatility and nearness to the 52-week high. "
          "quality_value / momentum: single-idea baselines.\n"
          "mean_ic: rank correlation with next month's return (0.02-0.05 is useful). ic_t above ~2: unlikely luck. "
          "top10_per_year: holding the top 10% each month, after 0.1% trading costs each way, vs average_stock "
          "(every stock in the universe, equal weight). Momentum can't be measured for companies that disappeared, "
          "so its numbers still leave them out.")
    passed, text = ml.verdict(card, args.split_year)
    print(f"\nVerdict: {text}")
    ranking, imp = ml.today(df, trees=args.trees)
    print("\nWhat the model leans on most (share of its total gain):")
    print("  " + ", ".join(f"{k} {v:.0%}" for k, v in imp.head(12).items()))
    # The last month in the table can be a partial one; the ranking uses its latest trading day.
    latest = con.execute("SELECT MAX(date) FROM prices WHERE ticker IN (SELECT ticker FROM tickers "
                         "WHERE last_date IS NOT NULL)").fetchone()[0][:10]
    print(f"\nRanking as of {latest} ({'model passed' if passed else 'model NOT passed - research only'}):")
    print("  Top 15:    " + ", ".join(ranking.index[:15]))
    print("  Bottom 15: " + ", ".join(ranking.index[-15:]))
    ml.save(con, ranking, latest, card, passed, text)
    from trend_bot import ideas

    ideas.save_scorecard(con, card, preds)
    print(f"Top ideas will use: {ideas.method(con)}{' + calm stocks and 52-week high' if ideas.extra(con) else ''} "
          "(each change only if it ranked better in both halves).")
    new = track.record_ml(con, ranking, latest)
    print(f"\nRecorded {len(new)} new picks (top and bottom 10%) for forward tracking: see 'python -m trend_bot track'.")


def _study_ideas(con, args: argparse.Namespace) -> None:
    from trend_bot import factors, ideas, model
    from trend_bot.db import load_prices

    if not con.execute("SELECT 1 FROM facts LIMIT 1").fetchone():
        print("No company financials yet. Run:  python -m trend_bot db update --fundamentals-only")
        return
    since = args.since if args.since != "2006-01-01" else "2009-06-30"  # XBRL filings start in 2009
    print("Building the monthly table of every signal (takes a while)...")
    df = factors.table(con, since=since, universe=args.universe)
    spy = load_prices(con, args.benchmark)["Close"]
    if df.empty or spy.empty:
        print("Not enough data.")
        return
    print(_gone_note(df))
    monthly = ideas.backtest(df, spy, hold=args.hold, buffer=args.buffer, cash_rate=args.cash_rate,
                             extra=ideas.extra(con))
    if monthly.empty:
        print("Not enough data.")
        return
    cols = [c for c in ideas.NAMES if c in monthly]
    fmt = {"cagr": "{:+.1%}".format, "volatility": "{:.0%}".format, "max_drawdown": "{:.0%}".format,
           "return_per_risk": "{:.2f}".format, "total": "{:+.0%}".format}
    print(f"\nTop {args.hold} ideas of the {args.universe:,} most traded stocks, equal weight, checked monthly, "
          f"kept while in the top {args.buffer}; 0.1% per trade; cash earns {args.cash_rate:g}%/yr when the weather "
          "filter says so (S&P 500 below its 200-day average).")
    split = pd.Timestamp(f"{args.split_year}-01-01")
    for label, part in (("All years", monthly), (f"before {args.split_year}", monthly[monthly.index < split]),
                        (f"{args.split_year} on", monthly[monthly.index >= split])):
        if part.empty:
            continue
        s = model.summarize(part, cols).rename(index=ideas.NAMES)
        print(f"\n== {label}: {part.index[0]:%Y-%m} to {part.index[-1]:%Y-%m} ==\n" + s.to_string(formatters=fmt))
    yearly = (1 + monthly[["mix", "mix_always", "mix_capped_always", "SPY"]]).groupby(monthly.index.year).prod(
        min_count=1) - 1
    print("\nYear by year:\n" + yearly.rename(columns=ideas.NAMES).rename_axis("year").to_string(
        float_format=lambda v: f"{v:+.0%}", na_rep="–"))
    beat = (yearly["mix"] > yearly["SPY"]).mean()
    print(f"\nIn the market {monthly['invested'].mean():.0%} of months; about "
          f"{monthly['mix_turnover'].mean():.0%} of the portfolio changed each month; the top ideas (+ weather) beat "
          f"the S&P 500 in {beat:.0%} of years.")
    print("return_per_risk = yearly growth divided by volatility (higher is better). Companies that disappeared "
          "only count in their last month, so the real past was a little worse than this for every stock list.")
    ideas.save_backtest(con, monthly, args.hold)
    print(f"The portfolio to follow will {'use' if ideas.use_cap(con) else 'not use'} the cap of "
          f"{ideas.MAX_PER_INDUSTRY} per industry (used only if it gave more return per risk in both halves).")


def _study_forecast(con, args: argparse.Namespace) -> None:
    from trend_bot import forecast

    print("Building 20 years of training data (takes a few minutes)...")
    rows = forecast.training_rows(con, universe=args.universe, benchmark=args.benchmark)
    if rows.empty:
        print("Not enough data.")
        return
    preds = forecast.walk_forward(rows, first_year=args.first_year)
    if preds.empty:
        print("Not enough history before the first test year.")
        return
    card = forecast.scorecard(preds)
    sc = forecast.save_scorecard(con, preds, card)
    pct = lambda v: f"{v:.1%}"
    print(f"\n30-day outlook, tested year by year from {preds['month'].min():%Y} to {preds['month'].max():%Y}: "
          f"{len(preds):,} predictions, each made with only the years before it.\n")
    print("== How often it was right ==")
    print(card["summary"].to_string(formatters={c: pct for c in ("happened", "accuracy", "naive_accuracy")} |
                                    {"brier": "{:.4f}".format, "naive_brier": "{:.4f}".format}))
    print("naive = always guessing the more common outcome. brier = prediction error, lower is better;\n"
          "the model only adds something if it beats the naive numbers.")
    for key, label in (("calibration_beat", "beats the S&P 500"), ("calibration_up", "goes up")):
        print(f"\n== When it said X% chance it {label}, how often did it happen? ==")
        print(card[key].to_string(formatters={"predicted": pct, "happened": pct}))
    print("\n== Ranking stocks by chance to beat the S&P 500, every month ==")
    ideas = card["ideas"].copy()
    ideas["beat_spy_months"] = ideas["beat_spy_months"].map(lambda v: "–" if pd.isna(v) else f"{v:.0%}")
    print(ideas.to_string(formatters={"avg_month": "{:+.2%}".format}))
    print(f"The top 10% did better than the bottom 10% in {card['top_beat_bottom']:.0%} of months.")
    print("\nIn one sentence (shown with every outlook from now on):\n" + forecast.reliability_note(sc))
    print("\nNote: only companies still listed today are included (survivorship bias).")


def _study_model(con, args: argparse.Namespace) -> None:
    from trend_bot import model

    print("Scoring every stock at every month-end since the data starts (takes a few minutes)...")
    monthly = model.backtest(con, hold=args.hold, universe=args.universe, buffer=args.buffer,
                             cash_rate=args.cash_rate, benchmark=args.benchmark)
    if monthly.empty:
        print("Not enough data.")
        return
    b = args.benchmark
    names = {"model": "Model + weather filter", "model_no_weather": "Model, always invested",
             b: f"{b} (buy & hold)", f"{b}_weather": f"{b} + weather filter", "all_eligible": "All eligible stocks"}
    cols = list(names)
    fmt = {"cagr": "{:+.1%}".format, "volatility": "{:.0%}".format, "max_drawdown": "{:.0%}".format,
           "return_per_risk": "{:.2f}".format, "total": "{:+.0%}".format}
    print(f"\nTrend Score model: top {args.hold} of the {args.universe:,} most traded stocks, checked monthly, "
          f"sell below rank {args.buffer}; 0.1% per trade; cash earns {args.cash_rate:g}%/yr")
    periods = [("All years", monthly.index.min(), monthly.index.max()),
               ("2006-2015 (check 1)", pd.Timestamp("2006-01-01"), pd.Timestamp("2015-12-31")),
               ("2016-now (check 2)", pd.Timestamp("2016-01-01"), monthly.index.max())]
    for label, start, end in periods:
        part = monthly[(monthly.index >= max(start, pd.Timestamp(args.since))) & (monthly.index <= end)]
        if part.empty:
            continue
        s = model.summarize(part, cols).rename(index=names)
        print(f"\n== {label}: {part.index[0]:%Y-%m} to {part.index[-1]:%Y-%m} ==")
        print(s.to_string(formatters=fmt))
    shown = monthly[monthly.index >= pd.Timestamp(args.since)]
    print(f"\nIn the market {shown['invested'].mean():.0%} of months; "
          f"on average {shown['turnover'].mean():.0%} of the portfolio changed each month.")
    # min_count=1 keeps years without benchmark data blank instead of showing +0%.
    yearly = (1 + shown[["model", "model_no_weather", b, f"{b}_weather"]]).groupby(
        shown.index.year).prod(min_count=1) - 1
    print("\nYear by year:\n" + yearly.rename(columns=names).rename_axis("year").to_string(
        float_format=lambda v: f"{v:+.0%}", na_rep="–"))
    print("\nreturn_per_risk = yearly growth divided by volatility (higher is better).")


def _study_momentum(con, args: argparse.Namespace) -> None:
    from trend_bot import momentum

    print("Loading month-end prices for every stock (takes a minute)...")
    closes, dollar_vol = momentum.monthly_panel(con)
    monthly = momentum.backtest(closes, dollar_vol, lookback=args.lookback, skip=1, top=args.top,
                                min_price=args.min_price, min_dollar_vol=args.min_volume)
    monthly = monthly[monthly.index >= pd.Timestamp(args.since)]
    if args.until:
        monthly = monthly[monthly.index <= pd.Timestamp(args.until)]
    if monthly.empty:
        print("Not enough data.")
        return
    monthly = momentum.add_benchmark(con, monthly, args.benchmark)
    top_label, bottom_label = f"Top {args.top:.0%} (winners)", f"Bottom {args.top:.0%} (losers)"
    monthly = monthly.rename(columns={"top": top_label, "bottom": bottom_label, "all": "All stocks (equal)"})
    cols = [top_label, "All stocks (equal)", bottom_label, args.benchmark]
    print(f"\nMomentum: rank by the {args.lookback}-month return (skipping the latest month), "
          f"rebalance monthly, 0.1% cost per trade")
    print(f"{monthly.index[0]:%Y-%m} to {monthly.index[-1]:%Y-%m}, on average "
          f"{monthly['stocks'].mean():,.0f} eligible stocks a month\n")
    table = momentum.stats(monthly, cols, base="All stocks (equal)")
    table["beat_all_months"] = table["beat_all_months"].map(lambda v: "–" if pd.isna(v) else f"{v:.0%}")
    print(table.to_string(formatters={"cagr": "{:+.1%}".format, "volatility": "{:.0%}".format,
                                      "max_drawdown": "{:.0%}".format, "total": "{:+.0%}".format}))
    print("\ncagr = yearly growth, beat_all_months = share of months it beat the all-stocks average")
    print("\nYear by year:\n" + momentum.yearly(monthly, cols).to_string(
        float_format=lambda v: f"{v:+.0%}"))


def cmd_study(args: argparse.Namespace) -> int:
    from trend_bot import db, study
    from trend_bot.screen import liquid_tickers

    strategy = build_strategy(args)
    with closing(db.connect(args.db)) as con:
        pct = lambda df: df.to_string(formatters={
            c: "{:+.1%}".format for c in ("avg_return", "median_return", "avg_vs_bench", "median_vs_bench")} | {
            c: "{:.0%}".format for c in ("win_rate", "beat_bench_rate")})
        print(f"Benchmark: {args.benchmark} (bought and sold on the same days as each stock)\n")
        if args.signal in ("insiders", "both"):
            events = study.cluster_events(con, window_days=args.cluster_days, min_buyers=args.cluster_min,
                                          since=args.since, min_value=args.min_value,
                                          officers_only=args.officers_only)
            if args.until:
                events = events[events["date"] <= pd.Timestamp(args.until)]
            rules = [f"{args.cluster_min}+ insiders within {args.cluster_days} days"]
            if args.min_value:
                rules.append(f"at least ${args.min_value:,.0f} bought")
            if args.officers_only:
                rules.append("executives only")
            period = f"{args.since} to {args.until}" if args.until else f"since {args.since}"
            print(f"Insider cluster buys {period} ({', '.join(rules)}): {len(events):,} events")
            ev = study.add_returns(con, events, strategy=strategy, benchmark=args.benchmark)
            if not ev.empty:
                print("\nAll clusters (buy the day after the filing is public):\n" + pct(study.summarize(ev, min_price=args.min_price)))
                for trend in ("UP", "DOWN"):
                    part = ev[ev["trend"] == trend]
                    print(f"\n...when the price trend was {trend} ({strategy.label}):\n"
                          + pct(study.summarize(part, min_price=args.min_price)))
                _print_fates(con, study, events, ev, args, pct)
        if args.signal == "momentum":
            _study_momentum(con, args)
            return 0
        if args.signal == "model":
            _study_model(con, args)
            return 0
        if args.signal == "forecast":
            _study_forecast(con, args)
            return 0
        if args.signal == "factors":
            _study_factors(con, args)
            return 0
        if args.signal == "ml":
            _study_ml(con, args)
            return 0
        if args.signal == "ideas":
            _study_ideas(con, args)
            return 0
        if args.signal in ("trend", "both"):
            tickers = liquid_tickers(con, args.min_price, args.min_volume)
            if args.max_tickers:
                tickers = tickers[: args.max_tickers]
            events = study.trend_flip_events(con, strategy, tickers, since=args.since)
            if args.until:
                events = events[events["date"] <= pd.Timestamp(args.until)]
            print(f"\nTrend BUY flips ({strategy.label}) in {len(tickers):,} stocks since {args.since}: "
                  f"{len(events):,} events")
            ev = study.add_returns(con, events, benchmark=args.benchmark)
            if not ev.empty:
                print(pct(study.summarize(ev, min_price=args.min_price)))
    print("\nNote: companies without price data only count where their SEC filings show a bankruptcy or buyout;\n"
          "the rest are left out, so results may still be a little optimistic.")
    return 0
