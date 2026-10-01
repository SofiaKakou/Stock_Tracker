import datetime as dt
import json

import pytest

from trend_bot import alerts, news
from trend_bot.cli import main


def row(ticker, trend, action):
    return {"ticker": ticker, "date": dt.date(2026, 9, 28), "close": 100.0, "20d_chg": "+1.0%",
            "rsi": 55.0, "trend": trend, "action": action}


def test_find_changes_uses_saved_state():
    state = {"AAPL": {"trend": "UP"}, "MSFT": {"trend": "UP"}}
    rows = [row("AAPL", "UP", "HOLD"), row("MSFT", "DOWN/FLAT", "WAIT"),  # MSFT flipped while PC was off
            row("NVDA", "UP", "HOLD"), row("SPY", "UP", "BUY")]           # new tickers
    changed = [r["ticker"] for r in alerts.find_changes(rows, state)]
    assert changed == ["MSFT", "SPY"]
    new = alerts.updated_state(rows, state)
    assert new["MSFT"]["trend"] == "DOWN/FLAT" and new["NVDA"]["trend"] == "UP"


def test_load_webhook_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    env = tmp_path / ".env"
    env.write_text('# comment\nDISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/1/abc"\n')
    assert alerts.load_webhook(env) == "https://discord.com/api/webhooks/1/abc"
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://env")
    assert alerts.load_webhook(env) == "https://env"


def test_embeds_and_payload_split():
    h = [news.Headline("NVIDIA unveils new GPU", "Reuters", "https://x", None)]
    e = alerts.change_embed(row("NVDA", "UP", "BUY"), "50/200-day SMA crossover", "in 3d (Oct 01)", h)
    assert e["title"].endswith("NVDA") and e["color"] == alerts.GREEN
    names = [f["name"] for f in e["fields"]]
    assert "Next earnings" in names and "Recent headlines" in names
    assert "[NVIDIA unveils new GPU](https://x)" in e["fields"][-1]["value"]
    assert [len(p["embeds"]) for p in alerts.payloads([e] * 23)] == [10, 10, 3]


class FakeTicker:
    def __init__(self, calendar=None, items=None):
        self.calendar = calendar
        self.news = items


def test_next_earnings_picks_first_upcoming():
    today = dt.date(2026, 9, 29)
    t = FakeTicker(calendar={"Earnings Date": [dt.date(2026, 7, 1), dt.date(2026, 10, 2), dt.date(2026, 10, 9)]})
    assert news.next_earnings("X", today=today, ticker=t) == dt.date(2026, 10, 2)
    assert news.next_earnings("SPY", today=today, ticker=FakeTicker(calendar={})) is None
    assert news.earnings_note(dt.date(2026, 10, 2), today=today) == "in 3d (Oct 02)"


def test_headlines_parse_new_and_old_formats():
    items = [
        {"content": {"title": "New format", "pubDate": "2026-09-28T12:00:00Z",
                     "provider": {"displayName": "Yahoo"}, "canonicalUrl": {"url": "https://a"}}},
        {"title": "Old format", "publisher": "Reuters", "link": "https://b", "providerPublishTime": 1790700000},
        {"content": {}},  # junk is skipped
    ]
    got = news.headlines("X", ticker=FakeTicker(items=items))
    assert [h.title for h in got] == ["Old format", "New format"]  # newest first
    assert got[1].publisher == "Yahoo" and got[1].url == "https://a"


def test_cli_alert_sends_changes_and_saves_state(up_then_down, tmp_path, monkeypatch, capsys):
    csv = tmp_path / "p.csv"
    up_then_down.to_csv(csv)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({str(csv): {"trend": "UP"}}))  # it was up; now it's down
    sent = []
    monkeypatch.setattr(alerts, "send", lambda url, payload: sent.append(payload))
    args = ["alert", str(csv), "--fast", "10", "--slow", "50", "--webhook", "https://hook",
            "--state", str(state), "--no-news", "--summary"]
    assert main(args) == 0
    assert len(sent) == 1
    titles = [e["title"] for e in sent[0]["embeds"]]
    assert titles[0].startswith("🔴 SELL") and titles[1] == "Daily trend summary"
    assert json.loads(state.read_text())[str(csv)]["trend"] == "DOWN/FLAT"

    sent.clear()
    assert main(args[:-1]) == 0  # second run, no --summary: nothing changed, nothing sent
    assert sent == []


def test_cli_alert_without_webhook_fails(monkeypatch, tmp_path):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["alert", "--test"]) == 2
