import datetime as dt
import json

import pytest

from trend_bot import alerts, insiders
from trend_bot.cli import main

FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
    <schemaVersion>X0508</schemaVersion>
    <documentType>4</documentType>
    <periodOfReport>{date}</periodOfReport>
    <aff10b5One>{planned}</aff10b5One>
    <issuer><issuerCik>0001045810</issuerCik><issuerName>NVIDIA CORP</issuerName>
        <issuerTradingSymbol>NVDA</issuerTradingSymbol></issuer>
    <reportingOwner>
        <reportingOwnerId><rptOwnerCik>0001197647</rptOwnerCik><rptOwnerName>{name}</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>{director}</isDirector><isOfficer>{officer}</isOfficer>
            <officerTitle>{title}</officerTitle><isTenPercentOwner>0</isTenPercentOwner><isOther>0</isOther>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>{date}</value></transactionDate>
            <transactionCoding><transactionFormType>4</transactionFormType>
                <transactionCode>{code}</transactionCode><equitySwapInvolved>0</equitySwapInvolved></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>{shares}</value></transactionShares>
                <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts><sharesOwnedFollowingTransaction><value>50000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>{date}</value></transactionDate>
            <transactionCoding><transactionFormType>4</transactionFormType>
                <transactionCode>F</transactionCode><equitySwapInvolved>0</equitySwapInvolved></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>100</value></transactionShares>
                <transactionPricePerShare><footnoteId id="F1"/></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>"""


def form4(name="DOE JANE", code="P", date="2026-09-10", shares=1000, price=200.5,
          director=1, officer=0, title="", planned=0):
    return FORM4.format(name=name, code=code, date=date, shares=shares, price=price,
                        director=director, officer=officer, title=title, planned=planned)


def test_parse_form4():
    trades = insiders.parse_form4(form4(officer=1, title="EVP, CFO", planned=1), "nvda", dt.date(2026, 9, 12), "acc-1")
    assert [t.code for t in trades] == ["P", "F"]
    buy = trades[0]
    assert buy.ticker == "NVDA" and buy.insider == "Doe Jane" and buy.role == "EVP, CFO, Director"
    assert buy.shares == 1000 and buy.price == 200.5 and buy.value == pytest.approx(200_500)
    assert buy.date == dt.date(2026, 9, 10) and buy.owned_after == 50_000 and buy.planned
    assert trades[1].price is None and trades[1].kind == "TAX WITHHOLDING"


def trade(name, day, code="P", shares=100, price=10.0, acc=None):
    return insiders.InsiderTrade("X", dt.date(2026, 9, day), dt.date(2026, 9, day), name, "Director",
                                 code, shares, price, None, False, acc or f"acc-{name}-{day}")


def test_cluster_buy():
    lone = [trade("A", 20), trade("A", 5)]
    assert insiders.cluster_buy(lone) is None  # one insider twice isn't a cluster
    group = [trade("B", 25), trade("C", 20, code="S"), trade("A", 10)]
    cluster = insiders.cluster_buy(group, window_days=30)
    assert {t.insider for t in cluster} == {"A", "B"}
    assert insiders.cluster_buy(group, window_days=10) is None  # 15 days apart


def test_summary_text():
    s = insiders.summarize([trade("A", 1, shares=1000, price=2500), trade("B", 2, "S", 10, 100)])
    assert s.short == "1 buy $2.5M / 1 sell $1.0K"
    assert insiders.summarize([]).short == "none"


def test_cluster_alerted_only_once():
    cluster = [trade("B", 25, acc="new"), trade("A", 10)]
    state = {}
    assert alerts.new_cluster("X", cluster, state)
    alerts.mark_cluster("X", cluster, state)
    assert not alerts.new_cluster("X", cluster, state)
    state = alerts.updated_state([{"ticker": "X", "trend": "UP", "date": "d", "close": 1}], state)
    assert state["X"]["insider_alerted"] == "new"  # survives the trend update
    e = alerts.insider_embed("X", cluster, "UP")
    assert "2 insiders" in e["description"] and e["color"] == alerts.GOLD


@pytest.fixture
def fake_sec(tmp_path, monkeypatch):
    """Serve fake SEC responses for NVDA (CIK 1045810) and record requested URLs."""
    monkeypatch.setattr(insiders, "CACHE_DIR", tmp_path / "sec")
    monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
    today = dt.date.today()
    d = lambda n: (today - dt.timedelta(days=n)).isoformat()
    docs = {
        "000104581026000003": form4("SMITH JOHN", "P", d(3), 500, 180),
        "000104581026000002": form4("DOE JANE", "P", d(12), 1000, 175),
        "000104581026000001": form4("BIG SELLER", "S", d(20), 9000, 190),
    }
    submissions = {"filings": {"recent": {
        "form": ["4", "4", "10-Q", "4", "4"],
        "accessionNumber": ["0001045810-26-000003", "0001045810-26-000002", "0001045810-26-000009",
                            "0001045810-26-000001", "0001045810-25-000001"],
        "filingDate": [d(1), d(10), d(15), d(18), d(400)],
        "primaryDocument": ["xslF345X05/f3.xml", "xslF345X05/f2.xml", "q.htm", "xslF345X05/f1.xml", "old.xml"],
    }}}
    requested = []

    def fake_get(url):
        requested.append(url)
        if url.endswith("company_tickers.json"):
            return json.dumps({"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA"}}).encode()
        if "submissions" in url:
            return json.dumps(submissions).encode()
        acc = url.split("/")[-2]
        assert "xslF345X05" not in url  # must fetch the raw XML, not the styled view
        return docs[acc].encode()

    monkeypatch.setattr(insiders, "_get", fake_get)
    return requested


def test_fetch_trades_end_to_end(fake_sec):
    trades = insiders.fetch_trades("NVDA", days=90)
    assert [t.insider for t in trades] == ["Smith John", "Doe Jane", "Big Seller"]  # newest first, F dropped
    assert insiders.fetch_trades("SPY") == []  # not in the SEC ticker list
    n = len(fake_sec)
    insiders.fetch_trades("NVDA", days=90)
    assert len(fake_sec) == n  # everything served from cache the second time


def test_missing_user_agent(monkeypatch, tmp_path):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        insiders.user_agent()
    (tmp_path / ".env").write_text("DISCORD_WEBHOOK_URL=x\nSEC_USER_AGENT=Jane Doe jane@example.com\n")
    assert insiders.user_agent() == "Jane Doe jane@example.com"


def test_cli_insiders(fake_sec, capsys):
    assert main(["insiders", "NVDA"]) == 0
    out = capsys.readouterr().out
    assert "Cluster buy: 2 insiders" in out and "Smith John" in out and "BUY" in out


def test_cli_alert_sends_insider_cluster_once(fake_sec, tmp_path, monkeypatch, up_then_down):
    import trend_bot.commands.common as common

    monkeypatch.setattr(common, "load_prices", lambda *a, **k: up_then_down)
    sent = []
    monkeypatch.setattr(alerts, "send", lambda url, payload: sent.append(payload))
    state = tmp_path / "state.json"
    args = ["alert", "NVDA", "--fast", "10", "--slow", "50", "--webhook", "https://hook",
            "--state", str(state), "--no-news"]
    assert main(args) == 0
    titles = [e["title"] for p in sent for e in p["embeds"]]
    assert titles == ["🔔 Insider buying  NVDA"]
    sent.clear()
    assert main(args) == 0
    assert sent == []  # same cluster isn't announced twice


def test_sec_rate_limit_waits_then_continues(monkeypatch):
    import requests as rq

    monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
    monkeypatch.setattr(insiders, "REQUEST_GAP", 0)
    sleeps = []
    monkeypatch.setattr(insiders.time, "sleep", lambda s: sleeps.append(s))
    codes = iter([200, 403, 403, 200])

    class Resp:
        def __init__(self, code):
            self.status_code, self.content = code, b"ok"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(insiders.requests, "get", lambda *a, **k: Resp(next(codes)))
    monkeypatch.setattr(insiders, "_ok_requests", 0)
    assert insiders._get("u1") == b"ok"
    assert insiders._get("u2") == b"ok"  # two refusals, then success
    assert sleeps == [60, 180]

    # Refused on the very first request: a User-Agent problem, reported right away.
    monkeypatch.setattr(insiders, "_ok_requests", 0)
    monkeypatch.setattr(insiders.requests, "get", lambda *a, **k: Resp(403))
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        insiders._get("u3")
