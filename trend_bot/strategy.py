"""Trend-following strategies.

A strategy turns a price DataFrame into a `position` column:
1 = hold the stock, 0 = stay in cash. The position for day t is decided
using data up to and including day t's close; the backtester applies it
from day t+1 so there's no look-ahead.

To add a new strategy, subclass Strategy, implement `generate`, and
register it in STRATEGIES.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from trend_bot import indicators as ind


class Strategy:
    name = "base"

    def generate(self, prices: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError


@dataclass
class MACrossover(Strategy):
    """Go long when the fast MA is above the slow MA.

    Optional RSI filter: don't open a new position when RSI is above
    `rsi_max` (the stock is already stretched / overbought).
    """

    fast: int = 50
    slow: int = 200
    use_ema: bool = False
    rsi_max: float | None = None
    name = "ma_cross"

    def generate(self, prices: pd.DataFrame) -> pd.DataFrame:
        if self.fast >= self.slow:
            raise ValueError("fast window must be shorter than slow window")
        ma = ind.ema if self.use_ema else ind.sma
        df = prices.copy()
        df["fast_ma"] = ma(df["Close"], self.fast)
        df["slow_ma"] = ma(df["Close"], self.slow)
        df["rsi"] = ind.rsi(df["Close"])

        trend_up = (df["fast_ma"] > df["slow_ma"]) & df["slow_ma"].notna()
        if self.rsi_max is None:
            df["position"] = trend_up.astype(int)
            return df

        # With the RSI filter, entries are blocked while overbought but an
        # existing position is held until the trend itself breaks.
        position = []
        held = 0
        for up, r in zip(trend_up, df["rsi"]):
            if not up:
                held = 0
            elif not held and (pd.isna(r) or r <= self.rsi_max):
                held = 1
            position.append(held)
        df["position"] = position
        return df


@dataclass
class Breakout(Strategy):
    """Donchian channel breakout (classic "turtle" style trend following).

    Buy when the close makes a new `entry`-day high; sell when it makes a
    new `exit`-day low.
    """

    entry: int = 55
    exit: int = 20
    name = "breakout"

    def generate(self, prices: pd.DataFrame) -> pd.DataFrame:
        df = prices.copy()
        # Shift by one so today's close is compared with the *prior* range.
        df["upper"] = df["High"].rolling(self.entry).max().shift(1)
        df["lower"] = df["Low"].rolling(self.exit).min().shift(1)
        position = []
        held = 0
        for close, upper, lower in zip(df["Close"], df["upper"], df["lower"]):
            if not held and pd.notna(upper) and close > upper:
                held = 1
            elif held and pd.notna(lower) and close < lower:
                held = 0
            position.append(held)
        df["position"] = position
        return df


STRATEGIES: dict[str, type[Strategy]] = {
    MACrossover.name: MACrossover,
    Breakout.name: Breakout,
}
