# Stock_Tracker

A small trend-following stock bot that runs on your own machine. It downloads daily prices, works out whether each stock is trending up or down, tells you what the strategy would do today (BUY / SELL / HOLD / WAIT), and backtests the strategy on past data.

> This is a research and learning tool. It does **not** place trades, and nothing it prints is financial advice.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

The bot gets prices from Yahoo Finance through [`yfinance`](https://github.com/ranaroussi/yfinance), so you need an internet connection. Downloads are cached in `data_cache/` for the rest of the day.

## Usage

### Scan your watchlist

Put tickers in `watchlist.txt`, one per line, and run:

```bash
python -m trend_bot scan
python -m trend_bot scan TSLA AMD          # or pass tickers directly
```

```
        date        close  20d_chg   rsi  trend      action
ticker
NVDA    2026-09-28  ...    +6.2%     61.3 UP         BUY
SPY     2026-09-28  ...    +1.1%     55.0 UP         HOLD
...
```

| Action | Meaning |
|--------|---------|
| **BUY** | The trend turned up today |
| **SELL** | The trend turned down today |
| **HOLD** | Still in an uptrend |
| **WAIT** | Still not in an uptrend |

### Backtest

```bash
python -m trend_bot backtest AAPL
python -m trend_bot backtest AAPL --period max --show-trades
python -m trend_bot backtest my_prices.csv   # CSV with Date,Open,High,Low,Close,Volume
```

The backtest compares the strategy with buy-and-hold on total return, max drawdown, CAGR, Sharpe ratio, time in market, and win rate.

## Strategies

| Name | Idea | Options |
|------|------|---------|
| `ma_cross` (default) | Hold while the fast moving average is above the slow one (50/200 is the classic "golden cross"). | `--fast 50 --slow 200`, `--ema`, `--rsi-max 70` (don't open a new position while overbought) |
| `breakout` | Donchian / "turtle" breakout: buy on a new N-day high, sell on a new M-day low. | `--entry 55 --exit 20` |

```bash
python -m trend_bot backtest MSFT --fast 20 --slow 100 --ema
python -m trend_bot scan --strategy breakout --entry 20 --exit 10
```

### How the backtest works

- Long or flat only: the bot holds 100% of the stock or 100% cash.
- A signal from day *t*'s close is traded at that close and starts earning on day *t+1*, so the backtest never uses future data.
- Each position change costs `--cost-bps` basis points (default 5) to cover commission and slippage.

## Project layout

```
trend_bot/
  data.py        price loading (Yahoo Finance + CSV), caching, watchlist
  indicators.py  SMA, EMA, RSI, MACD, slope
  strategy.py    strategies -> a 0/1 `position` column
  backtest.py    long/flat backtester and stats
  cli.py         `scan` and `backtest` commands
tests/           offline tests on synthetic prices (run: pytest)
```

To add a strategy, subclass `Strategy` in `strategy.py`, implement `generate()` so it returns a DataFrame with a `position` column, register it in `STRATEGIES`, and add its options in `cli.py`.

## Ideas for next steps

- **Alerts**: run `scan` on a schedule (cron or Task Scheduler) and send BUY/SELL changes to email, Discord, or Telegram.
- **Charts**: plot price, moving averages, and trade markers with matplotlib or plotly.
- **Portfolio backtest**: spread money across the whole watchlist, with position sizing and risk limits such as ATR-based stops.
- **Parameter sweeps**: find which MA windows would have worked, then check them on data the sweep didn't use (walk-forward) to avoid overfitting.
- **More signals**: MACD, trend strength (ADX), volume confirmation, relative strength against SPY.
- **Paper trading**: connect to a broker's paper-trading API (for example Alpaca) to try it live with fake money.
