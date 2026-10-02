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

If the SEC keeps refusing requests after the bot's waits (1, 3 and 10 minutes), the bot **pauses all SEC downloads for 24 hours**, so it doesn't extend the block. Prices, alerts and the report keep updating; use `--ignore-sec-pause` to try sooner. The one-time catch-ups (what happened to delisted companies, and industry codes) are spread over several nights, at most `--sec-lookups` (default 1,500) per run.

Useful flags: `--prices-only`, `--insiders-only`, `--insider-since 2015`, `--retry-failed` (retry tickers that returned no data), and `--pause 2` (go slower if Yahoo starts refusing).

### Screen the whole market

```bash
python -m trend_bot screen                    # today's trend flips + insider buying clusters
python -m trend_bot screen --days 5 --signal buy
python -m trend_bot screen --min-price 10 --min-volume 5000000
```

Stocks under $5 and stocks averaging under $1M of trading a day are skipped by default, and so are funds. Funds include closed-end funds (identified by their SEC industry code) and ETFs/ETNs. ETFs/ETNs are identified from Nasdaq's daily list of every US-listed security, which also catches leveraged products like GDXU that the SEC files under the issuing bank. Use `--include-funds` to keep the funds. Flips that also have an insider buying cluster are listed first.

🔔 **Strong insider picks** are stocks where 3 or more insiders bought $250k+ in the last 30 days *and* the price trend is up. This was the best-performing rule in the studies. `alert --market` adds a 🌎 **Market screen** message to your Discord alert, with strong picks first and **NEW** on ones not seen before.

### Track record

```bash
python -m trend_bot track                        # how every flagged stock has done since
python -m trend_bot track --signal strong_insider --benchmark IWM
```

Every night, `alert --market` saves each flagged stock (new uptrends and downtrends, insider clusters, strong insider picks) with that day's price. `track` shows how they've done since, per signal and against a benchmark, plus the most recent picks. This is the most honest test of the signals: nobody can tune a rule to prices that didn't exist yet. Give it a few months.

### Daily report page

```bash
python -m trend_bot report --open
```

Writes `reports/report-DATE.html` (and `reports/latest.html`): a web page with market breadth, strong insider picks with 6-month mini charts, new uptrends and downtrends, insider clusters and the track record. It works offline and follows your light/dark setting. The nightly `run_alerts.bat` creates it automatically.

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

### Trend Score model (buy and sell ideas)

```bash
python -m trend_bot study model        # 20-year test, split into 2006-2015 and 2016-now
python -m trend_bot model              # today's portfolio, buy/sell signals, market weather
```

Every stock among the ~1,000 most traded (over $5, no funds) gets a **score from 0 to 100**, combining signals with research behind them:

| Weight | Signal | Higher score when... |
|---|---|---|
| 25% | Trend | the price is above its 50- and 200-day averages |
| 25% | Momentum | the 6- and 12-month return is strong (skipping the latest month) |
| 20% | 52-week high | the price is close to its high of the past year |
| 15% | Steadiness | day-to-day swings are small |
| 15% | Insiders | 2+ insiders bought in the last 90 days (3+ sellers lower it) |

The weights are fixed round numbers, not fitted to past data.

- **Model portfolio:** the top 20 scores, equal amounts. It's checked once a month. A stock is sold only when it falls out of the top 40, which avoids needless trading, and the best new ones are bought.
- **Market weather:** when the S&P 500 is below its 200-day average at the monthly check, the model holds cash.
- **Signals:** the nightly alert (`--model`) posts 📈 **monthly buys and sells** to Discord, plus a note whenever the weather changes. The report page shows the portfolio, and `track` records every model buy and sell so you can see how they do.

`study model` compares the model with and without the weather filter, SPY buy-and-hold, SPY with the weather filter, and all eligible stocks. It shows all years, then **2006–2015 and 2016–now separately**: a rule you can trust should hold up in both. Options: `--hold`, `--universe`, `--buffer`, `--cash-rate` (default 2%/yr while in cash). This is theoretical, and the survivorship bias caveat applies.

### 30-day outlook (up / down odds)

```bash
python -m trend_bot study forecast     # how reliable the odds have been (walk-forward test)
python -m trend_bot forecast           # today's ideas: most and least likely to beat the market
```

For every stock in the model universe, the bot estimates two chances for the next 30 days: that it **goes up**, and that it **beats the S&P 500**. It uses a logistic regression on the Trend Score's signal parts plus the market weather, learned from every month since 2006. It relearns once a month, and each idea comes with its main reasons ("in an uptrend, near its 52-week high").

`study forecast` is the honesty check. It predicts each year from 2011 using **only the years before it**, then reports:
- accuracy and prediction error, compared with always guessing the more common outcome,
- whether "60% chance" really happened about 60% of the time,
- how the top 10% of ideas did each month compared with the bottom 10% and the S&P 500.

**What the test found on the full database:** the up/down odds were no more accurate than always guessing the common outcome. The ranking did spot laggards: the bottom 10% trailed the average stock. The top 10% did not beat the market. The outputs are built around that result:

1. **🌦️ Market weather first:** invest or caution, for how many days, and how far the S&P 500 is from its 200-day average. This was the one signal that held up, roughly halving the worst crash.
2. **⚠️ Most likely to lag:** stocks to be careful with.
3. **💡 Ideas to research:** the highest odds, clearly marked as a starting point rather than a buy list.
4. **"How reliable is this?":** one sentence from the latest `study forecast` run, shown with every outlook.

The nightly alert (`--forecast`) posts this as a **weekly outlook** to Discord. The report page opens with the same sections, and `track` records the lists so their live record builds up.

### Company fundamentals (factor study)

```bash
python -m trend_bot db update --fundamentals-only   # download company financials now (also weekly in db update)
python -m trend_bot study factors                   # which fundamental signals predicted returns?
```

The bot reads the SEC's free **company facts** file: every number each company has reported in its 10-K and 10-Q filings since about 2009 (around 1.3 GB, refreshed weekly). It keeps revenue, gross profit, net income, operating cash flow, assets, liabilities, equity and shares outstanding, **with the date each number was filed**. Every lookup uses only what had been filed by then, so the tests never use numbers that weren't public yet.

From these it builds classic signals from finance research:

| Signal | Meaning | Better when |
|---|---|---|
| gross_profitability | gross profit / assets | higher |
| roe | net income / equity | higher |
| accruals | (net income − operating cash flow) / assets | lower |
| leverage | liabilities / assets | lower |
| revenue_growth | sales vs a year earlier | higher |
| asset_growth | assets vs a year earlier | lower |
| earnings_yield | net income / market value | higher |
| book_to_market | book equity / market value | higher |
| sales_to_price | sales / market value | higher |
| quality_value_combo | average rank of profitability, earnings yield, book-to-market, accruals, asset growth | higher |
| sue | latest quarter's profit surprise: profit minus the same quarter a year earlier, divided by how much that change usually varies for the company | higher |
| revenue_sue | the same surprise measure for revenue | higher |
| ear | stock minus S&P 500 over the 3 trading days around the latest quarterly filing | higher |
| earnings_combo | average rank of sue, revenue_sue and ear | higher |
| short_ratio | shares sold short / shares outstanding | lower |
| days_to_cover | shares sold short / average daily volume | lower |
| short_change | shares sold short vs a month earlier | lower |
| short_volume_ratio | share of last month's off-exchange trading that was short sales | lower |
| short_combo | average rank of short_ratio, days_to_cover and short_change | higher |

**Earnings surprises** (`trend_bot/earnings.py`): after a company reports results far better (or worse) than its own history suggested, its stock has tended to keep drifting the same way for weeks ("post-earnings drift"). The bot rebuilds each company's quarterly numbers from its 10-Qs (the fourth quarter is the full year minus the first nine months), uses the value as first reported (not later restatements), and dates each surprise by the day the filing reached the SEC. A surprise counts for 95 days, and needs at least 4 earlier quarters of history.

Market value is price × shares outstanding. Past stock splits are recorded (and back-filled once by `db update`), so old share counts and Yahoo's split-adjusted prices line up.

`study factors` ranks the ~1,000 most traded stocks by each signal every month. It then reports:
- **IC (information coefficient):** how well the ranking matched next month's returns. 0.02–0.05 is useful; an `ic_t` above about 2 means it's unlikely to be luck.
- **Top minus bottom:** the best 10% minus the worst 10%, per month.

Results are shown for all years, before 2018 and from 2018 on (`--split-year`), and a real signal should hold up in both.

Building the monthly table of every signal takes about 10 minutes, so it's saved (in `data_cache/factors/`) and reused by `study factors`, `study ml`, `study ideas` and `ideas` until the data changes (new prices, financials, short data, company fates or industry codes).

**Industry codes and fates in bulk.** Once a week `db update` downloads the SEC's bulk company file (`submissions.zip`, about 1.5 GB, deleted afterwards; `sec_submissions.py`). One request gives every tracked company's industry code and every vanished company's recent filings, so there's no need for thousands of one-by-one lookups (which stop entirely while the SEC asks us to slow down). The one-by-one lookups remain only for anything the file missed.

**Companies that disappeared.** Yahoo only has prices for companies listed today, so a plain study never sees the ones that went bankrupt or were bought out. That makes weak, risky companies look better than they were. To correct this, the bot keeps the financials of every company whose public float ever reached $500M, including ones with no prices today. The nightly update then looks up how each one ended in its SEC filings, a batch each night. Each such company is put back for the month it disappeared:
- bankrupt: **−100%**,
- delisted for another reason: **−30%** (the research average),
- bought out: the average stock's return.

It only goes back in if its public float was at least as big as the universe's smaller members. Price-based signals can't be measured for these companies. The main tables include them, and a last table shows each signal with and without them, so you can see how much the bias mattered.

### Top ideas (every signal, simple mix)

```bash
python -m trend_bot ideas                 # rank the ~1,000 most traded stocks now, save and track the list
python -m trend_bot ideas --top 20 --no-record
```

This is the bot's main ideas list. Every stock in the universe (over $5, no funds) is ranked on every signal, each pointed the way research expects: cheap, profitable, profits backed by cash, little debt, beating earnings, few short sellers, strong past year, and so on. The average of those ranks is its score. Nothing is fitted to past returns, so there's no hindsight in how the signals are added up.

In the walk-forward test (`study ml`), this simple mix was steadier than the machine-learning model, and its top 10% beat the average stock. Each idea lists the signals that put it there (e.g. "cheap vs sales, profit beat, few short sellers"), and the bottom of the list shows the stocks most likely to lag.

**In the cloud:**
- The nightly run makes a new list on the first run of each month, after `study ml` has refreshed its track record.
- The weekly Discord outlook and the report show the list with a "How reliable is this?" line.
- The top and bottom 10% are recorded for forward tracking (`track`, `💡 Top ideas` / `⚠️ Most likely to lag`).

**Within each industry.** A bank and a software company look very different on debt, margins and price-to-book. So `study ml` also tests an **industry_mix**, where company signals are ranked against the company's own industry instead of the whole market. It uses about 15 groups built from SEC industry codes (`sectors.py`); an industry with fewer than 10 companies that month falls back to the whole market. The top ideas switch to the industry version **only if it ranked stocks better in both halves of the test**. Otherwise they stay with the plain mix, and the note says which one is used. `study factors` also shows each signal ranked against the whole market and within its industry.

**The portfolio to follow.** Each time `ideas` runs (monthly in the cloud), it also updates a 20-stock portfolio using exactly the rule the test below measures:
- **🟢 Buy:** stocks that enter the top 20.
- **🔴 Sell:** only stocks that drop out of the top 40 (or disappear), so trades stay rare.
- **Keep:** everything else.

Discord gets a "📋 Top-ideas portfolio" message once per update with the buys (and why), the sells (with their return since bought) and the holds. When the weather filter says caution, the message says so: the tested version held cash. Buys and sells are also recorded for `track`. Options: `--hold`, `--buffer`.

**Two more signals, only if they earn their place.** Research has also found that calmer stocks (low volatility) and stocks near their 52-week high tend to do a bit better. `study ml` tests a "+" version of the mix with both added (`simple_mix_plus` / `industry_mix_plus`). The top ideas use it only if it ranked stocks better in both halves of the test; the note says when it's on, and the reasons can then say "calm stock" or "near its 52-week high".

**Would it have made money?**

```bash
python -m trend_bot study ideas                     # hold the top 20 each month, 2009-now
python -m trend_bot study ideas --hold 30 --split-year 2020
```

This holds the top 20 ideas (`--hold`) in equal amounts and re-checks them monthly. A stock is kept while it stays in the top 40 (`--buffer`), which means fewer trades. Each trade costs 0.1%. It's shown with and without the **weather filter** (cash, earning 2% a year, while the S&P 500 is below its 200-day average). Both the plain and the industry version are compared with the S&P 500 and the average stock, for all years and for both halves, plus year by year.

Each version is also run with **at most 5 stocks per industry** (so the 20 can't all be, say, energy stocks; stocks with an unknown industry aren't capped). The portfolio to follow uses the cap only if it gave more return per unit of risk in both halves of the test.

The nightly run repeats this on the first run of each month, and the headline numbers (yearly growth and worst drop vs the S&P 500) join the "How reliable is this?" note. Companies that disappeared only count in their last month, so the real past was a little worse than this for any stock list.

It's a starting point for your own research, not a buy list.

### One model over every signal (machine learning)

```bash
python -m trend_bot study ml                      # walk-forward test, then today's ranking
python -m trend_bot study ml --first-year 2014 --split-year 2020
```

Every month-end, each of the ~1,000 most traded stocks gets about 30 inputs: price trend and momentum, last month's move, volatility, size, insider buying and selling, company financials, earnings surprises and short selling. Each input becomes a within-month rank, and a gradient-boosted tree model (LightGBM) learns to predict each stock's rank in next month's returns. Trees can pick up combinations a simple average can't ("cheap **and** improving earnings"), but they can also learn noise.

So it's tested the honest way: each year is predicted by a model trained only on the years before it. The results are compared with:
- **simple_mix:** the equal-weight average rank of every signal plus momentum (nothing fitted),
- **quality_value** and **momentum** on their own.

For each it shows the IC, top-minus-bottom, and a portfolio holding the top 10% each month after trading costs, vs the average stock. The **verdict** only says PASSED if the model beat the simple mix on unseen years in both halves (before and from `--split-year`) with an `ic_t` above 2. Otherwise treat its rankings as no better than the simple mix. The last lines show what the model relies on most and today's top and bottom 15.

**In the cloud** the nightly run re-tests the model on the first run of each month. Then:
- **Weekly outlook and report:** they show its top and most-likely-to-lag ideas **only if it passed**. Otherwise the outlook says in one line that it's not shown.
- **Forward tracking, either way:** its top and bottom 10% are recorded as picks (`🤖 All-signal model` in `track` and the report). Grading picks on prices that didn't exist yet when they were made is the one test a model can't fool.

Companies that disappeared are handled as in `study factors` (below). The model is never trained on them, because their price inputs are missing and it mustn't learn "missing prices = bankrupt". It is scored on them, though. Momentum can't be measured for them, so its row still leaves them out.

### Short selling (FINRA)

```bash
python -m trend_bot db update --short-only        # also part of every db update; --no-short skips it
python -m trend_bot study factors --split-year 2022
```

Short sellers borrow shares and sell them, betting the price will fall. They are often well-informed, and research has found that heavily shorted stocks, and stocks whose short interest jumps, tend to lag. The bot downloads FINRA's free files:
- **Short interest:** how many shares of each stock were sold short and not yet bought back, reported twice a month (around the 15th and the month end). History on FINRA's site starts at the end of 2017. A report only counts from 12 days after its date, because FINRA publishes it about 7 business days later.
- **Short volume:** each day, how much of the off-exchange trading in each stock was short selling (from August 2018), kept as monthly totals.

The first download takes about half an hour; after that each run only adds what's new. Because the history starts in 2018, use `--split-year 2022` to compare 2018–2021 with 2022 on.

### Momentum

```bash
python -m trend_bot study momentum --benchmark SPY
python -m trend_bot study momentum --top 0.2 --lookback 6 --since 2016-01-01
```

Momentum is one of the best-documented patterns in stock markets: stocks that rose most over the past year tend to keep outperforming for a while. At the end of each month, the study ranks every stock by its return over the past 12 months (`--lookback`), skipping the latest month because very recent moves tend to reverse. It then holds the top 10% (`--top`) for a month, charging 0.1% per trade. The result is compared with the losers, all stocks held equally, and the benchmark, both overall and year by year. Survivorship bias applies here too.

Backtests can read from the database too: `python -m trend_bot backtest NVDA --from-db --period max`.

## Running in the cloud (GitHub Actions)

**New to the messages?** [GUIDE.md](GUIDE.md) explains every Discord message in plain words, and what the bot can and can't do. The nightly run posts it to Discord once (pin it), and again only when it changes (`python -m trend_bot send-guide`).

**Keeping an eye on itself:**
- **Tests on every pull request** (`.github/workflows/tests.yml`): a ✅ or ❌ shows next to the merge button.
- **Bot health:** at the end of each nightly run, `python -m trend_bot health` checks that prices are fresh, the SEC isn't asking us to slow down, the financials, SEC bulk file, short data and monthly ideas are up to date, and every step finished. Discord gets a "⚠️ Bot health" message only when something is wrong (repeated after 3 days if it isn't fixed), and one "✅ all fine again" when it clears. Silence means healthy.
- **Monthly pick scorecard:** on the first run of each month, Discord gets "📊 Pick scorecard". Every kind of pick the bot has recorded (top ideas, buys and sells, model picks, insider clusters, trend flips) is graded against the S&P 500 over the same days. Only picks at least 20 days old count, and for picks meant to lag (sells, laggards, downtrends), lagging counts as working.

`.github/workflows/nightly.yml` runs everything on GitHub's servers every weekday at 22:30 UTC, so your PC doesn't need to be on. Each run updates the database, sends the Discord messages, builds the report and posts it to Discord as a file. Each report is also kept for 30 days under the run's "Artifacts".

1. On GitHub, open the repository, then **Settings → Secrets and variables → Actions → New repository secret**, and add:
   - `DISCORD_WEBHOOK_URL`: your webhook URL
   - `SEC_USER_AGENT`: your name and email, e.g. `Jane Doe jane@example.com`
2. Open the **Actions** tab, then **Nightly market run → Run workflow**. The first run builds the database from scratch (about 1–2 hours). Later runs take a few minutes.
3. Turn off the Windows task so you don't get messages twice: `Disable-ScheduledTask -TaskName "TrendBot Alerts"`.

**Research runs in the cloud:** in the **Actions** tab, open **Research run → Run workflow** and pick a study (`factors`, `ml`, `ideas`, `forecast`, `model`, `insiders`, `momentum` or `trend`), with optional extra options. It uses the same cloud database. The results go to Discord as a file, appear on the run's summary page, and are kept as an artifact for 90 days. Pick **refresh: fundamentals** to download company financials first, **short** for FINRA short selling data, **fates** to fill in industry codes and what happened to every company that disappeared, straight away (locally: `db update --fates-only`), or **full** to do the whole nightly update first. Research runs and nightly runs wait for each other, so they never use the database at the same time.

The database is kept between runs in GitHub's Actions cache. If it's ever evicted (after 7 days without a run, or if the 10 GB cache limit is exceeded), the next run rebuilds it automatically. Public repositories run for free; private ones get about 2,000 free minutes a month, and the nightly run uses roughly 10–20 of them.

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

**4. Run it every weekday (Windows).** `run_alerts.bat` runs `alert --summary` and appends the output to `alerts.log`. Once `market.db` exists, it also runs `db update` first, adds the market screen, the Trend Score model and the weekly outlook (`--market --model --forecast`), and writes the report page. Schedule it after the US market closes (4pm New York time, plus about an hour for the data to settle). Set `/ST` to that time in *your* time zone. `23:30` below is for Central/Eastern Europe:

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
  company_info.py SEC industry codes (to leave funds out of the screen)
  track.py       records flagged stocks and measures how they did
  report.py      the daily HTML report
  momentum.py    monthly momentum backtest
  model.py       Trend Score model: scores, model portfolio, weather, backtest
  forecast.py    30-day outlook: logistic regression, walk-forward test, live odds
  fundamentals.py SEC company facts: download, point-in-time lookups, factor values
  factors.py     factor study (IC and top-minus-bottom per signal)
  earnings.py    quarterly earnings surprises (SUE) and announcement returns
  short_interest.py FINRA short interest and short volume
  ml.py          one LightGBM model over every signal, walk-forward tested
  ideas.py       monthly top ideas from the simple mix, with reasons
  sectors.py     industry groups from SEC industry codes
  sec_submissions.py the SEC's weekly bulk file: industry codes and company fates for everyone
  health.py      the bot's own health check (fresh data, SEC pause, failed steps)
  alerts.py      Discord messages and the saved-trend state
  cli.py         command-line options for every command (`python -m trend_bot ...`)
  commands/      what each command does: watchlist.py (scan, backtest, portfolio, alert, news,
                 insiders), data.py (db, screen), studies.py (study ...), outputs.py (track,
                 report, model, forecast, ideas, health, send-guide, send-report), common.py
run_alerts.bat   what Windows Task Scheduler runs
tests/           offline tests on synthetic prices (run: pytest)
```

To add a strategy, subclass `Strategy` in `strategy.py`, implement `generate()` so it returns a DataFrame with a `position` column, register it in `STRATEGIES`, and add its options in `cli.py` (and `commands/common.py`'s `build_strategy`).

## Ideas for next steps

- **Risk controls**: position sizing by volatility, stop-losses based on average daily range (ATR), and a cap on how much goes into any one stock.
- **Test the insider signal**: backtest "trend is up *and* insiders bought recently" against the trend alone.
- **Event filters**: backtest rules like "don't buy in the week before earnings" to see whether they reduce nasty surprises.
- **Headline sentiment**: score headlines as positive or negative (for example with an AI model) and test whether that adds anything on top of the trend.
- **Parameter sweeps**: find which MA windows would have worked, then check them on data the sweep didn't use (walk-forward) to avoid overfitting.
- **More signals**: MACD, trend strength (ADX), volume confirmation, relative strength against SPY.
- **Paper trading**: connect to a broker's paper-trading API (for example Alpaca) to try it live with fake money.
