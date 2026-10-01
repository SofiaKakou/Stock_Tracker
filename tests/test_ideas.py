import json

import numpy as np
import pytest
import pandas as pd

from test_fundamentals import bulk_zip  # noqa: F401
from test_model import DATES, model_db  # noqa: F401
from trend_bot import alerts, db, fundamentals, ideas, ml, report


def month_frame(n=200, seed=0):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(rng.normal(size=(n, len(ml.FEATURES))), columns=ml.FEATURES)
    df["ticker"], df["month"], df["close"] = [f"T{i}" for i in range(n)], pd.Timestamp("2026-09-30"), 50.0
    df["gone"] = pd.Series([None] * n, dtype=object)
    return df


def test_rank_month_scores_every_signal_the_expected_way():
    df = month_frame()
    # T0: cheap, profitable, beating earnings, few shorts, strong year. T1: the opposite.
    for c in ml.SIGNALS:
        df.loc[0, c] = 10 * fundamentals.EXPECTED[c]
        df.loc[1, c] = -10 * fundamentals.EXPECTED[c]
    df.loc[0, "mom12"], df.loc[1, "mom12"] = 10, -10
    df.loc[2, ml.SIGNALS] = np.nan                       # too little known: left out
    df.loc[3, "gone"] = "bankrupt"                       # disappeared companies are never listed
    ranked = ideas.rank_month(df)
    assert ranked.index[0] == "T0" and ranked.index[-1] == "T1"
    assert "T2" not in ranked.index and "T3" not in ranked.index
    assert len(ranked.loc["T0", "why"].split(", ")) == 3
    assert all(w in ideas.GOOD.values() for w in ranked.loc["T0", "why"].split(", "))
    assert any(w in ranked.loc["T1", "why"] for w in ideas.BAD.values())
    assert ranked["pct"].is_monotonic_decreasing


def test_reasons_in_plain_words():
    r = pd.Series({"sales_to_price": 0.99, "sue": 0.95, "leverage": 0.5, "mom12": 0.85})
    assert ideas.reasons(r) == "cheap vs sales, profit beat, strong past year"
    assert ideas.reasons(1 - r, best=False) == "expensive vs sales, profit miss, weak past year"
    assert ideas.reasons(pd.Series({"sue": 0.5})) == "a bit of everything"


def test_ideas_command_saves_records_and_shows_everywhere(model_db, tmp_path, monkeypatch, capsys):
    from trend_bot.cli import main

    years = list(range(DATES[0].year - 1, DATES[-1].year))
    data = bulk_zip(years)
    monkeypatch.setattr(fundamentals.insiders, "CACHE_DIR", tmp_path / "sec")
    fundamentals.update_fundamentals(model_db, download=lambda p: (p.parent.mkdir(parents=True, exist_ok=True),
                                                                   p.write_bytes(data), p)[-1],
                                     log=lambda *_: None)
    today = DATES[-1].date() + pd.Timedelta(days=1)
    monkeypatch.setattr(ideas.dt, "date", type("D", (ideas.dt.date,), {"today": classmethod(lambda c: today)}))
    path = str(tmp_path / "market.db")
    assert main(["ideas", "--db", path, "--universe", "100"]) == 0
    out = capsys.readouterr().out
    assert "Top ideas" in out and "Most likely to lag" in out and "Not tested yet" in out
    saved = ideas.load(model_db)
    assert saved["date"] == str(DATES[-1].date()) and len(saved["top"]) == 15 and len(saved["bottom"]) == 10
    assert saved["top"][0]["why"]
    picks = dict(model_db.execute("SELECT signal, COUNT(*) FROM picks GROUP BY signal"))
    assert picks["mix_top"] == picks["mix_bottom"] >= 1

    # study ml adds the track record the note quotes.
    assert main(["study", "ml", "--db", path, "--trees", "10", "--universe", "100"]) == 0
    assert "Tested" in ideas.reliability_note(model_db)

    from trend_bot import model
    weather = model.weather_status(model_db)
    e = alerts.ideas_embed(saved, weather, ideas.reliability_note(model_db))
    assert saved["top"][0]["ticker"] in e["description"] and "How reliable" in e["description"]
    assert alerts.ideas_embed(None, weather, "") is None
    html = "".join(report._ideas_section(model_db, str))
    assert "Top ideas" in html and saved["bottom"][0]["ticker"] in html


def panel_with_edge(months=36, n=150, seed=1):
    """Stocks good on every signal earn more next month."""
    rng = np.random.default_rng(seed)
    rows = []
    for m in pd.date_range("2019-01-31", periods=months, freq="ME"):
        f = pd.DataFrame(rng.normal(size=(n, len(ml.FEATURES))), columns=ml.FEATURES)
        quality = sum(f[c] * fundamentals.EXPECTED[c] for c in ml.SIGNALS) / len(ml.SIGNALS)
        f["ret"] = 0.01 + 0.03 * quality + rng.normal(0, 0.03, n)
        f["month"], f["ticker"], f["close"] = m, [f"T{i}" for i in range(n)], 50.0
        f["sector"] = ["Technology", "Financials", "Energy"] * (n // 3)
        f["gone"] = pd.Series([None] * n, dtype=object)
        rows.append(f)
    return pd.concat(rows, ignore_index=True)


def test_portfolio_backtest_of_the_top_ideas(tmp_path):
    from trend_bot import model

    df = panel_with_edge()
    days = pd.bdate_range("2018-01-01", "2022-03-31")
    spy = pd.Series(np.linspace(100, 150, len(days)), index=days)
    spy[(spy.index >= "2020-03-01") & (spy.index < "2020-06-01")] = 50.0   # a crash: weather turns to cash
    m = ideas.backtest(df, spy, hold=20, buffer=40, cash_rate=2.0)
    assert {"mix", "mix_always", "industry_mix", "SPY", "SPY_weather", "average_stock"} <= set(m.columns)
    assert m.index.min() == pd.Timestamp("2019-02-28")                      # returns are next month's
    s = model.summarize(m, ["mix_always", "average_stock"])
    assert s.loc["mix_always", "cagr"] > s.loc["average_stock", "cagr"]     # the edge shows up after costs
    cash = (1.02) ** (1 / 12) - 1
    assert (~m["invested"]).any()
    assert m.loc[~m["invested"], "mix"].tolist() == pytest.approx([cash] * int((~m["invested"]).sum()))
    assert m["mix_turnover"].iloc[0] == 1.0 and m["mix_turnover"].iloc[1:].mean() < 1.0

    con = db.connect(tmp_path / "m.db")
    ideas.save_backtest(con, m, 20)
    assert "Holding the top 20 with the weather filter" in ideas.backtest_note(con)


def test_study_ideas_end_to_end(model_db, tmp_path, monkeypatch, capsys):
    from trend_bot.cli import main

    years = list(range(DATES[0].year - 1, DATES[-1].year))
    data = bulk_zip(years)
    monkeypatch.setattr(fundamentals.insiders, "CACHE_DIR", tmp_path / "sec")
    fundamentals.update_fundamentals(model_db, download=lambda p: (p.parent.mkdir(parents=True, exist_ok=True),
                                                                   p.write_bytes(data), p)[-1],
                                     log=lambda *_: None)
    assert main(["study", "ideas", "--db", str(tmp_path / "market.db"), "--hold", "5", "--buffer", "10",
                 "--split-year", "2026"]) == 0
    out = capsys.readouterr().out
    assert "Top ideas + weather filter" in out and "S&P 500 (buy & hold)" in out and "Year by year" in out
    assert json.loads(db.get_meta(model_db, "ideas_backtest"))["hold"] == 5


def ranking(order, closes=None):
    """A ranking in the shape rank_month returns, best first."""
    n = len(order)
    return pd.DataFrame({"score": np.linspace(1, 0, n), "pct": np.linspace(1, 0, n),
                         "close": [closes.get(t, 10.0) if closes else 10.0 for t in order],
                         "signals": 12, "sector": "Technology", "why": "cheap vs sales"}, index=order)


def test_portfolio_buys_top_and_sells_only_below_the_buffer(tmp_path):
    from trend_bot import track

    con = db.connect(tmp_path / "m.db")
    sunny, rainy = {"invest": True}, {"invest": False}
    names = [f"S{i:02d}" for i in range(60)]
    first = ideas.update_portfolio(con, ranking(names), "2026-09-30", sunny, hold=5, buffer=10)
    assert first["first"] and [p["ticker"] for p in first["buys"]] == names[:5] and first["sells"] == []

    # Next month: S00 slips to rank 8 (inside the buffer: kept), S01 to rank 30 (sold), S02 disappears.
    order = [n for n in names if n not in ("S00", "S01", "S02")]
    order.insert(7, "S00")
    order.insert(29, "S01")
    port = ideas.update_portfolio(con, ranking(order, {"S01": 12.0}), "2026-10-31", rainy, hold=5, buffer=10)
    assert not port["first"] and not port["invest"]
    assert {p["ticker"] for p in port["sells"]} == {"S01", "S02"}
    s01 = next(p for p in port["sells"] if p["ticker"] == "S01")
    assert s01["rank"] == 30 and s01["return"] == pytest.approx(0.2) and s01["since"] == "2026-09-30"
    held = [t for (t,) in con.execute("SELECT ticker FROM ideas_holdings ORDER BY rank")]
    assert "S00" in held and len(held) == 5
    assert {p["ticker"] for p in port["holds"]} == {"S00", "S03", "S04"}
    assert len(port["buys"]) == 2
    assert con.execute("SELECT since FROM ideas_holdings WHERE ticker = 'S00'").fetchone()[0] == "2026-09-30"
    assert ideas.load_portfolio(con)["date"] == "2026-10-31"

    con.execute("INSERT INTO prices(ticker, date, close) VALUES ('S01', '2026-10-30', 12.0)")
    new = track.record_ideas_trades(con, port)
    assert ("S01", "ideas_sell") in new and all((p["ticker"], "ideas_buy") in new for p in port["buys"])

    e = alerts.ideas_portfolio_embed(port, "note")
    assert "Caution" in e["description"] and "S01" in e["description"] and "+20%" in e["description"]


def test_monthly_portfolio_message_is_sent_once(model_db, tmp_path, monkeypatch):
    from trend_bot import alerts as a
    from trend_bot.cli import main

    ideas.update_portfolio(model_db, ranking([f"S{i:02d}" for i in range(30)]), "2026-09-30", {"invest": True},
                           hold=5, buffer=10)
    sent = []
    monkeypatch.setattr(a, "send", lambda url, payload, timeout=15: sent.append(payload))
    args = ["alert", "S01", "--from-db", "--db", str(tmp_path / "market.db"), "--webhook", "https://h",
            "--state", str(tmp_path / "s.json"), "--no-news", "--no-insiders", "--model"]
    assert main(args) == 0
    titles = [e["title"] for p in sent for e in p["embeds"]]
    assert titles.count("📋 Top-ideas portfolio - monthly update") == 1
    sent.clear()
    assert main(args) == 0
    assert not any(e["title"].startswith("📋") for p in sent for e in p["embeds"])
