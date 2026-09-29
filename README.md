# Stock_Tracker

A small trend-following stock bot that runs on your own machine. It downloads daily prices, works out whether each stock is trending up or down, tells you what the strategy would do today (BUY / SELL / HOLD / WAIT), and backtests the strategy on past data.

> This is a research and learning tool. It does **not** place trades, and nothing it prints is financial advice.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

The bot gets prices from Yahoo Finance through [`yfinance`](https://github.com/ranaroussi/yfinance), so you need an internet connection. Downloads are cached in `data_cache/` for an hour, so repeated runs are fast and later runs still get fresh prices.

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
python -m trend_bot backtest SPY --period max --cash-rate 4 --plot
```

The backtest shows the strategy and buy-and-hold side by side: total return, yearly growth (CAGR), max drawdown, Sharpe ratio, and time in market. It also reports the number of trades and the win rate.

- `--cash-rate 4` pays 4% a year on money held in cash, as a money-market fund would. The same rate is used as the risk-free rate in the Sharpe ratio. The default is 0%.
- `--plot` saves a chart to `charts/` and opens it. The top panel shows the price, the strategy's lines, buy (▲) and sell (▼) markers, and shading while in the market. The bottom panel shows the strategy's portfolio value against buy-and-hold. Add `--no-open` to save the chart without opening it.

### Backtest the whole watchlist

```bash
python -m trend_bot portfolio --cash-rate 4 --plot
python -m trend_bot portfolio AAPL MSFT NVDA --weighting active --period max
```

This runs the strategy on every ticker with one pot of money and compares it with putting equal amounts in each ticker and holding them.

| `--weighting` | How money is split |
|---|---|
| `equal` (default) | Each ticker gets a fixed 1/N share. When a ticker is out of its trend, its share sits in cash. |
| `active` | The money is split evenly between the tickers currently in an uptrend. This keeps you fully invested, but you can end up 100% in one stock. |

Only dates where every ticker has prices are used, so a young stock shortens the test for all of them. The chart shows the portfolio's value over time and what it was holding each day.

### Earnings dates and headlines

```bash
python -m trend_bot news NVDA AAPL      # next earnings date + latest headlines
python -m trend_bot scan --events       # scan with an extra "earnings" column
```

This is **context, not a signal**. Big public news, like a new product launch, is usually reflected in the price within minutes. Earnings dates are still worth knowing, because prices often jump sharply around them.

## Discord alerts

The bot can post to a Discord channel whenever a stock's trend flips. Each alert shows the price, the 20-day change, RSI, the next earnings date, and a few recent headlines.

**1. Create a webhook.** In Discord, open the channel's settings (the ⚙️ next to its name), go to **Integrations → Webhooks → New Webhook**, and click **Copy Webhook URL**.

**2. Save it in a `.env` file** in the `Stock_Tracker` folder. You can copy `.env.example` and edit it:

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Anyone with this URL can post to your channel, so keep it private. `.env` is listed in `.gitignore`, so it's never pushed to GitHub.

**3. Try it:**

```bash
python -m trend_bot alert --test        # sends "Trend Bot is connected"
python -m trend_bot alert --summary     # real run, plus a table of every ticker
python -m trend_bot alert --dry-run     # print the messages instead of sending
```

The bot remembers each ticker's last trend in `alert_state.json`. The next run reports every change since then, even if the computer was off for a few days. On the very first run it only alerts for flips that happened that day.

**4. Run it every weekday (Windows).** `run_alerts.bat` runs `alert --summary` and appends the output to `alerts.log`. Schedule it after the US market closes (4pm New York time, plus about an hour for the data to settle). Set `/ST` to that time in *your* time zone. `23:30` below is for Central/Eastern Europe:

```powershell
schtasks /Create /TN "TrendBot Alerts" /TR "$env:USERPROFILE\Stock_Tracker\run_alerts.bat" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 23:30
schtasks /Run /TN "TrendBot Alerts"        # run once now to check it works
schtasks /Delete /TN "TrendBot Alerts"     # remove it
```

Scheduled tasks only run while the computer is on and you're logged in. If you miss a day, the state file makes sure the next run catches up. On Mac or Linux, use cron: `30 23 * * 1-5 cd ~/Stock_Tracker && python3 -m trend_bot alert --summary >> alerts.log 2>&1`.

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
- While out of the market, cash earns `--cash-rate`% a year (default 0).

## Project layout

```
trend_bot/
  data.py        price loading (Yahoo Finance + CSV), caching, watchlist
  indicators.py  SMA, EMA, RSI, MACD, slope
  strategy.py    strategies -> a 0/1 `position` column
  backtest.py    long/flat backtester and stats
  portfolio.py   multi-ticker backtest with one pot of money
  plot.py        backtest charts (matplotlib)
  news.py        earnings dates and headlines (Yahoo Finance)
  alerts.py      Discord messages and the saved-trend state
  cli.py         the `scan`, `backtest`, `portfolio`, `alert` and `news` commands
run_alerts.bat   what Windows Task Scheduler runs
tests/           offline tests on synthetic prices (run: pytest)
```

To add a strategy, subclass `Strategy` in `strategy.py`, implement `generate()` so it returns a DataFrame with a `position` column, register it in `STRATEGIES`, and add its options in `cli.py`.

## Ideas for next steps

- **Risk controls**: position sizing by volatility, stop-losses based on average daily range (ATR), and a cap on how much goes into any one stock.
- **Event filters**: backtest rules like "don't buy in the week before earnings" to see whether they reduce nasty surprises.
- **Headline sentiment**: score headlines as positive or negative (for example with an AI model) and test whether that adds anything on top of the trend.
- **Parameter sweeps**: find which MA windows would have worked, then check them on data the sweep didn't use (walk-forward) to avoid overfitting.
- **More signals**: MACD, trend strength (ADX), volume confirmation, relative strength against SPY.
- **Paper trading**: connect to a broker's paper-trading API (for example Alpaca) to try it live with fake money.
