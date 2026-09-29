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

### Insider trades (SEC Form 4)

Company insiders (executives, directors, and anyone owning 10% or more) must report trades in their own company's stock to the SEC within two business days. The bot reads these public filings for free.

```bash
python -m trend_bot insiders NVDA            # open-market buys and sells, last 90 days
python -m trend_bot insiders NVDA --all      # also awards, option exercises, gifts...
python -m trend_bot scan --insiders          # scan with an "insiders_90d" column
```

- **Buys matter most.** An insider spending their own money on the open market is a meaningful signal. **Cluster buying**, where two or more different insiders buy within 30 days, is the pattern with the best track record in research.
- **Sales matter much less.** Insiders sell for taxes, diversification, or big purchases. Many sales are pre-scheduled "10b5-1" plans (marked `10b5-1` in the output), which say even less.
- ETFs like SPY have no insiders, so they show "none".

**Setup:** the SEC asks every automated tool to identify itself. Add your name and email to `.env`:

```
SEC_USER_AGENT=Your Name your.email@example.com
```

Filings are cached in `data_cache/sec/`, so only the first run for a ticker is slow.

## Whole-market database

Put every US stock (NYSE, Nasdaq and Cboe, roughly 6,000) and all SEC insider trades since 2006 into one local SQLite file, `market.db`. Then you can scan the whole market daily and test signals on thousands of stocks.

```bash
python -m trend_bot db update --limit 50   # quick test with 50 tickers first
python -m trend_bot db update              # the full build (leave it running)
python -m trend_bot db status              # what's in the database
```

**The first build takes a while:** roughly 1–3 hours for prices and about 1–2 hours for insider data, and the result is about 2 GB. It saves after every batch, so you can stop it (Ctrl+C) at any time and run it again to continue. After that, a daily `db update` only fetches what's new (about 10–15 minutes). `run_alerts.bat` does this automatically once `market.db` exists.

What `db update` does:

| Step | Source | Notes |
|---|---|---|
| Stock list | SEC `company_tickers_exchange.json` | NYSE, Nasdaq, Cboe, plus SPY/QQQ/IWM as benchmarks. Add `--include-otc` for over-the-counter stocks. |
| Prices | Yahoo Finance, 100 tickers per request | Full history for new tickers (`--period`, default `max`), then only new days. When a dividend or split changes past prices, that ticker is reloaded in full. |
| Insider trades | SEC quarterly bulk files (2006 onwards) | Complete and fast. Each quarter is published a few weeks after it ends. Only open-market buys and sales are stored. |
| Recent insider trades | SEC daily filing index | Fills the weeks since the last published quarter, one filing at a time (`--insider-days`, default 30). Only filings for companies the screen can show are downloaded; add `--insiders-all-companies` for everyone. |

Useful flags: `--prices-only`, `--insiders-only`, `--insider-since 2015`, `--retry-failed` (retry tickers that returned no data), and `--pause 2` (go slower if Yahoo starts refusing).

### Screen the whole market

```bash
python -m trend_bot screen                    # today's trend flips + insider buying clusters
python -m trend_bot screen --days 5 --signal buy
python -m trend_bot screen --min-price 10 --min-volume 5000000
```

Stocks under $5, or averaging under $1M of trading a day, are skipped by default. Flips that also have an insider buying cluster are listed first. `alert --market` adds a 🌎 **Market screen** message with the top results to your Discord alert.

### Test a signal on history

```bash
python -m trend_bot study insiders            # insider cluster buys since 2006
python -m trend_bot study trend               # 50/200 golden crosses across the market
python -m trend_bot study both --since 2015-01-01
```

For every past event, the study buys the day after the signal became public. It reports the average and median return 1, 3, 6 and 12 months later, how often the trade made money, and how it did against a benchmark over the same days (`--benchmark`, default SPY; use `IWM` for small companies, which is where most insider buying happens). Insider clusters are also split by whether the price trend was up or down at the time, which tests the "trend + insiders" combination directly.

Refine the insider test with `--cluster-min 3` (more insiders), `--min-value 250000` (bigger buys), and `--officers-only` (only executives like the CEO and CFO, not directors or large outside investors).

**Avoid fooling yourself.** If you try enough combinations, one will look good by luck. Choose your rule on one period (`--until 2015-12-31`), then check it on a period it hasn't seen (`--since 2016-01-01`). Only trust a rule that holds up in both.

Two caveats:
- **Survivorship bias:** Yahoo only has prices for companies that still exist today. To correct for this, `db update` checks the SEC filings of every company that had insider buying but no longer has prices (about 15–30 minutes the first time, then only new ones). It flags **bankruptcies** (8-K Item 1.03) and **buyouts** (deregistration after merger paperwork). The insider study then adds a second set of results where bankruptcies count as −100% and buyouts as matching the benchmark. It also shows a worst case for the trend-UP group. Companies whose fate is unclear are still left out.
- **Overlapping events:** events that overlap in time aren't independent, so treat small differences between results as noise.

Backtests can read from the database too: `python -m trend_bot backtest NVDA --from-db --period max`.

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

If `SEC_USER_AGENT` is set, alerts also check insider trades. A trend alert includes each stock's insider buys and sells from the last 90 days. A separate 🔔 **Insider buying** alert goes out when 2 or more insiders buy within 30 days (change with `--cluster-min` and `--cluster-days`). Each cluster is announced only once. Use `--no-insiders` to turn this off.

The bot remembers each ticker's last trend in `alert_state.json`. The next run reports every change since then, even if the computer was off for a few days. On the very first run it only alerts for flips that happened that day.

**4. Run it every weekday (Windows).** `run_alerts.bat` runs `alert --summary` and appends the output to `alerts.log`. Once `market.db` exists, it also runs `db update` first and adds the market screen. Schedule it after the US market closes (4pm New York time, plus about an hour for the data to settle). Set `/ST` to that time in *your* time zone. `23:30` below is for Central/Eastern Europe:

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
  insiders.py    insider trades from SEC Form 4 filings
  db.py          the SQLite market database (schema + readers)
  market_data.py stock list and price downloads into the database
  sec_bulk.py    SEC insider data sets (quarterly bulk + daily feed)
  screen.py      whole-market screen
  study.py       event studies: did a signal come before better returns?
  fates.py       what happened to delisted companies (bankrupt / bought out)
  alerts.py      Discord messages and the saved-trend state
  cli.py         the `scan`, `backtest`, `portfolio`, `alert` and `news` commands
run_alerts.bat   what Windows Task Scheduler runs
tests/           offline tests on synthetic prices (run: pytest)
```

To add a strategy, subclass `Strategy` in `strategy.py`, implement `generate()` so it returns a DataFrame with a `position` column, register it in `STRATEGIES`, and add its options in `cli.py`.

## Ideas for next steps

- **Risk controls**: position sizing by volatility, stop-losses based on average daily range (ATR), and a cap on how much goes into any one stock.
- **Test the insider signal**: backtest "trend is up *and* insiders bought recently" against the trend alone.
- **Event filters**: backtest rules like "don't buy in the week before earnings" to see whether they reduce nasty surprises.
- **Headline sentiment**: score headlines as positive or negative (for example with an AI model) and test whether that adds anything on top of the trend.
- **Parameter sweeps**: find which MA windows would have worked, then check them on data the sweep didn't use (walk-forward) to avoid overfitting.
- **More signals**: MACD, trend strength (ADX), volume confirmation, relative strength against SPY.
- **Paper trading**: connect to a broker's paper-trading API (for example Alpaca) to try it live with fake money.
