# Stock_Tracker (Trend Bot)

A free research assistant for the US stock market. Every weekday evening it runs on GitHub's
servers, scores about 1,000 large US stocks, suggests a 20-stock portfolio once a month, warns about
problems in the stocks you follow, and sends everything to Discord and your email.

> **It never trades real money (the optional demo account uses fake money) and it isn't financial advice.** In 20 years of honest testing its best picks
> beat the *average* stock but not reliably an S&P 500 index fund, especially since 2018, so for real
> money the index fund is the default. The bot is for ideas, warnings and learning.

New to the market? Start with **[GUIDE.md](GUIDE.md)**: what every message means, in plain words.

## Features

**What you get**
- 📧 **Daily email** and 💬 **Discord messages**, each opening with "What this means" in one plain sentence
- 📄 **Daily report** (web page, phone-friendly): a *Start here* box, the portfolio, top ideas, a growth chart; detail folded under *For the curious*
- 📋 **Monthly portfolio**: 20 stocks, equal amounts, with its real record vs the S&P 500 since it started
- 💡 **Top ideas**: ~1,000 stocks scored on ~20 measures (value, profitability, earnings surprises, share buybacks, short sellers, momentum, ...) and averaged
- 🚩 **Warning signs**: restatements, auditor changes, defaults, late reports and more from SEC 8-K filings
- 🔔 **Insider buying**, 🌎 **market screen**, 👀 **watchlist trends**, 📊 **pick scorecard**
- ⚠️ **Bot health**: tells you when something breaks (old prices, SEC limits) and when it's fixed
- 👾 **The bot's office** (`python -m trend_bot office`): a pixel-art office where every part of the bot is a worker at a desk, **live** like Pixel Agents: it follows GitHub Actions as the bot runs (the worker whose step is running types, a run that fails shows at once; no key needed). Typing = working, "!" bubble = needs a look, up from a red smoking screen = broken, empty desk = not set up; the boss walks over to whoever has a problem); made every night next to the report
- 🛡️ **Safety**: nightly emergency exits (bankruptcy, delisting notice, unreliable accounts), warnings, drawdown alarm; for day trading broker-side stops, a 3% daily loss limit, an independent safety net and pre-trade checks
- 🔎 **"Why isn't this stock ranked?"**: a one-click lookup that explains any ticker
- 📒 **Logbook**: every run logged; every result stamped with the code and data that made it
- 🧪 **Demo account** (optional): follows the portfolio with fake money on Alpaca paper trading, vs the S&P 500
- 🗄️ **Trading journal in SQL Server** (optional, Azure SQL free offer): every trade, account value, holding and safety event in tables you can open in SSMS
- 🧪 **Day-trading demo account** (optional, a second paper account): the intraday patterns traded live every weekday, closed by the close, with an evening report

**Honest testing** (the core of the project)
- Walk-forward tests: every year is judged by rules made only from earlier years
- Survivorship fix: companies that went bankrupt or were bought out are put back in
- **Look-ahead check**: rebuilds past months from data cut off at that date to prove no test peeks at the future
- **Luck check** (vs 1,000 random portfolios), **likely ranges** (bootstrap), **fluke chance** (false discovery rate across every idea tested)
- **The bad times**: longest time to recover, worst months, growth without the best months
- **"Does the money add up" tests**: the followed portfolio matches the tested one; numbers match slow reference versions
- **Data audit**: misdated, impossible or missing data
- **Robustness check** (settings nudged), **kinds of market**, large-companies-only and equal-weight S&P 500 (RSP) comparisons
- New measures stay **on trial** until they pass in both halves of history; nothing changes without your OK
- **ML lab**: six machine-learning variants (several from FreqAI) tested against the simple average; none has beaten it yet
- **Day-trading research** (`study intraday`): well-known intraday patterns tested on years of Alpaca half-hour bars, after costs, both halves, luck check

**Data (all free)**: prices from Yahoo Finance; financials, filings, insider
trades and red flags from the SEC; short selling from FINRA. Gentle on every source (pacing, pauses,
automatic recovery).

## How it runs

Everything runs in the cloud on **GitHub Actions**; nothing needs to run on your computer.

| Workflow | When | What |
|---|---|---|
| `nightly.yml` | weekdays 22:30 UTC | update data, send messages, report and email; monthly honesty checks on the 1st-3rd |
| `research.yml` | by hand (Actions tab) | any study (`ideas`, `ml`, `mllab`, `lookahead`, `audit`, `candidates`, `intraday`, ...), data refreshes, `sec-check`, `email-test` |
| `research-parallel.yml` | by hand | long studies split into many jobs at once (the yearly look-ahead check in ~20 min) |
| `why.yml` | by hand | "Why is a stock (not) ranked?": type tickers, answer in Discord |
| `demo.yml` | by hand | demo account check: its value and the orders it would send (sends nothing) |
| `daytrade.yml` | weekdays, market hours | day-trading demo account (fake money): trades, closes by the close, evening report |
| `demo-morning.yml` | weekdays 14:52 UTC | demo account: how last night's orders filled; retries refusals |
| `tests.yml` | every push / PR | the test suite |
| `ai.yml` | by hand, optional | an AI report reader (needs a paid API key; off by default) |

The database lives in the Actions cache between runs.

## Setup

1. **Secrets** (repository → Settings → Secrets and variables → Actions):
   - `DISCORD_WEBHOOK_URL`: a Discord channel webhook
   - `SEC_USER_AGENT`: your name and email, e.g. `Jane Doe jane@example.com` (the SEC requires it)
   - optional email: `EMAIL_USER`, `EMAIL_APP_PASSWORD` (a Gmail app password), `EMAIL_TO`
   - optional demo account: `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` (Alpaca **paper** keys; see GUIDE.md)
   - optional day-trading demo account: `ALPACA_DAY_KEY_ID`, `ALPACA_DAY_SECRET_KEY` (a second paper account)
2. **First data load**: Actions → *Research run* → refresh `full`. Then the nightly run keeps it up to date.
3. Optional: put your tickers in `watchlist.txt`.

## Running it locally (optional)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m trend_bot db update          # build/update the database (long the first time)
python -m trend_bot scan               # watchlist trends
python -m trend_bot ideas              # this month's top ideas and portfolio
python -m trend_bot report             # the HTML report
python -m trend_bot study ideas        # any study: ideas, ml, mllab, lookahead, candidates, factors, ...
python -m pytest                       # tests
```

`python -m trend_bot --help` lists every command.

## Project layout

```
trend_bot/       the bot: data (market_data, sec_*, fundamentals, events, short_interest),
                 signals (factors, earnings, text_changes), portfolio (ideas, model),
                 testing (ml, ml_lab, lookahead, uncertainty, candidates), output (alerts,
                 report, email_report, charts, health), commands/ (the CLI)
tests/           200+ tests with simulated markets (no network)
.github/         the workflows above
GUIDE.md         how to read every message, for beginners
```

## Credits

Ideas borrowed (no code copied) from academic research (Fama-French, Novy-Marx, Sloan,
Harvey-Liu-Zhu, Lazy Prices, Loughran-McDonald) and from [freqtrade](https://github.com/freqtrade/freqtrade)
(look-ahead analysis, FreqAI) and [AI-Trader](https://github.com/HKUDS/AI-Trader) (bootstrap ranges,
false discovery rate, data fallback).
