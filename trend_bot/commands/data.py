"""Market database commands: db (update/status) and screen."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from contextlib import closing
from pathlib import Path

import pandas as pd

from trend_bot.commands.common import (
    build_strategy,
)


def cmd_db(args: argparse.Namespace) -> int:
    from trend_bot import db, market_data, sec_bulk

    with closing(db.connect(args.db)) as con:
        if args.action == "status":
            s = db.status(con)
            size = Path(args.db).stat().st_size / 1e6 if Path(args.db).exists() else 0
            print(f"Database: {Path(args.db).resolve()}  ({size:,.0f} MB)")
            print(f"Tickers:        {s['tickers']:,} ({s['tickers_with_prices']:,} with prices, "
                  f"{s['failed_tickers']:,} failing)")
            print(f"Price rows:     {s['price_rows']:,}  ({s['price_first']} -> {s['price_last']})")
            print(f"Insider trades: {s['insider_trades']:,}  ({s['insider_buys']:,} open-market buys, "
                  f"filed {s['insider_first']} -> {s['insider_last']})")
            quarters, days = s["insider_quarters"], s["insider_days"]
            if quarters:
                print(f"  quarterly SEC files: {len(quarters)} loaded, {quarters[0]} -> {quarters[-1]}")
            if days:
                print(f"  recent days read one by one: {len(days)} ({days[0]} -> {days[-1]})")
            from trend_bot.company_info import fund_tickers

            checked = con.execute("SELECT COUNT(DISTINCT cik) FROM tickers WHERE info_checked_at IS NOT NULL").fetchone()[0]
            print(f"Industry codes: {checked:,} companies looked up; {len(fund_tickers(con)):,} tickers are funds/ETFs "
                  f"(left out of the screen)")
            si = con.execute("SELECT COUNT(DISTINCT settle), MIN(settle), MAX(settle) FROM short_interest").fetchone()
            if si[0]:
                print(f"Short interest: {si[0]:,} FINRA reports, {si[1]} to {si[2]}; short volume up to "
                      f"{db.get_meta(con, 'short_volume_last') or 'not loaded'}")
            picks = con.execute("SELECT COUNT(*), MIN(date) FROM picks").fetchone()
            if picks[0]:
                print(f"Tracked picks:  {picks[0]:,} since {picks[1]}")
            if s["fates"]:
                f = s["fates"]
                print(f"Delisted companies checked: {sum(f.values()):,} ({f.get('bankrupt', 0):,} bankrupt, "
                      f"{f.get('acquired', 0):,} bought out, {f.get('delisted', 0):,} delisted, "
                      f"{f.get('unknown', 0):,} unclear)")
            return 0

        if args.fundamentals_only or args.fates_only:
            args.insiders_only = True  # same SEC step, but only that part matters
        everything = not (args.universe_only or args.prices_only or args.insiders_only or args.short_only)
        if everything or args.universe_only or args.prices_only:
            n = market_data.update_universe(con, include_otc=args.include_otc, all_securities=args.all_securities)
            print(f"[universe] {n:,} tickers")
            market_data.update_etf_flags(con)
        if everything or args.prices_only:
            counts = market_data.update_prices(con, period=args.period, batch_size=args.batch, pause=args.pause,
                                               retry_failed=args.retry_failed, limit=args.limit)
            print(f"[prices] new {counts['new']:,}, updated {counts['updated']:,}, "
                  f"reloaded {counts['reloaded']:,}, no data {counts['failed']:,}")
            market_data.backfill_splits(con, pause=args.pause)
        if (everything and not args.no_short) or args.short_only:
            from trend_bot import short_interest

            try:
                short_interest.update(con)
            except RuntimeError as e:
                print(f"[short] stopped: {e} (progress is saved; the next run continues)", file=sys.stderr)
            if args.short_only:
                return 0
        if everything or args.insiders_only:
            paused = db.get_meta(con, "sec_paused_until")
            if paused and dt.datetime.now().isoformat() < paused and not args.ignore_sec_pause:
                print(f"[sec] paused until {paused[:16].replace('T', ' ')} because the SEC asked us to slow down; "
                      "skipping insider data today (prices, alerts and the report still update)")
                return 0
            try:
                if args.fundamentals_only:
                    from trend_bot import fundamentals

                    fundamentals.update_fundamentals(con, force=True)
                    return 0
                if args.fates_only:
                    from trend_bot import fates, sec_submissions

                    # Catch up in one go: the SEC's bulk file has every company's filings.
                    sec_submissions.update(con, force=True)
                    found = fates.update_fates(con, limit=args.sec_lookups)  # any the bulk file missed
                    print("[fates] " + (", ".join(f"{k} {v:,}" for k, v in sorted(found.items())) or "nothing new"))
                    left = len(fates.companies_to_check(con))
                    print(f"[fates] {left:,} companies still to check" if left else "[fates] all caught up")
                    return 0
                added = sec_bulk.load_quarters(con, since_year=args.insider_since)
                print(f"[insiders] {added} quarterly file(s) loaded")
                filings = sec_bulk.load_recent_days(con, max_days=args.insider_days,
                                                    all_companies=args.insiders_all_companies)
                print(f"[insiders] {filings:,} recent filing(s) loaded")
                from trend_bot import fates, sec_submissions

                # Industry codes and fates for every company from one weekly bulk file; the one-by-one
                # lookups below then only cover what it missed (spread over nights, well under the SEC's limits).
                sec_submissions.update(con)
                found = fates.update_fates(con, limit=args.sec_lookups)
                if found:
                    print("[fates] " + ", ".join(f"{k} {v:,}" for k, v in sorted(found.items())))
                from trend_bot import company_info

                company_info.update_company_info(con, limit=args.sec_lookups)
                if not args.no_fundamentals:
                    from trend_bot import fundamentals

                    fundamentals.update_fundamentals(con, force=args.fundamentals_only)
            except RuntimeError as e:
                print(f"[insiders] stopped: {e}", file=sys.stderr)
                if "limiting" in str(e):
                    until = (dt.datetime.now() + dt.timedelta(hours=24)).isoformat(timespec="minutes")
                    db.set_meta(con, "sec_paused_until", until)
                    con.commit()
                    print(f"[sec] pausing all SEC downloads until {until.replace('T', ' ')} "
                          "(use --ignore-sec-pause to try sooner)", file=sys.stderr)
                return 1
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    from trend_bot import db
    from trend_bot.screen import screen

    if not Path(args.db).exists():
        print(f"{args.db} not found. Build it first:  python -m trend_bot db update", file=sys.stderr)
        return 1
    strategy = build_strategy(args)
    with closing(db.connect(args.db)) as con:
        flips, clusters = screen(con, strategy, days=args.days, min_price=args.min_price,
                                 min_dollar_vol=args.min_volume, cluster_days=args.cluster_days,
                                 min_buyers=args.cluster_min, include_funds=args.include_funds)
        if args.record:
            from trend_bot.track import record_picks

            new = record_picks(con, flips, clusters)
            print(f"Recorded {len(new)} new pick(s) for tracking.")
    print(f"Strategy: {strategy.label}  |  price >= ${args.min_price:g}, "
          f"avg daily volume >= ${args.min_volume:,.0f}")

    def show(df: pd.DataFrame) -> str:
        df = df.copy()
        df["chg_20d"] = df["chg_20d"].map("{:+.1%}".format)
        df["buy_value"] = df["buy_value"].map(lambda v: f"${v:,.0f}" if v else "")
        df["buyers"] = df["buyers"].map(lambda v: v or "")
        df["cluster"] = df["cluster"].map(lambda v: "YES" if v else "")
        if "strong" in df:
            df["strong"] = df["strong"].map(lambda v: "🔔" if v else "")
        return df.head(args.limit).to_string()

    if args.signal != "all" and not flips.empty:
        flips = flips[flips["signal"] == args.signal.upper()]
    print(f"\n== Trend flips in the last {args.days} trading day(s): {len(flips)} ==")
    print(show(flips) if not flips.empty else "None.")
    print(f"\n== Insider buying clusters (2+ insiders within {args.cluster_days} days): {len(clusters)} ==")
    print(show(clusters.drop(columns=["name"])) if not clusters.empty else "None.")
    return 0
