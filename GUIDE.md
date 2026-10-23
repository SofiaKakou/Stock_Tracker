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
old prices, or the SEC asking it to slow down). It usually fixes itself; **no message means
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
- **Kinds of market**: the portfolio's record in rising vs falling and calm vs volatile markets.
  It shows where any edge came from; it never moves the portfolio to cash.

**For the curious (research runs):**
- **IC (ranking quality)**: how well a ranking lined up with what happened next month: 0 means
  none, 0.02 to 0.05 is useful, higher is rare.
- **t-stat (sure it's not luck)**: how confident we can be that a result isn't chance. The bot asks
  for 3 or more, a high bar, because when many ideas are tested some look good by luck.
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

## Optional setup

- **Daily email**: free through a Gmail "app password". Add the `EMAIL_USER` and
  `EMAIL_APP_PASSWORD` secrets (`EMAIL_TO` optional); see `trend_bot/email_report.py`. To test
  it, start a Research run with "email-test".
- **Report as a web page**: see the end of `.github/workflows/nightly.yml`. The page is public,
  like this repository.
