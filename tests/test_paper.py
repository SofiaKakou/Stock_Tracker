import datetime as dt
import json

import pytest

from trend_bot import db, paper


class FakeBroker:
    """Stands in for Alpaca's paper account: records orders, fills nothing."""

    def __init__(self, equity=100_000.0, positions=None, open_orders=None, refuse=()):
        self.equity, self.held = equity, dict(positions or {})
        self.waiting, self.refuse, self.sent = list(open_orders or []), set(refuse), []

    def account(self):
        return {"equity": str(self.equity), "cash": "1000", "account_number": "PA123"}

    def positions(self):
        return [{"symbol": s, "qty": str(q)} for s, q in self.held.items()]

    def open_orders(self):
        return self.waiting

    def order(self, symbol, qty, side, tag):
        if symbol in self.refuse:
            raise paper.PaperError(f"POST /orders: 422 asset {symbol} is not tradable")
        assert len(tag) <= 48 and tag.startswith("tb-")
        self.sent.append((symbol, side, qty))

    def close(self, symbol):
        self.sent.append((symbol, "close", self.held[symbol]))


def setup(tmp_path, buys=("AAA", "BRK-B"), holds=("CCC",), date="2026-11-02"):
    con = db.connect(tmp_path / "m.db")
    rows = [("AAA", "2026-10-30", 100.0), ("BRK-B", "2026-10-30", 400.0), ("CCC", "2026-10-30", 50.0),
            ("SPY", "2026-10-30", 500.0), ("SPY", "2026-11-20", 510.0)]
    con.executemany("INSERT INTO prices(ticker, date, close, adj_close) VALUES (?, ?, ?, ?)",
                    [(t, d, c, c) for t, d, c in rows])
    db.set_meta(con, "ideas_portfolio", json.dumps({"date": date, "buys": [{"ticker": t} for t in buys],
                                                    "holds": [{"ticker": t} for t in holds], "sells": []}))
    con.commit()
    return con


def test_the_address_is_fixed_to_paper_trading():
    assert paper.PAPER_URL == "https://paper-api.alpaca.markets/v2"
    client = paper.Paper("k", "s", session=type("S", (), {"headers": {}})())
    assert client.http.headers == {"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"}


def test_plan_sells_what_left_then_buys_equal_amounts():
    orders = paper.plan(["AAA", "BRK-B", "CCC"], {"AAA": 100.0, "BRK-B": 400.0, "CCC": 50.0},
                        {"OLD": 7, "CCC": 100}, equity=30_000)
    each = 30_000 * paper.INVESTED / 3                       # 9,800 per stock
    assert orders[0] == ("OLD", "close", 7)
    assert ("AAA", "buy", int(each // 100)) in orders and ("BRK.B", "buy", int(each // 400)) in orders
    assert ("CCC", "buy", int(each // 50) - 100) in orders    # tops up from $5,000 to ~$9,800
    assert [s for s, side, _ in orders].index("OLD") == 0       # sells first


def test_small_differences_are_left_alone():
    assert paper.plan(["AAA"], {"AAA": 100.0}, {"AAA": 95}, equity=10_000) == []      # $9,500 vs $9,800
    assert paper.plan(["AAA"], {"AAA": 100.0}, {"AAA": 130}, equity=10_000) == [("AAA", "sell", 32)]


def test_sync_follows_a_new_portfolio_once_and_records_the_account(tmp_path):
    con = setup(tmp_path)
    broker = FakeBroker(equity=100_000.0)
    res = paper.sync(con, broker, today=dt.date(2026, 11, 2), log=lambda *_: None)
    assert {s for s, _, _ in res["traded"]} == {"AAA", "BRK.B", "CCC"} and not res["failed"]
    assert db.get_meta(con, "paper_synced") == "2026-11-02"
    again = paper.sync(con, broker, today=dt.date(2026, 11, 3), log=lambda *_: None)
    assert again["skipped"].startswith("already matches") and not again["traded"]
    assert len(broker.sent) == 3                                   # nothing sent twice

    broker.equity = 103_000.0
    paper.sync(con, broker, today=dt.date(2026, 11, 20), log=lambda *_: None)
    r = paper.record(con)
    assert r["start"] == "2026-11-02" and r["account"] == pytest.approx(0.03) and r["spy"] == pytest.approx(0.02)
    assert "Demo account" in paper.line(con) and "+3.0%" in paper.line(con)
    e = paper.embed(res, "2026-11-02")
    assert "fake money" in e["description"] and "buy" in e["description"]


def test_waits_while_earlier_orders_are_open_and_notes_refused_stocks(tmp_path):
    con = setup(tmp_path)
    res = paper.sync(con, FakeBroker(open_orders=[{"id": "x"}]), log=lambda *_: None)
    assert "haven't filled" in res["skipped"] and db.get_meta(con, "paper_synced") is None
    res = paper.sync(con, FakeBroker(refuse={"BRK.B"}), log=lambda *_: None)
    assert [f[0] for f in res["failed"]] == ["BRK.B"] and len(res["traded"]) == 2
    assert "Refused by the broker" in paper.embed(res, "2026-11-02")["description"]


def test_dry_run_sends_and_saves_nothing(tmp_path):
    con = setup(tmp_path)
    broker = FakeBroker()
    res = paper.sync(con, broker, dry_run=True, log=lambda *_: None)
    assert len(res["planned"]) == 3 and not broker.sent
    assert db.get_meta(con, "paper_synced") is None and db.get_meta(con, "paper_status") is None
    assert paper.line(con) == ""


def test_command_does_nothing_without_keys(tmp_path, monkeypatch, capsys):
    from trend_bot.cli import main

    monkeypatch.delenv(paper.KEY_ENV, raising=False)
    monkeypatch.delenv(paper.SECRET_ENV, raising=False)
    assert main(["paper", "--db", str(tmp_path / "m.db")]) == 0
    assert "isn't set up" in capsys.readouterr().out


class TradingBroker(FakeBroker):
    """Fills orders on the second look (like a real market order a moment later)."""

    def __init__(self, open_=True, never_fill=False):
        super().__init__()
        self.open_, self.never_fill, self.orders, self.cancelled = open_, never_fill, {}, []

    def clock(self):
        return {"is_open": self.open_, "next_open": "2026-10-26T09:30:00-04:00"}

    def order(self, symbol, qty, side, tag):
        oid = f"o{len(self.orders)}"
        self.orders[oid] = {"id": oid, "status": "new", "looks": 0, "side": side}
        return {"id": oid, "status": "new"}

    def get_order(self, oid):
        o = self.orders[oid]
        o["looks"] += 1
        if o["looks"] >= 2 and not self.never_fill:
            o.update(status="filled", filled_avg_price="500.25" if o["side"] == "buy" else "500.30")
        return dict(o)

    def cancel(self, oid):
        self.cancelled.append(oid)


def test_test_trade_buys_and_sells_one_share():
    out = []
    assert paper.test_trade(TradingBroker(), sleep=lambda s: None, log=out.append)
    assert "bought 1 SPY at $500.25" in out[0] and "sold 1 SPY at $500.30" in out[1]


def test_test_trade_waits_for_the_market_and_cancels_what_doesnt_fill():
    out = []
    broker = TradingBroker(open_=False)
    assert not paper.test_trade(broker, sleep=lambda s: None, log=out.append)
    assert "market is closed" in out[0] and not broker.orders            # nothing sent while closed
    broker = TradingBroker(never_fill=True)
    assert not paper.test_trade(broker, wait=4, sleep=lambda s: None, log=out.append)
    assert broker.cancelled == ["o0"] and "didn't fill" in out[-1]
