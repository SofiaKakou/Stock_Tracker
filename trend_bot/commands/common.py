"""Helpers shared by the commands: strategy and ticker options, price loading, charts."""

from __future__ import annotations

import argparse
import os
import re
import sys
from contextlib import closing
from pathlib import Path

import pandas as pd

from trend_bot.data import load_prices, read_watchlist
from trend_bot.strategy import Breakout, MACrossover, Strategy


ACTION_ORDER = {"BUY": 0, "SELL": 1, "HOLD": 2, "WAIT": 3}


def build_strategy(args: argparse.Namespace) -> Strategy:
    if args.strategy == Breakout.name:
        return Breakout(entry=args.entry, exit=args.exit)
    return MACrossover(fast=args.fast, slow=args.slow, use_ema=args.ema, rsi_max=args.rsi_max)


def describe_today(ticker: str, signals: pd.DataFrame) -> dict:
    last, prev = signals.iloc[-1], signals.iloc[-2]
    if last["position"] and not prev["position"]:
        action = "BUY"
    elif not last["position"] and prev["position"]:
        action = "SELL"
    else:
        action = "HOLD" if last["position"] else "WAIT"
    change_20d = signals["Close"].iloc[-1] / signals["Close"].iloc[-21] - 1 if len(signals) > 20 else float("nan")
    return {
        "ticker": ticker,
        "date": signals.index[-1].date(),
        "close": round(float(last["Close"]), 2),
        "20d_chg": f"{change_20d:+.1%}",
        "rsi": round(float(last["rsi"]), 1) if "rsi" in signals and pd.notna(last["rsi"]) else None,
        "trend": "UP" if last["position"] else "DOWN/FLAT",
        "action": action,
    }


def tickers_from(args: argparse.Namespace) -> list[str]:
    if not args.tickers:
        return read_watchlist(args.watchlist)
    return [t if t.lower().endswith(".csv") else t.upper() for t in args.tickers]


def _period_start(period: str) -> str | None:
    """'5y' / '6mo' / '30d' -> an ISO start date; 'max' -> None."""
    m = re.fullmatch(r"(\d+)(y|mo|d)", period or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    days = n * {"y": 365.25, "mo": 30.5, "d": 1}[unit]
    return (pd.Timestamp.today() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")


def get_prices(source: str, args: argparse.Namespace, default_period: str) -> pd.DataFrame:
    """Prices from the local database (--from-db) or Yahoo/CSV."""
    period = args.period or default_period
    if getattr(args, "from_db", False) and not source.lower().endswith(".csv"):
        from trend_bot import db

        with closing(db.connect(args.db)) as con:
            return db.load_prices(con, source, start=_period_start(period))
    return load_prices(source, period=period, use_cache=not args.no_cache)


def generate_all(tickers: list[str], strategy: Strategy, args: argparse.Namespace,
                 default_period: str) -> dict[str, pd.DataFrame]:
    """Run the strategy on every ticker, skipping (and reporting) any that fail."""
    out = {}
    for t in tickers:
        try:
            out[t] = strategy.generate(get_prices(t, args, default_period))
        except Exception as e:  # keep going with the rest of the list
            print(f"[skip] {t}: {e}", file=sys.stderr)
    return out


def open_file(path: Path) -> None:
    """Open a file with the system's default viewer (best effort)."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
    except OSError:
        pass


def _finish_chart(chart: Path, args: argparse.Namespace) -> None:
    print(f"\nChart saved to {chart.resolve()}")
    if not args.no_open and sys.stdout.isatty():
        open_file(chart)


def _insider_short(insiders, ticker: str) -> str:
    if ticker.lower().endswith(".csv"):
        return "–"
    try:
        return insiders.summarize(insiders.fetch_trades(ticker, days=90)).short
    except Exception as e:
        print(f"[insiders] {ticker}: {e}", file=sys.stderr)
        return "?"
