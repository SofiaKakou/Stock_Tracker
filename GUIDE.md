# How to read the Trend Bot's messages

The bot runs every weekday evening (22:30 UTC) on GitHub's servers and posts to Discord.
Everything it says is based on past patterns. **It finds small edges on average, not sure
things.** It's a research assistant, not financial advice.

## The one-line summary of what we've learned

- No single signal (trend, insiders, cheapness, earnings, short sellers) reliably beats just
  holding the S&P 500 on its own.
- Combining them (the "top ideas") ranks stocks a little better than chance, and beat the
  *average* stock in testing, but **not the S&P 500**. An S&P 500 index fund is the honest
  benchmark: if the bot can't beat it, the index fund wins.
- Every pick is recorded and graded on prices that came after it. That forward record is the
  real test, and it builds up month by month.

## Messages you'll see

**🌦️ Market weather**: is the S&P 500 above its average of the last 200 trading days?
- ☀️ **Invest**: it's above, so the market is in an uptrend.
- 🌧️ **Caution**: it's below. In the past this cut the worst drops roughly in half, but it also
  sat in cash during some rebounds, which cost returns. Treat it as a warning, not a crystal ball.

**💡 Top ideas (every signal, simple mix)**, weekly: the ~1,000 most traded stocks ranked on
every signal (cheap, profitable, beating earnings, few short sellers, strong past year, ...),
averaged. Each stock lists the signals that put it there.
- **Top ideas**: best overall scores. A starting point for your own research.
- **⚠️ Most likely to lag**: worst overall scores. In testing, this end was a bit more
  reliable than the top end.
- **How reliable is this?**: the tested numbers, always shown with the list.

**📋 Top-ideas portfolio**, monthly: the portfolio you could follow, using exactly the rule
that was tested.
- 🟢 **Buy**: stocks that entered the top 20.
- 🔴 **Sell**: stocks that dropped out of the top 40 (the gap keeps trading low).
- **Keep**: everything else. Equal amounts in each.
- 📈 **Since …**: how this portfolio has *really* done since it started, compared with the
  S&P 500. This is the most important line.

**📊 Pick scorecard**, monthly: every kind of pick the bot has ever made, graded against the
S&P 500 over the same days. ✅ = working so far, ❌ = not. For "should lag" picks (sells,
laggards, downtrends), lagging the market counts as working. Early numbers swing a lot; give
it a few months.

**🌦️ Weekly outlook**: a 30-day "chance to beat the S&P 500" for each stock. **Tests show
it has no real skill**, and it says so. Kept mainly for its "likely to lag" list.

**🟢 BUY / 🔴 SELL `TICKER`**: a stock on your watchlist changed trend (its short-term average
crossed its long-term average). Trend-following alone hasn't beaten the market in our tests.

**🔔 Insider buying**: several company insiders bought their own shares with their own money
recently. Interesting to research; on its own it didn't beat the market either.

**🚩 8-K red flags**: a stock on your watchlist or in the top-ideas portfolio reported a warning
sign to the SEC: past results can't be relied on (a restatement), the auditor changed, assets were
written down, a debt default, a stock exchange warning, bankruptcy, restructuring, or a notice
that its report will be late. These have tended to come before weaker returns. Checked weekly
(from the SEC's weekly bulk file), so it can be a few days late.

**🌎 Market screen**: today's new uptrends and insider-buying clusters across the market.

**📈 Trend Score model**, monthly: an older 20-stock model based on trend and momentum.

**🤖 All-signal model**: a machine-learning model over every signal. **Only shown if it passes
its honesty test** (beating the simple mix on years it never saw). So far it hasn't.

**🧠 AI report reader**, only when started by hand (it costs money): Claude reads a sample of
company reports (the management's discussion), with the company's name and the years hidden,
and scores each from -5 (likely to lag the market) to +5 (likely to beat it). The message shows
the real cost, how the scores lined up with what the stocks did next, and how often Claude
recognized the company anyway. On past reports it may half-remember what happened, so only
reports filed from now on are a fair test.

**🧪 Signals on probation**, monthly: every new signal starts on probation (measured, but left
out of the top ideas). ✅ means it worked in both halves of its history *and* made the top-ideas
mix better in both halves, with a high bar against luck (t-stat 3+: when many signals are tested,
some look good by chance); 🔶 promising: the same but t between 2 and 3, watched but not trusted;
⏳ not enough history yet; ❌ not proven. A ✅ signal is only added if you say so.

**⚠️ Bot health**: something is wrong with the bot itself, such as stale prices, a failed step,
or the SEC asking it to slow down. It fixes itself on later runs in most cases; **silence means
healthy**. "✅ all fine again" means it cleared.

**📄 Daily report (HTML file)**: open it in your browser for everything above with charts.
It opens with **📋 At a glance**: the portfolio to follow, how it's really doing vs the S&P 500,
red flags in its stocks, and the signals on probation. It can also be published as a web page
you open on your phone (optional, see the end of `.github/workflows/nightly.yml`; the page is
public, like this repository).

**🔬 Research run**: results of a test run started from the Actions tab (attached as a file).

## Words you'll see

- **IC**: how well a ranking lined up with what happened next month (0 = none, 0.02-0.05 =
  useful, higher is rare).
- **Walk-forward / "years it never saw"**: each year is predicted using only earlier years, so
  the test can't peek at the answers.
- **Survivorship bias**: only companies that still exist have prices, which makes the past look
  better than it was. The bot adds back companies that went bankrupt or were bought out.
- **Worst drop**: the biggest fall from a peak before recovering.
- **Report change ("Lazy Prices")**: how much a company rewrote its latest 10-K / 10-Q compared
  with the same report a year earlier. Research found big rewrites tend to come before weaker
  stock returns. The bot reads a batch of reports from the SEC each night; this signal is still
  a *candidate*: it is tested in research runs but only joins the mix if it proves itself.
- **Net issuance**: how much a company's share count grew over a year (splits removed). Companies
  buying back shares have tended to beat those issuing new ones.
- **Tone / uncertainty / legal words**: the share of negative, uncertain ("may", "unpredictable")
  and legal ("lawsuit", "plaintiff") words in the latest report, and whether it got more negative
  than a year earlier. **Risk change / risk growth**: how much the "risk factors" section was
  rewritten or grew. Same reports, no AI, also candidates until they prove themselves.

## What it can't do

- Predict news, earnings surprises or crashes before they happen.
- Know your situation: taxes, how much risk you can take, when you need the money.
- Promise anything: past patterns can stop working, especially once many people use them.
