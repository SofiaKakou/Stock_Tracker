"""Portfolio backtest: run one strategy across a whole watchlist with one pot of money.

Weighting schemes (applied to the positions decided at the previous close):

- "equal":  each ticker owns a fixed 1/N slot. When a ticker is out of its
            trend, its slot sits in cash. Conservative; never concentrated.
- "active": the money is split evenly between the tickers that are currently
            in an uptrend. Fully invested whenever anything is trending, but can
            end up 100% in a single stock.

Weights are rebalanced daily (a simplification); costs are charged on the
change in weights.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from trend_bot.backtest import TRADING_DAYS, _cagr, _max_drawdown, _sharpe, run_backtest

WEIGHTINGS = ("equal", "active")


@dataclass
class PortfolioResult:
    equity: pd.Series          # portfolio value over time
    benchmark: pd.Series       # equal-weight buy & hold of the same tickers
    weights: pd.DataFrame      # fraction of the portfolio in each ticker, per day
    per_ticker: pd.DataFrame   # each ticker's stand-alone strategy result on the same dates
    stats: dict[str, float]


def run_portfolio_backtest(
    signals: dict[str, pd.DataFrame],
    weighting: str = "equal",
    capital: float = 10_000.0,
    cost_bps: float = 5.0,
    cash_rate: float = 0.0,
) -> PortfolioResult:
    if weighting not in WEIGHTINGS:
        raise ValueError(f"weighting must be one of {WEIGHTINGS}")
    if not signals:
        raise ValueError("no tickers to backtest")

    # Only use dates where every ticker has a price, so the comparison is fair.
    closes = pd.DataFrame({t: s["Close"] for t, s in signals.items()}).dropna()
    if len(closes) < 2:
        raise ValueError("the tickers have no overlapping price history")
    positions = pd.DataFrame({t: s["position"] for t, s in signals.items()}).reindex(closes.index).fillna(0)

    rets = closes.pct_change().fillna(0.0)
    held = positions.shift(1).fillna(0)
    if weighting == "equal":
        weights = held / len(closes.columns)
    else:
        weights = held.div(held.sum(axis=1), axis=0).fillna(0.0)

    cash_daily = (1 + cash_rate / 100) ** (1 / TRADING_DAYS) - 1
    cash_weight = 1 - weights.sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    turnover.iloc[0] = weights.iloc[0].sum()
    port_ret = (weights * rets).sum(axis=1) + cash_weight * cash_daily - turnover * cost_bps / 10_000
    bench_ret = rets.mean(axis=1)

    equity = capital * (1 + port_ret).cumprod()
    benchmark = capital * (1 + bench_ret).cumprod()

    rows = []
    for t in closes.columns:
        window = signals[t].loc[closes.index]
        s = run_backtest(window, capital=capital, cost_bps=cost_bps, cash_rate=cash_rate).stats
        rows.append({"ticker": t, "strategy": s["total_return"], "buy_hold": s["buy_hold_return"],
                     "max_dd": s["max_drawdown"], "in_market": s["exposure"], "trades": int(s["trades"])})
    per_ticker = pd.DataFrame(rows).set_index("ticker")

    stats = {
        "total_return": float(equity.iloc[-1] / capital - 1),
        "benchmark_return": float(benchmark.iloc[-1] / capital - 1),
        "cagr": _cagr(equity),
        "benchmark_cagr": _cagr(benchmark),
        "sharpe": _sharpe(port_ret, cash_daily),
        "benchmark_sharpe": _sharpe(bench_ret, cash_daily),
        "max_drawdown": _max_drawdown(equity),
        "benchmark_max_drawdown": _max_drawdown(benchmark),
        "avg_invested": float(weights.sum(axis=1).mean()),
        "avg_holdings": float(held.sum(axis=1).mean()),
    }
    return PortfolioResult(equity, benchmark, weights, per_ticker, stats)
