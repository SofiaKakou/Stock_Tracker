"""Simple long/flat backtester for a single stock."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TRADING_DAYS = 252


@dataclass
class BacktestResult:
    equity: pd.Series          # strategy equity curve, starts at `capital`
    buy_hold: pd.Series        # buy-and-hold equity curve for comparison
    trades: pd.DataFrame       # one row per completed or open trade
    stats: dict[str, float]


def _max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float((equity / peak - 1).min())


def _cagr(equity: pd.Series) -> float:
    years = len(equity) / TRADING_DAYS
    if years <= 0 or equity.iloc[0] <= 0:
        return 0.0
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)


def _sharpe(returns: pd.Series) -> float:
    std = returns.std()
    if not std or np.isnan(std):
        return 0.0
    return float(returns.mean() / std * np.sqrt(TRADING_DAYS))


def _extract_trades(signals: pd.DataFrame, held: pd.Series) -> pd.DataFrame:
    # A position held on day t was opened/closed at day t-1's close.
    fill_price = signals["Close"].shift(1)
    rows = []
    entry_date = entry_price = None
    for date, pos in held.items():
        if pos and entry_date is None:
            entry_date, entry_price = date, fill_price[date]
        elif not pos and entry_date is not None:
            rows.append((entry_date, date, entry_price, fill_price[date], False))
            entry_date = None
    if entry_date is not None:
        rows.append((entry_date, held.index[-1], entry_price, signals["Close"].iloc[-1], True))
    trades = pd.DataFrame(rows, columns=["entry", "exit", "entry_price", "exit_price", "open"])
    trades["return"] = trades["exit_price"] / trades["entry_price"] - 1
    return trades


def run_backtest(
    signals: pd.DataFrame,
    capital: float = 10_000.0,
    cost_bps: float = 5.0,
) -> BacktestResult:
    """Backtest a DataFrame with Close and position columns.

    The position decided at day t's close is held through day t+1, so trades
    effectively execute at the close of the signal day. `cost_bps` is charged
    on every change in position (commission + slippage).
    """
    daily_ret = signals["Close"].pct_change().fillna(0.0)
    held = signals["position"].shift(1).fillna(0).astype(int)
    turnover = held.diff().abs().fillna(held.iloc[0])
    strat_ret = held * daily_ret - turnover * cost_bps / 10_000

    equity = capital * (1 + strat_ret).cumprod()
    buy_hold = capital * (1 + daily_ret).cumprod()
    trades = _extract_trades(signals, held)

    closed = trades[~trades["open"]]
    stats = {
        "total_return": float(equity.iloc[-1] / capital - 1),
        "buy_hold_return": float(buy_hold.iloc[-1] / capital - 1),
        "cagr": _cagr(equity),
        "sharpe": _sharpe(strat_ret),
        "max_drawdown": _max_drawdown(equity),
        "buy_hold_max_drawdown": _max_drawdown(buy_hold),
        "exposure": float(held.mean()),
        "trades": float(len(trades)),
        "win_rate": float((closed["return"] > 0).mean()) if len(closed) else 0.0,
    }
    return BacktestResult(equity, buy_hold, trades, stats)
