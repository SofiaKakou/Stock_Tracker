# How to read the Trend Bot

The Trend Bot is a research assistant for the US stock market. Every weekday evening it runs on
GitHub's computers, then sends you messages on Discord and a daily email. **It's not financial
advice**, and it can't see the future: everything it says is based on patterns from the past.

## Start here: the three things to know

1. **An index fund is the honest default.** An *S&P 500 index fund* is one investment that holds
   the 500 biggest US companies. In 20 years of testing, the bot's best picks beat the *average*
   stock, but they did **not** reliably beat simply owning that fund, especially since 2018. For
   real money, the index fund is the safe choice.
2. **The bot is most useful for ideas and warnings.** It points out stocks worth researching and
   warns you when a company you hold reports a problem.
3. **Most days there's nothing to do.** The suggested portfolio changes once a month. Every message
   starts with **"What this means"**: one plain sentence saying whether you need to do anything.

## The basics in one minute

- **A stock (or share)** is a small piece of ownership in a company. Its price goes up and down
  every day the market is open.
- **The S&P 500** is a list of the 500 biggest US companies. People use it as "the market": if
  the S&P 500 rose 10% this year, the market rose 10%.
- **An index fund** is one investment that holds every company in such a list, so you own a bit
  of all of them. It's cheap and needs no picking.
- **Beating the market** means doing better than the S&P 500 over the same time. Most
  professional investors don't manage it over long periods; that's the bar the bot is held to.
- **The SEC** is the US market regulator. Companies must file reports with it, and the bot reads
  those filings.

## What you'll get

### Every night

**📧 Daily email.** Opens with **In short**: what to do (usually nothing), any warning signs in
your stocks, and whether the bot is working. Then the portfolio, each stock's gain or loss since
it was bought, and the full report attached (open it in your browser for charts).

**👀 Your watchlist today.** Your chosen stocks with their price, the change over the last 20
trading days (about a month), and whether the price trend is up or down.

**📄 The daily report (a web page file).** Opens with a **👋 Start here** box, then the portfolio
to follow (with a chart of what $10,000 would have become in testing, next to an S&P 500 index
fund) and the top ideas. Extra research sits in a folded **🔍 For the curious** section at
the bottom; you never need it.

### When something happens

**🟢 Price trend turned up / 🔴 Price trend turned down**: a stock on your watchlist changed
direction. *Example: "TSLA's price has been climbing lately".* A prompt to take a look, not a
reason to buy or sell: on its own this signal didn't beat the market in our tests.

**🔔 Insiders are buying**: several people who run a company (executives or board members) bought
its shares with their own money. They know the company best, so it can be a good sign. On its
own it hasn't beaten the market either.

**🚩 Warning signs in your stocks**: a company you hold or watch reported a problem to the SEC:
for example its past results were wrong, its auditor left, it is restructuring, or its yearly
report will be late. These have tended to come before weaker returns, so it's worth a look before
buying more. Checked weekly, so it can be a few days late.

**🌎 Market screen**: stocks across the whole market whose price started climbing today, or
where several insiders bought. Ideas to look into, not tips.

**⚠️ Bot health**: something went wrong with the bot itself, not with your stocks (for example
old prices, the SEC asking it to slow down, or a recommended stock whose company hasn't
reported new quarterly figures in 5 months, which makes its earnings scores old). If Yahoo
refuses prices, the nightly run tries again an hour later. It usually fixes itself; **no message means
everything is fine**. "✅ all fine again" means it cleared, and **✅ SEC data is back** tells you
what it caught up on after a break.

### Once a month

**📋 The portfolio to follow**: 20 stocks with the best overall scores, equal amounts in each.
The message starts with what to do: *"This month: sell 2, buy 2, keep the other 18."*
- 🟢 **Buy**: stocks that just reached the top 20.
- 🔴 **Sell**: stocks that fell out of the top 40. Waiting until the top 40 keeps trading rare.
- **Keep**: everything else.
- 📈 **Since …**: how the portfolio has *really* done since it started, next to an S&P 500 index
  fund over the same days. This is the most important line.

**💡 Top ideas this month**: about 1,000 large US stocks scored on around 20 measures, and the best
and worst scores. Each stock says why, e.g. *"cheap for its sales, profitable"*. The measures
ask things like: is it cheap for what it earns? Is it profitable? Are fewer people betting
against it? Has its price been rising? Is the company buying back its own shares?

**🌦️ Market weather (market mood)**: is the S&P 500 above its average price of the last 200
trading days (about 10 months)?
- ☀️ **Steady**: it's above, so the market has been rising.
- 🌧️ **Shaky**: it's below, so expect a bumpier ride. **A heads-up only**: the portfolio stays
  invested, because in testing, moving to cash at these times cost more than it saved (it missed
  the rebounds).

**📊 Pick scorecard**: every pick the bot has ever made, checked against the S&P 500 over the same
days. ✅ = working so far, ❌ = not. For warnings ("should lag"), lagging counts as working. Early
numbers swing a lot; give it a few months.

**🧪 Measures on trial**: new measures the bot tests before it may use them. ✅ passed, 🔶
promising (needs more proof), ❌ didn't prove itself, ⏳ too new to judge. **Nothing is added
without your OK.**

**📈 Trend model**: an older, simpler 20-stock model kept to compare with the portfolio.

**🔬 Research run**: results of a test you or Claude started by hand (attached as a file). These
are detailed and technical; the dictionary below explains the terms.

### Not posted any more

The **🌦️ Weekly outlook** and the **🤖 All-signal model** failed their honesty tests every time,
so they no longer post to Discord. Their picks are still recorded and graded in the scorecard.
The **🧠 AI report reader** only runs if started by hand (it costs money).

## Dictionary

- **Gain or loss**: how much a stock's price has changed since it was bought.
- **Yearly growth (CAGR)**: the average yearly growth rate, as if it grew the same every year.
- **Biggest fall (worst drop)**: the largest fall from a high point before it recovered.
- **Trading costs**: the small cost of buying and selling. The tests subtract them.
- **Price trend (50 / 200-day averages)**: the average price of the last 50 trading days compared
  with the last 200. If the short one is above the long one, the trend is up.
- **Speed gauge (RSI)**: above 70 means the price ran up fast recently, below 30 that it fell fast.
- **Insiders**: a company's executives and board members.
- **8-K**: a short report a company must file with the SEC when something important happens.
- **Buybacks / issuing shares**: a company buying back its own shares (often a good sign) or
  creating new ones (often a weaker sign).
- **Short sellers**: investors betting a stock will fall. Fewer of them has tended to be better.
- **Equal-weight S&P 500 (RSP)**: a fund holding every S&P 500 company in equal amounts, the way
  the portfolio does. A fairer yardstick than the usual S&P 500, where a few giants weigh most.
- **Luck check**: the portfolio compared with 1,000 portfolios of 20 stocks picked at random. Beating
  95% of them means it's very unlikely to be luck; about half means no better than random.
- **Look-ahead check**: proof that the tests never used information from the future. For a few
  past months, the bot rebuilds that month's scores from a copy of its data cut off at that date
  and checks they match. Any difference would mean a test quietly peeked at the answers.
- **Robustness check**: the portfolio tested again with its settings nudged (15, 20 or 25 stocks;
  selling a bit sooner or later). If the results barely move, they don't hinge on lucky settings.
- **ML lab**: six ideas (several borrowed from the FreqAI project) for making the machine-learning
  model beat the simple average of the signals: counting recent years more, adding noise so it
  doesn't memorize the past, staying quiet on unusual stocks, acting only when confident,
  predicting "beats the middle stock: yes/no", and a 50/50 blend. Each is tested on years it never
  saw; a winner is only offered to you.
- **Kinds of market**: the portfolio's record in rising vs falling and calm vs volatile markets.
  It shows where any edge came from; it never moves the portfolio to cash.

**For the curious (research runs):**
- **IC (ranking quality)**: how well a ranking lined up with what happened next month: 0 means
  none, 0.02 to 0.05 is useful, higher is rare.
- **t-stat (sure it's not luck)**: how confident we can be that a result isn't chance. The bot asks
  for 3 or more, a high bar, because when many ideas are tested some look good by luck.
- **Fluke chance (q)**: when many ideas are tested, a few look good by luck. The bot counts every idea
  it has tried and adjusts each one's chance of being a fluke (the "false discovery rate"); a new
  measure or model only passes if that chance is 5% or less.
- **Likely range**: the history re-drawn 2,000 times in one-year chunks to see how much a result
  could have differed. "+12% a year (likely +8% to +16%)". If the gap to the S&P 500's range
  includes zero, the test can't tell which was really better.
- **Walk-forward / "years it never saw"**: each year is tested using only earlier years, so the
  test can't peek at the answers.
- **Survivorship bias**: only companies that still exist have prices, which makes the past look
  better than it was. The bot adds back companies that went bankrupt or were bought out.
- **Report change ("Lazy Prices")**, **tone**, **risk change**: how much a company rewrote its
  yearly or quarterly report, or how gloomy, uncertain or legal its wording is, compared with a
  year earlier. Read without AI; tested as measures on trial.

## What it can't do

- Predict news, earnings surprises or crashes before they happen.
- Know your situation: taxes, how much risk you can take, when you need the money.
- Promise anything: past patterns can stop working, especially once many people use them.

## Where the data comes from

Prices from Yahoo Finance (if Yahoo stops answering, the bot-health message tells you the
prices are getting old; the next run catches up). Company reports, insider trades and warning signs from the SEC. Short-selling
data from FINRA. All free.

## Checking things yourself

- **Why isn't a stock ranked?** GitHub → Actions → *Why is a stock (not) ranked?* → Run workflow →
  type the tickers. The answer comes to Discord: its rank, or the reason it's left out (a fund, another
  share class, too little known about it, under $5, not traded enough, no recent prices).
- **📒 Logbook**: the report's last section lists recent runs, what they did and whether they worked.
  Every result carries a stamp ("code … · data …") saying which code and data made it.
- **Data audit**: runs every month (and on demand as a Research run, study *audit*). It checks the
  data of stocks that could be ranked for misdated, impossible or missing prices and misdated
  filings. Problems the bot already handles (zero prices, one-day blips, unexplained jumps) are
  shown as handled; anything else appears in the ⚠️ Bot health message.

## Day trading?

The bot's research is about which stocks to hold for a month. Day-trading ideas are tested
separately (Research run → study `intraday`): years of half-hour prices from Alpaca, the costs of
every trade, both halves of history, and a luck check (the same trades in random directions).
Most day-trading patterns don't survive their costs.
The study also tests two newer patterns (a clear first half hour carrying on for the next hour;
big movers giving some back at the end of the day) and two filters for every pattern: **earnings
days vs other days**, and **nervous vs calm markets** (how much the market moved in its first half
hour). New patterns and filters are only tested; nothing goes live without your OK.

**Day-trading demo account** (optional, fake money, a second Alpaca paper account): trades these
patterns live every weekday so you can watch them: big opening gaps faded at 9:31 New York time,
breakouts from the first half hour checked every half hour, the index funds' last half hour at
15:30. Up to 20 positions a day (about $4,900 each), long or short, all closed at the closing price.
Each evening "🧪 Day trading" shows today's trades, profit or loss per pattern, the running total,
each pattern's 10-year test result next to it, and what the live trades have taught so far (how much
worse the real prices were than expected, the average trade). Every trade is kept in a trade log
(a spreadsheet file, `day-trade-log`, on each day's run page in the Actions tab).

It learns from them, slowly on purpose (a few weeks of day trades are mostly luck):
- **Forecast vs actual**: each pattern's live result next to its 10-year test, with whether the
  difference is within the normal range or beyond what luck explains.
- **Real costs**: after 50 trades, the measured cost of getting in replaces the assumed one, in
  the 10-year study too.
- **Money per pattern**: after 20 live trades a pattern's share of the money moves with its results,
  blended with its 10-year record (which counts like 100 trades), always between 15% and 60%.
- **Automatic pause**: after 40 trades, a pattern doing clearly worse than its history is paused
  (and resumes if it comes back within range). A great streak never gets bigger bets than the cap.

Setup: a second paper account in Alpaca, its
keys as the `ALPACA_DAY_KEY_ID` and `ALPACA_DAY_SECRET_KEY` secrets. The monthly demo account is
separate and unaffected.

## Short the big gap-ups (day trading, tested only)

The only idea that passed the 20-year swing study after realistic costs: a stock that **opens 20%+
above yesterday's close** usually falls back during the day. Each morning the day-trading account looks
at the whole market's biggest risers (Alpaca's screener), and **sells short** up to 3 that opened
20-60% up (price $5+, $5M+ traded a day, and only if the broker can lend the shares), with a **stop 15%
above** and everything bought back by the close (never overnight: a short's loss has no limit). The
study made about +0.35% a trade after costs, mostly in smaller stocks, and less in recent years; the
scorecard (`dw.vPatternScorecard`, pattern `gapup`) shows whether live trading matches.

## Short yesterday's big losers (day trading, tested only)

From the "big news days" swing study: a stock that **closed 15%+ down yesterday on at least 3 times its
usual volume** (big bad news) tends to keep falling, the next day and for weeks (20 years, as real short trades
with a 25% stop: about +0.39% a trade the next day after costs, measured against the S&P 500; a tighter
15% stop gets hit by the jumps crashed stocks make on the way down and doesn't pass).
Each morning before the open the day-trading account scans every stock the broker lists (price $5+,
$5M+ traded a day, shares the broker can lend), and at the open **sells short** up to 3 of yesterday's
biggest such losers, with a **stop 25% above**, bought back by the close. About one or two a day; some
days none. The scorecard (`dw.vPatternScorecard`, pattern `loser`) shows whether live trading matches.
Our real costs (about 0.17% a side so far) are higher than the study's, so the live results decide.

## The $100 crypto account

The third practice account (fake money, $100) trades **crypto**: no day-trading limit, it trades day and
night, and tiny amounts are normal. Its rule comes from our test (`study crypto --all-coins`, 15-minute
prices of all 36 of Alpaca's coins since 2021): **a coin that fell sharply in the last hour usually
bounces**. Bought right after and sold a day later, after fees:

| Dip in an hour | Trades a day (all coins) | Per trade after fees |
|---|---|---|
| 3%+ | ~3.7 | +0.34% (limit-order fees; ~+0.14% at the fees our orders paid) |
| 4%+ | ~1.9 | +0.81% |
| 5%+ | ~1.1 | +1.57% |

Anything held only 15 minutes or an hour lost to the fees.

**Rechecked on Oct 28** on 5-minute prices, 2024 to now (`study crypto-fast`, `study crypto-maker`): 3%+
dips made money until mid-2025 and lost after it, whether checked every 15 or every 5 minutes, and
whatever the order. Looking for faster falls (3% in 15 or 30 minutes) or smaller ones (2%) didn't help
either; the busiest rule (2% falls, sold after 4 hours) would make ~21 trades a day and lose 0.38% each.
Orders that **wait in the book** pay Alpaca 0.15% a side instead of 0.25%, and a buy that waits a little
below the price gets a better price (it misses about 1 dip in 4 that bounces straight away):

| Rule, bought with a buy waiting 30 min 0.25% below the bid | Trades a day | Per trade after fees | Since mid-2025 |
|---|---|---|---|
| 3%+ in an hour | ~3.3 | +0.18% | -0.11% |
| **4%+ in an hour (what it trades)** | ~1.6 | **+0.77%** | +0.18% |

The 4% rule is just short of our luck bar (t = 1.9; 2 passes), so the live results decide.

Every 15 minutes it checks every coin Alpaca trades (except stablecoins and gold tokens, which aren't
meant to move): a coin down **4%+** in the last hour gets a buy of about **$16** that waits **0.25% below
the bid for 30 minutes** (up to 5 coins held or being bought at once, one per coin). Once filled, a
**safety stop 15% below** goes in at the broker (coins that collapsed, like LUNA in 2022, aren't in the
test data). **24 hours later** a sell waits at the asking price for 15 minutes, then the rest is sold at
the market.

(A **fast experiment**, 2-3% dips sold an hour later, ran Oct 27-28 on request; it lost to fees in every
test and was stopped.)

Every dip trade goes to Discord ("🪙"), the nightly run adds a summary, and every
finished trade is in SQL (`dw.vCryptoTrades`). Its keys are the secrets `ALPACA_SMALL_KEY_ID` and
`ALPACA_SMALL_SECRET_KEY`. (It used to hold the monthly portfolio at $100; those stocks were sold when it
switched to crypto.)

## The demo accounts week by week

Every **Friday night** (after the close) "📅 The week" shows the demo accounts (monthly, day trading and
$100) and the S&P 500 this week and since the start, the day-trading patterns' week, and a chart of the
accounts against the S&P 500 (each as % change, so $100 and $100,000 compare directly). In SSMS the
$100 account is `small` in `dw.DimAccount` and its daily values are in `dw.FactAccountValue`. The same
chart is in the report and the email every day.

## The trading journal in SSMS

Optional: every night the bot writes everything it did into a SQL Server database (Azure SQL, free
offer) that you can open in **SQL Server Management Studio**. Tables (all in `dbo`):
`day_trades` (every day trade: expected vs actual price, result), `day_summary` (each day per
pattern), `account_values` (both demo accounts and the S&P 500 each day), `monthly_holdings`
(the portfolio after each monthly update), `safety_events`, `study_results` (the 10-year tests),
`runs` (the bot's logbook), and the extra data it collects:
- `ranking_snapshots` / `measure_snapshots`: **what the bot thought**: every stock's place and its
  rank on each measure, at each monthly update and after every Friday's close. Later you can check: was it right?
- `news`: headlines for the stocks it follows (Alpaca News), with a tone score (+ good, − bad).
- `macro`: the economy from FRED: 10-year and 2-year rates, the yield curve, the Fed rate, inflation,
  unemployment, high-yield credit spreads and the VIX (the market's fear gauge).
- `day_trades` also has the **bid, ask and spread** when each order went out.
Rerunning a night replaces that night, never doubles it.

A second database, **`TrendBotMarket`** (create it on the same server, also with the free offer),
gets a copy of all the market data: `prices` (every stock, every day) and `tickers`. It's filled
after each nightly run (Actions → *Market data mirror*); the first fill (~30 million rows) can take
a few runs.

For the economy data, get a free FRED key (fred.stlouisfed.org → My Account → API Keys) and add it
as the secret `FRED_API_KEY`.

Setup (once):
1. Azure portal → **SQL databases → Create**, apply the **free offer**, database `TrendBotTrading`,
   a new server with **SQL authentication**, "auto-pause when the free limit is reached".
2. Secrets: `AZURE_SQL_SERVER` (e.g. `yourserver.database.windows.net`), `AZURE_SQL_USER`,
   `AZURE_SQL_PASSWORD` (and `AZURE_SQL_DATABASE` if not `TrendBotTrading`).
3. So the database never has to accept connections from everywhere, each nightly run lets in only
   its own address and removes it after. That needs a small Azure app allowed to change the
   firewall: **Microsoft Entra ID → App registrations → New registration** (e.g. `trend-bot-github`)
   → **Certificates & secrets → New client secret**. Then on the SQL **server** → **Access control
   (IAM) → Add role assignment → "SQL Server Contributor"** → that app. Secrets:
   `AZURE_CLIENT_ID` (the app's Application ID), `AZURE_CLIENT_SECRET` (the secret's value),
   `AZURE_TENANT_ID` (Directory ID) and `AZURE_RESOURCE_GROUP` (e.g. `trend-bot`).

Some queries to start with:

```sql
-- Day trading: each pattern so far
SELECT pattern, COUNT(*) AS trades, AVG(ret) AS avg_return, SUM(pnl) AS dollars,
       AVG(CASE WHEN ret > 0 THEN 1.0 ELSE 0 END) AS right_share, AVG(slippage) AS avg_slippage
FROM dbo.day_trades GROUP BY pattern;

-- Both demo accounts vs the S&P 500, day by day
SELECT value_date, account, value FROM dbo.account_values ORDER BY value_date, account;

-- What the 10-year test said about each pattern
SELECT * FROM dbo.study_results ORDER BY per_trade DESC;

-- The latest headlines and their tone
SELECT TOP 50 created, symbols, headline, tone FROM dbo.news ORDER BY created DESC;

-- The bot's top 20 at each snapshot
SELECT snapshot_date, rank, ticker, score, sector FROM dbo.ranking_snapshots WHERE rank <= 20
ORDER BY snapshot_date DESC, rank;
```

### The star schema (schema `dw`): search stocks like in a BI tool

On top of the `dbo` tables (the staging area) the bot builds a **star schema** every night, the way
a data warehouse for TARGIT or Power BI is built: **dimensions** describe things, **facts** hold the
numbers, and every fact links to its dimensions by an ID ("key"). `dw.usp_Load` fills it after the
journal is written (you can also run `EXEC dw.usp_Load` yourself in SSMS); running it again changes
nothing. Every dimension has an *Unknown* row with key **-1** for facts whose stock/pattern is missing.

| Dimensions | What's in it |
|---|---|
| `dw.DimStock` (**StockKey**) | every listed stock: ticker, name, exchange, sector (or *Fund / ETF*), CIK, latest close, $ traded a day, in the portfolio now |
| `dw.DimDate` (**DateKey** = yyyymmdd) | 1990–2040: year, quarter, month, weekday, weekend |
| `dw.DimPattern` | the day-trading patterns, live or tested only, with their latest 10-year test |
| `dw.DimAccount` | monthly demo, day-trading demo, S&P 500 |
| `dw.DimMeasure` | the measures behind the ranking (ROE, earnings surprise, ...) |
| `dw.DimSeries` | the FRED economy series |
| `dw.DimNews` | each headline (text, source, link, tone) |

| Facts | One row per | Keys |
|---|---|---|
| `dw.FactForecast` | stock and snapshot day: rank, score, top-% | DateKey, StockKey |
| `dw.FactForecastMeasure` | stock, day and measure: its rank on that measure | DateKey, StockKey, MeasureKey |
| `dw.FactDayTrade` | day trade: price expected vs got, spread, return, $ | DateKey, StockKey, PatternKey |
| `dw.FactDayPattern` | day and pattern: $ result, trades | DateKey, PatternKey |
| `dw.FactAccountValue` | account and day: value, % change | DateKey, AccountKey |
| `dw.FactHolding` | monthly update and stock held | DateKey, StockKey |
| `dw.FactSafetyEvent` | warning or emergency exit | DateKey, StockKey |
| `dw.FactNews` | headline and stock it mentions: tone | NewsId, DateKey, StockKey |
| `dw.FactMacro` | series and day: value | DateKey, SeriesKey |
| `dw.FactForecastOutcome` | stock and snapshot day: its return over the next month (21 trading days) and the S&P 500's | DateKey, StockKey |

Ready-made views: **`dw.vStockSearch`** (one row per stock: what it is plus the bot's latest
forecast, last 30 days' news and day trades), `dw.vForecast` (every ranking, readable) and
`dw.vDayTrades`. In SSMS, *Database Diagrams → New* with the `dw` tables draws the star.

```sql
-- Search a stock by name or ticker
SELECT * FROM dw.vStockSearch WHERE Name LIKE '%nvidia%' OR Ticker = 'NVDA';

-- The bot's current top 20 with their sectors
SELECT TOP 20 Ticker, Name, Sector, ForecastRank, ForecastScore, InPortfolio
FROM dw.vStockSearch WHERE ForecastRank IS NOT NULL ORDER BY ForecastRank;

-- One stock's forecast over time
SELECT [Date], [Rank], Score FROM dw.vForecast WHERE Ticker = 'AAPL' ORDER BY [Date];

-- Star join: day-trading results by pattern and weekday
SELECT p.Pattern, d.DayName, COUNT(*) AS Trades, SUM(f.Pnl) AS Dollars, AVG(f.Ret) AS AvgReturn
FROM dw.FactDayTrade f
JOIN dw.DimPattern p ON p.PatternKey = f.PatternKey
JOIN dw.DimDate d ON d.DateKey = f.DateKey
GROUP BY p.Pattern, d.DayName, d.DayOfWeek ORDER BY p.Pattern, d.DayOfWeek;

-- Which measures put a stock where it is (latest snapshot)
SELECT m.Measure, f.Pct FROM dw.FactForecastMeasure f
JOIN dw.DimStock s ON s.StockKey = f.StockKey JOIN dw.DimMeasure m ON m.MeasureKey = f.MeasureKey
WHERE s.Ticker = 'AAPL' AND f.DateKey = (SELECT MAX(DateKey) FROM dw.FactForecastMeasure)
ORDER BY f.Pct DESC;
```

**Is the bot right?** Two views answer it, and fill up on their own as weeks pass:

- **`dw.vForecastCheck`**: one row per ranking snapshot. How the bot's **top 20** did over the next
  month vs **every other ranked stock** and vs the **S&P 500** (`Top20MinusRest`, `Top20MinusMarket`,
  and the share of the top 20 that beat the market). `IsFinal = 0` means the month isn't over yet
  (the numbers are "so far"). `dw.vForecastDeciles` does the same per tenth of the ranking (1 = the
  best 10%): if the ranking works, the returns fall from 1 to 10.
- **`dw.vPatternScorecard`**: each day-trading pattern, the 10-year test's "per trade after costs"
  next to what live trading actually got, with the hit rate, slippage and spread. It says
  "too early to tell" until a pattern has 20 closed trades.
- **`dw.vCryptoTrades`**: every finished trade of the $100 crypto account: coin, bought and sold
  (UTC), big dip or fast experiment, entry and exit price, return, $ result, and what sold it (a day
  later, an hour later, or the safety stop). e.g.
  `SELECT Kind, COUNT(*), AVG(Ret), SUM(Pnl) FROM dw.vCryptoTrades GROUP BY Kind`

```sql
-- Over all finished months: does the top 20 beat the rest and the market, on average?
SELECT COUNT(*) AS Months, AVG(Top20MinusRest) AS VsRest, AVG(Top20MinusMarket) AS VsMarket
FROM dw.vForecastCheck WHERE IsFinal = 1;

-- The live day-trading scorecard
SELECT Pattern, TestPerTrade, ClosedTrades, LivePerTrade, LiveMinusTest, HitRate, Verdict
FROM dw.vPatternScorecard WHERE IsLive = 1;
```

(One week or one month is mostly luck: judge the ranking after many snapshots, and a pattern after
dozens of trades.)

(Prices stay in `TrendBotMarket`: Azure SQL can't join across databases, so a price fact may come
later as its own step.)

## How the bot keeps things safe

**The portfolio to follow (and its demo account)**
- 20 stocks in equal amounts, **at most 5 from one industry** (always): one company is about 5%.
- **Checked every night** (🛡️ Portfolio safety): a **bankruptcy filing, a delisting notice or "past
  accounts can't be relied on"** means an **emergency exit**: the demo account sells it at the next
  open and keeps it out for 90 days. **Other red flags, a fall of 25% since buying or 15% in a day**
  are warnings for you (selling on those didn't help in 20 years of tests, so it's your call).
- **Drawdown alarm**: a message if the demo account is 10% below its peak or 10% behind the S&P 500.

**The day-trading demo account**
- Every position has a **protective stop at the broker** (out if it moves 5% against us; it works
  even if our job stops); any position found without one gets one when a trading part starts. At most 37 positions of about 1/20 of the account each (~$4,900 on $100,000), all together never more than **2× the account** (the broker's intraday buying power; nothing is ever held overnight). Twice the positions means $ wins and losses can be twice as big: the 3% daily loss limit below is the brake.
- **Daily loss limit**: down 3% on the day → everything closed, no more trades that day.
- **Never overnight**: every position closed with a market order at 15:50, checked and retried, a
  last sweep at 15:58, and an **independent safety net** (15:57, 16:06 and 9:20 New York) closes
  anything the trading job missed and tells you.
- **Checks before each trade**: the stock must be tradable, shortable and easy to borrow (for shorts),
  and have traded in the last 15 minutes (not halted).
- **Limit orders, not market orders**: each entry pays at most 0.2% more than the current asking price
  (or gets at least 0.2% less than the bid for a short). The first live trades paid about 0.17% a side
  more than expected with market orders; a limit caps that. An entry that hasn't filled in 5 minutes
  is cancelled (the moment has passed).
- **Not one bet on an industry**: within one sector (Yahoo's: Technology, Financial Services, ...) longs
  minus shorts at most **15% of the account**, about 3 positions one way. On Oct 27 the gap fade shorted six
  AI-chip and data-centre stocks at once, and the whole group rallied; now a 4th short in one sector is
  skipped (a trade that balances the sector still goes in).
- **No fading an earnings gap**: before the open the bot asks Yahoo which watched stocks reported
  earnings after yesterday's close or before today's open, and doesn't trade against their gap (10 years:
  fading an earnings gap lost -0.14% a trade, other gaps -0.01%; the news usually keeps moving it).
- **Opening trades only until 9:45**: the gap fade, gap-up shorts and loser shorts are about the open.
  If the morning part starts later than 9:45 (GitHub started it at 10:13 on Oct 27), it skips them and
  only does the breakouts.
- **One part failing doesn't stop the day**: each pattern (and each breakout check) runs on its own; if
  one stops with an error, the rest carries on, the report and the status message say which, and the
  15:50 closing always runs.
- **Not one big bet on the market**: longs minus shorts at most **50% of the account**. A trade that
  would tilt it further one way is skipped and logged ("too one-sided"); one that evens it out is fine.

**The bot itself**
- Both demo accounts can only reach Alpaca's paper-trading (fake money) server; keys live only in
  GitHub Secrets. The GitHub Actions it uses are locked to exact versions and every workflow can only
  read the code by default. ⚠️ Bot health and a Discord message tell you if a trading job fails.

## Seeing the crypto rule on TradingView

`tradingview/crypto_4pct_dip.pine` is the crypto account's rule as a TradingView strategy, to see where it
would have bought and sold and how that went. It only draws and replays on the chart: it never trades.

1. tradingview.com → open a chart of a coin (e.g. `SOLUSD`; Alpaca's prices aren't needed, any exchange
   works) → set the chart to **15 minutes** (the bot looks at 15-minute bars).
2. Bottom panel → **Pine Editor** → delete what's there → paste the whole file → **Save** → **Add to chart**.
3. Orange triangles: the 4% dips. Arrows: the buys (only when the price came back to the waiting order) and
   the sells. **Strategy Tester** (bottom panel): every trade and the total, after 0.15% fees a side.
4. Settings (the gear on the strategy's name): try another dip size, wait or hold, and see what changes. A
   number that looks good on a few coins isn't proof: anything worth changing gets tested here first
   (`study crypto-maker` runs all 32 coins with the bot's stricter fills).
5. Optional alert (paid plans): Alerts → Create → condition: this strategy, **Any alert() function call**.

## A reliable timer for the trading jobs (cron-job.org)

GitHub's own schedule is best effort: runs start late or are skipped on busy days (the morning part
has started an hour late, and once not at all). The bot copes (backup times, the morning starts the
afternoon itself, a safety net), but a free outside timer that **asks GitHub to start a job** at an exact
minute is more reliable. GitHub's schedule stays as the backup; a job that already ran today does
nothing the second time, so double starts are harmless.

1. **A GitHub token that can only start workflows**: GitHub → your picture → Settings → Developer
   settings → Personal access tokens → **Fine-grained tokens** → Generate new token. Name it
   "cron-job.org", expiry 1 year, **Repository access: Only select repositories → Stock_Tracker**,
   **Permissions → Repository → Actions: Read and write** (nothing else). Copy the token; don't share it
   anywhere else (not in a chat either). If it ever leaks, delete it on the same page.
2. **A free account at [cron-job.org](https://cron-job.org)**, then **Create cronjob** for each line below:
   - **URL**: `https://api.github.com/repos/SofiaKakou/Stock_Tracker/actions/workflows/FILE/dispatches`
     (FILE from the table)
   - **Schedule**: custom, Monday to Friday, the time below, **time zone America/New_York** (so summer
     and winter time take care of themselves)
   - **Advanced → Request method: POST**; **Headers**: `Authorization` = `Bearer ` followed by the token,
     `Accept` = `application/vnd.github+json`, `Content-Type` = `application/json`;
     **Request body**: from the table
   - **Notifications**: on failure (it emails you if GitHub refuses the start)

   | Job | FILE | New York time | Body |
   |---|---|---|---|
   | Day trading, morning | `daytrade.yml` | 9:20 | `{"ref":"main","inputs":{"phase":"auto"}}` |
   | Day trading, afternoon (backup) | `daytrade.yml` | 12:05 | `{"ref":"main","inputs":{"phase":"auto"}}` |
   | Safety net | `daytrade-safety.yml` | 9:20, 15:57, 16:06 | `{"ref":"main"}` |
   | Day trading status | `daytrade-status.yml` | 11:00, 14:00 | `{"ref":"main"}` |
   | Crypto account | `crypto.yml` | every 15 minutes, every day (minutes 2, 17, 32, 47) | `{"ref":"main"}` |

   The status job only reads: what's open per pattern with its gain or loss now, what's already closed,
   and the account's change today, sent to Discord. Start it by hand any time (Actions → Day trading
   status → Run workflow).

3. **Test it**: cron-job.org → the job → **Test run**. It should answer **204** (started), and a new run
   appears in GitHub → Actions. 401 or 403 means the token or its permission is wrong; 404 means the URL.

The nightly run stays on GitHub's schedule: it isn't time-critical, and a run started from outside
counts as a manual run, which does the monthly checks every time.

## Optional setup

- **Daily email**: free through a Gmail "app password". Add the `EMAIL_USER` and
  `EMAIL_APP_PASSWORD` secrets (`EMAIL_TO` optional); see `trend_bot/email_report.py`. To test
  it, start a Research run with "email-test".
- **Report as a web page**: see the end of `.github/workflows/nightly.yml`. The page is public,
  like this repository.
- **Demo account** (fake money, real prices): the bot follows its monthly portfolio on Alpaca's
  free paper-trading account, so you can see how its findings do with real orders. It can't touch
  real money: it only talks to Alpaca's paper-trading server, where real-money keys don't work.
  1. Sign up at alpaca.markets (free) and open the **Paper Trading** account (it starts with $100,000 of fake money).
  2. In the paper account's dashboard, generate an API key.
  3. Add two secrets: `ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY`.

  The nightly run sends the buys and sells (whole shares, filled at the next morning's open) and
  posts "🧪 Demo account: orders sent". After the open, a morning check posts "🧪 Demo account:
  orders filled" with each price paid and the **cost of buying the next morning** (vs the closing
  prices the tests use), and sends again anything the broker refused overnight. A stock whose one
  share costs more than twice its share of the account is left out and named.
  The report and email show the account since its first trades vs **the bot's own record over the
  same days** and the S&P 500. The gap to the bot's record is the cost of real trading; expect the
  account to trail a little (`study ideas` measures the next-morning part of that over 20 years). To see the orders without sending them: Actions → **Demo account check** → Run workflow.
