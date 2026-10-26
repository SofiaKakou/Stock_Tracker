import datetime as dt
import json

import pytest

from trend_bot import db, paper


class FakeBroker:
    """Stands in for Alpaca's paper account: records orders, fills nothing."""

    def __init__(self, equity=100_000.0, positions=None, open_orders=None, refuse=(), market_open=False):
        self.equity, self.held = equity, dict(positions or {})
        self.waiting, self.refuse, self.sent = list(open_orders or []), set(refuse), []
        self.market_open, self.closed_orders = market_open, []

    def clock(self):
        return {"is_open": self.market_open}

    def _call(self, method, path, params=None):
        assert (method, path, params["status"]) == ("GET", "/orders", "closed")
        return self.closed_orders

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
    # Refused at night (market closed): not marked done, so the morning check tries again.
    res = paper.sync(con, FakeBroker(refuse={"BRK.B"}), log=lambda *_: None)
    assert [f[0] for f in res["failed"]] == ["BRK.B"] and len(res["traded"]) == 2 and res["retry"]
    assert db.get_meta(con, "paper_synced") is None
    assert "Refused by the broker" in paper.embed(res, "2026-11-02")["description"]
    # Refused again with the market open: final for this month (the broker doesn't offer it).
    held = {"AAA": 48, "CCC": 98}
    res = paper.sync(con, FakeBroker(positions=held, refuse={"BRK.B"}, market_open=True), log=lambda *_: None)
    assert [f[0] for f in res["failed"]] == ["BRK.B"] and db.get_meta(con, "paper_synced") == "2026-11-02"


def test_a_stock_pricier_than_its_amount_gets_one_share_or_is_named():
    left = []
    orders = paper.plan(["AAA", "BIG", "HUGE"], {"AAA": 100.0, "BIG": 5_000.0, "HUGE": 50_000.0}, {},
                        equity=10_000, left_out=left)                       # 3,267 each
    assert ("BIG", "buy", 1) in orders and not any(s == "HUGE" for s, _, _ in orders)
    assert left == [("HUGE", 50_000.0)]


def test_morning_fill_report_and_the_cost_of_buying_next_morning(tmp_path):
    con = setup(tmp_path)
    broker = FakeBroker(positions={"OLD": 10})
    con.execute("INSERT INTO prices(ticker, date, close, adj_close) VALUES ('OLD', '2026-10-30', 20, 20)")
    paper.sync(con, broker, today=dt.date(2026, 10, 30), log=lambda *_: None)
    assert paper.fills(con, broker) is None                                 # nothing filled yet
    qty = {s: q for s, side, q in broker.sent}
    broker.closed_orders = [
        {"symbol": "AAA", "side": "buy", "status": "filled", "filled_avg_price": "101", "filled_qty": str(qty["AAA"]),
         "client_order_id": "tb-x"},
        {"symbol": "OLD", "side": "sell", "status": "filled", "filled_avg_price": "19", "filled_qty": "10",
         "client_order_id": "abc"},
        {"symbol": "SPY", "side": "buy", "status": "filled", "filled_avg_price": "500", "client_order_id": "tb-test-1"}]
    f = paper.fills(con, broker)
    assert f["waiting"] == 2 and {x[0] for x in f["filled"]} == {"AAA", "OLD"}
    # Paid $1 more per AAA share, got $1 less per OLD share: the cost over the money traded.
    expected = (qty["AAA"] * 1 + 10 * 1) / (qty["AAA"] * 100 + 10 * 20)
    assert f["cost"] == pytest.approx(expected)
    e = paper.fills_embed(f)
    assert "bought" in e["description"] and "sold 10 OLD" in e["description"] and "not filled yet" in e["description"]
    for s in ("BRK.B", "CCC"):
        broker.closed_orders.append({"symbol": s, "side": "buy", "status": "filled", "filled_avg_price": "1",
                                     "filled_qty": "1", "client_order_id": "tb-y"})
    assert paper.fills(con, broker)["waiting"] == 0
    assert paper.fills(con, broker) is None                                 # reported once



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


def test_the_demo_line_compares_with_the_bots_own_record(tmp_path):
    con = setup(tmp_path)
    con.executemany("INSERT INTO ideas_periods VALUES (?, ?, ?)", [("2026-10-30", t, 1) for t in ("AAA", "CCC")])
    con.executemany("INSERT INTO prices(ticker, date, close, adj_close) VALUES (?, ?, ?, ?)",
                    [("AAA", "2026-11-02", 100.0, 100.0), ("CCC", "2026-11-02", 50.0, 50.0),
                     ("AAA", "2026-11-20", 110.0, 110.0), ("CCC", "2026-11-20", 50.0, 50.0)])
    db.set_meta(con, "paper_status", json.dumps({"start_date": "2026-11-02", "start_equity": 100_000,
                                                 "date": "2026-11-20", "equity": 104_000}))
    r = paper.record(con)
    assert r["bot"] == pytest.approx((1 + 0.05) * (1 - 0.001) - 1)        # AAA +10%, CCC 0%: +5% less costs
    line = paper.line(con)
    assert "+4.0%" in line and "bot's own record over the same days" in line and "+4.9%" in line


def test_left_out_stocks_are_named_in_the_message():
    e = paper.embed({"traded": [], "failed": [], "left_out": [("NVR", 7000.0)], "status": {"equity": 1e5, "cash": 1e5}},
                    "2026-11-02")
    assert "NVR ($7,000 a share)" in e["description"]


# --- The $100 account: the same portfolio in dollar amounts (fractional shares) ---------------------

class SmallBroker(FakeBroker):
    """The $100 account: dollar orders; BRK.B can't be bought in fractions."""

    def __init__(self, **kw):
        super().__init__(equity=100.0, **kw)
        self.values = {}

    def _call(self, method, path, params=None):
        if path.startswith("/assets/"):
            sym = path.split("/")[-1]
            return {"symbol": sym, "tradable": True, "fractionable": sym != "BRK.B"}
        return super()._call(method, path, params)

    def positions(self):
        return [{"symbol": s, "qty": "0.1", "market_value": str(v)} for s, v in self.values.items()]

    def order_notional(self, symbol, dollars, side, tag):
        assert len(tag) <= 48 and dollars >= paper.MIN_ORDER
        self.sent.append((symbol, side, dollars))


def test_plan_in_dollars_buys_equal_amounts_and_skips_what_cant_be_split():
    left = []
    orders = paper.plan_dollars(["AAA", "BRK-B", "CCC"], {"OLD": 3.0, "CCC": 10.0}, equity=100.0,
                                tradable={"AAA", "CCC"}, left_out=left)
    each = 100 * paper.INVESTED / 2                           # $49 each: BRK.B left out
    assert orders[0] == ("OLD", "close", 0.0) and left == [("BRK.B", 0.0)]
    assert ("AAA", "buy", round(each, 2)) in orders and ("CCC", "buy", round(each - 10, 2)) in orders
    assert paper.plan_dollars(["AAA"], {"AAA": 97.5}, equity=100.0) == []        # within 10%: left alone


def test_the_100_account_keeps_its_own_notes_and_orders_in_dollars(tmp_path):
    con = setup(tmp_path)
    broker = SmallBroker()
    res = paper.sync(con, broker, today=dt.date(2026, 11, 2), log=lambda *_: None, prefix="small", fractional=True)
    assert {s for s, _, _ in res["traded"]} == {"AAA", "CCC"} and res["left_out"] == [("BRK.B", 0.0)]
    assert all(side == "buy" and amount == pytest.approx(49.0) for _, side, amount in broker.sent)
    assert db.get_meta(con, "small_synced") == "2026-11-02" and db.get_meta(con, "paper_synced") is None
    assert json.loads(db.get_meta(con, "small_status"))["start_equity"] == 100.0
    e = paper.embed(res, "2026-11-02", small=True)
    assert "$100 account" in e["title"] and "buy $49.00 of AAA" in e["description"]
    assert "in fractions: BRK.B" in e["description"]
    assert "$100 account" in paper.line(con, prefix="small") and paper.line(con) == ""   # the monthly one is untouched


def test_the_small_account_uses_its_own_keys(tmp_path, monkeypatch, capsys):
    from trend_bot.cli import main

    monkeypatch.delenv(paper.SMALL_KEY_ENV, raising=False)
    monkeypatch.setenv(paper.KEY_ENV, "k")
    monkeypatch.setenv(paper.SECRET_ENV, "s")
    assert main(["paper", "--account", "small", "--db", str(tmp_path / "m.db")]) == 0
    assert "ALPACA_SMALL_KEY_ID" in capsys.readouterr().out       # never falls back to the monthly account's keys
