import datetime as dt

import numpy as np
import pandas as pd
import pytest

from trend_bot import cryptotrade as ct
from trend_bot.paper import PaperError

NOW = dt.datetime(2026, 10, 27, 15, 2, tzinfo=dt.timezone.utc)


def bars(moves: dict[str, float], end=NOW):
    """3 hours of 15-minute bars per coin; the last complete bar ends at `end`'s quarter hour; each coin
    moves `moves[coin]` over its last hour."""
    last_start = pd.Timestamp(end).floor("15min") - pd.Timedelta(minutes=15)
    rows = []
    for sym, move in moves.items():
        ts = pd.date_range(end=last_start, periods=12, freq="15min")
        closes = np.r_[[100.0] * 8, np.linspace(100, 100 * (1 + move), 5)[1:]]
        for t, c in zip(ts, closes):
            rows.append((sym, t.strftime("%Y-%m-%dT%H:%M:%SZ"), c, c, c, c, 1.0))
        rows.append((sym, (last_start + pd.Timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"), c, c, c, c, 1.0))  # still forming
    return pd.DataFrame(rows, columns=["symbol", "t", "open", "high", "low", "close", "volume"])


class Broker:
    def __init__(self, positions=None, cash=100.0):
        self.held = {p["symbol"]: p for p in (positions or [])}
        self.cash, self.orders, self.closed, self.cancelled = cash, [], [], []
        self.book = {}

    def positions(self):
        return list(self.held.values())

    def account(self):
        return {"cash": str(self.cash), "equity": "100.5", "last_equity": "100.0"}

    def close(self, symbol):
        if symbol == "STUCK":
            raise PaperError("403 held for orders")
        self.closed.append(symbol)
        p = self.held.pop(symbol)
        return {"id": f"close-{symbol}", "status": "filled", "filled_avg_price": p.get("current_price", "1")}

    def cancel(self, oid):
        self.cancelled.append(oid)
        if oid in self.book and self.book[oid]["status"] != "filled":
            self.book[oid]["status"] = "canceled"

    def get_order(self, oid):
        return self.book.get(oid, {"id": oid, "status": "filled", "filled_avg_price": "85"})

    def fill(self, oid, price, qty=None):
        """The market came to a waiting order."""
        o = self.book[oid]
        q = qty if qty is not None else o["qty"]
        o.update(status="filled", filled_avg_price=str(price), filled_qty=str(q), filled_at=NOW.isoformat())
        sym = o["symbol"].replace("/", "")
        if o["side"] == "buy":
            self.held[sym] = {"symbol": sym, "qty": f"{q * 0.9985:.8f}", "asset_class": "crypto", "current_price": str(price)}
        else:
            self.held.pop(sym, None)

    def buy_limit(self, symbol, qty, limit, tag, tif="ioc"):
        self.orders.append(("buy", symbol, qty, limit, tag, tif))
        oid = f"b-{symbol}"
        self.book[oid] = {"id": oid, "symbol": symbol, "side": "buy", "qty": qty, "status": "new", "filled_qty": "0"}
        return self.book[oid]

    def sell_limit(self, symbol, qty, limit, tag):
        self.orders.append(("sell", symbol, qty, limit, tag))
        oid = f"s-{symbol}"
        self.book[oid] = {"id": oid, "symbol": symbol, "side": "sell", "qty": float(qty), "status": "new"}
        return self.book[oid]

    def stop_sell(self, symbol, qty, stop, limit, tag):
        self.orders.append(("stop", symbol, qty, stop, limit, tag))
        return {"id": f"stop-{symbol}"}


class Data:
    def __init__(self, moves):
        self.moves = moves


@pytest.fixture
def market(monkeypatch):
    def use(moves, ask=95.0):
        monkeypatch.setattr(ct.crypto, "fetch", lambda data, coins, start, end: bars(moves))
        monkeypatch.setattr(ct, "latest_quotes", lambda data, syms: {s: {"bid": ask - 0.1, "ask": ask} for s in syms})
    return use


def quiet(broker, state, now=NOW):
    return ct.run(broker, Data(None), state, now=now, log=lambda *_: None, sleep=lambda s: None)


def test_a_dip_gets_a_buy_that_waits_at_the_bid_and_a_safety_stop_once_filled(market):
    market({"BTC/USD": -0.01, "SOL/USD": -0.06, "ETH/USD": -0.025})      # 2.5%: the stopped experiment, not bought
    broker, state = Broker(), {}
    assert quiet(broker, state) == []
    buy = [o for o in broker.orders if o[0] == "buy"]
    assert [o[1] for o in buy] == ["SOL/USD"] and buy[0][5] == "gtc"
    limit = 94.9 * (1 - ct.OFFSET)
    assert buy[0][3] == pytest.approx(limit) and buy[0][2] * limit == pytest.approx(ct.AMOUNT)   # a little below the bid
    assert list(state["buying"]) == ["SOLUSD"] and not state["open"]
    broker.fill("b-SOL/USD", limit)
    market({})
    out = quiet(broker, state, NOW + dt.timedelta(minutes=15))
    stop = [o for o in broker.orders if o[0] == "stop"][0]
    assert stop[3] == pytest.approx(limit * 0.85) and stop[2] == state["open"]["SOLUSD"]["qty"]   # the qty after the fee
    assert not state["buying"] and "Bought SOL" in out[0]["title"]


def test_a_buy_the_price_never_came_back_to_is_cancelled_after_its_wait(market):
    market({"SOL/USD": -0.06})
    broker, state = Broker(), {}
    quiet(broker, state)
    market({})
    quiet(broker, state, NOW + dt.timedelta(minutes=15))
    assert state["buying"] and not broker.cancelled                      # still within its wait
    out = quiet(broker, state, NOW + ct.WAIT_BUY)
    assert broker.cancelled == ["b-SOL/USD"] and not state["buying"] and not state["open"] and out == []


def test_held_a_day_a_sell_waits_at_the_ask_then_the_rest_goes_at_the_market(market):
    market({})
    t0 = NOW - dt.timedelta(days=1, minutes=1)
    broker = Broker([{"symbol": "SOLUSD", "qty": "0.26", "asset_class": "crypto", "current_price": "99"}])
    state = {"open": {"SOLUSD": {"symbol": "SOL/USD", "time": t0.isoformat(), "price": 95.0, "qty": "0.26",
                                 "cost": 24.7, "stop_id": "s1"}}}
    assert quiet(broker, state) == []
    assert broker.cancelled == ["s1"] and ("sell", "SOL/USD", "0.26", 95.0, broker.orders[0][4]) == broker.orders[0]
    out = quiet(broker, state, NOW + ct.WAIT_SELL)                      # nobody paid the ask in 15 minutes
    assert "s-SOL/USD" in broker.cancelled and broker.closed == ["SOLUSD"] and not state["open"]
    assert state["log"][0]["ret"] == pytest.approx(99 / 95 - 1) and "+4.2%" in out[0]["title"]


def test_a_waiting_sell_that_fills_is_recorded_at_its_price(market):
    market({})
    t0 = NOW - dt.timedelta(days=1, minutes=1)
    broker = Broker([{"symbol": "SOLUSD", "qty": "0.26", "asset_class": "crypto", "current_price": "99"}])
    state = {"open": {"SOLUSD": {"symbol": "SOL/USD", "time": t0.isoformat(), "price": 95.0, "qty": "0.26",
                                 "cost": 24.7, "stop_id": "s1"}}}
    quiet(broker, state)
    broker.fill("s-SOL/USD", 95.0 * 1.05)
    out = quiet(broker, state, NOW + dt.timedelta(minutes=15))
    assert not broker.closed and state["log"][0]["ret"] == pytest.approx(0.05) and state["log"][0]["how"] == "day"
    assert "a day later" in out[0]["title"]


def test_not_yet_a_day_it_is_kept_and_a_stopped_one_is_recorded(market):
    market({})
    broker = Broker([{"symbol": "ETHUSD", "qty": "0.01", "asset_class": "crypto"}])
    state = {"open": {"ETHUSD": {"symbol": "ETH/USD", "time": (NOW - dt.timedelta(hours=5)).isoformat(),
                                 "price": 2500.0, "qty": "0.01", "cost": 25.0},
                      "SOLUSD": {"symbol": "SOL/USD", "time": (NOW - dt.timedelta(hours=2)).isoformat(),
                                 "price": 100.0, "qty": "0.25", "cost": 25.0, "stop_id": "s2"}}}
    out = quiet(broker, state)
    assert list(state["open"]) == ["ETHUSD"] and not broker.closed
    assert state["log"][0]["how"] == "stop" and state["log"][0]["ret"] == pytest.approx(-0.15)
    assert "safety stop" in out[0]["title"]


def test_leftover_stocks_are_sold_once_announced(market):
    market({})
    broker = Broker([{"symbol": "AAPL", "qty": "0.02", "asset_class": "us_equity"},
                     {"symbol": "MSFT", "qty": "0.01", "asset_class": "us_equity"}])
    state = {}
    out = quiet(broker, state)
    assert sorted(broker.closed) == ["AAPL", "MSFT"] and state["stocks_sold"] and "now a crypto account" in out[0]["title"]


def test_at_most_five_coins_held_or_being_bought_and_a_stale_signal_is_ignored(market):
    market({f"C{i}/USD": -0.06 for i in range(7)})
    broker, state = Broker(cash=1000.0), {}
    quiet(broker, state)
    assert len(state["buying"]) == ct.SLOTS
    late = NOW + dt.timedelta(minutes=40)                       # GitHub started the check 40 minutes late
    assert ct.dips(bars({"X/USD": -0.06}), late) == {}


def test_a_trade_from_the_stopped_fast_experiment_is_still_sold_after_an_hour(market):
    market({})
    broker = Broker([{"symbol": "ETHUSD", "qty": "0.004", "asset_class": "crypto", "current_price": "2480"}])
    state = {"open": {"ETHUSD": {"symbol": "ETH/USD", "time": (NOW - dt.timedelta(minutes=61)).isoformat(),
                                 "price": 2500.0, "qty": "0.004", "cost": 10.0, "kind": "fast"}}}
    quiet(broker, state)
    assert broker.orders[0][0] == "sell"


def test_the_evening_summary_reads_well():
    state = {"open": {}, "log": [{"symbol": "SOL/USD", "in": "2026-10-26T15:02:00+00:00", "out": NOW.isoformat(),
                                  "entry": 95.0, "exit": 99.0, "how": "day", "cost": 24.7, "ret": 99 / 95 - 1,
                                  "kind": "big"},
                                 {"symbol": "ETH/USD", "in": "2026-10-27T13:02:00+00:00", "out": NOW.isoformat(),
                                  "entry": 2500.0, "exit": 2490.0, "how": "hour", "cost": 10.0, "ret": -0.004,
                                  "kind": "fast"}]}
    d = ct.summary(Broker(), state, now=NOW)["description"]
    assert "$100.50" in d and "SOL +4.2%" in d and "dip 4%+, held a day**: 1 trades" in d
    assert "2% experiment (stopped)" in d and "-0.40%" in d


def test_all_the_brokers_coins_except_the_stable_ones_read_once_a_day(monkeypatch):
    calls = []

    def listed(broker):
        calls.append(1)
        return ["BTC/USD", "DOGE/USD", "USDT/USD", "PAXG/USD", "USDC/USD"]

    monkeypatch.setattr(ct.crypto, "all_coins", listed)
    state = {}
    assert ct.coins(None, state, NOW.date()) == ["BTC/USD", "DOGE/USD"]
    assert ct.coins(None, state, NOW.date()) == ["BTC/USD", "DOGE/USD"] and len(calls) == 1   # once a day

    def broken(broker):
        raise RuntimeError("403")

    monkeypatch.setattr(ct.crypto, "all_coins", broken)
    assert ct.coins(None, {}, NOW.date()) == ct.COINS                  # the big ones if the list can't be read


def test_diagnose_lists_the_accounts_record_and_the_recent_dips(monkeypatch):
    class B(Broker):
        def _call(self, method, path, **kw):
            assert method == "GET" and path == "/account/activities"
            return [{"activity_type": "FILL", "transaction_time": "2026-10-27T13:30:01Z", "side": "sell",
                     "qty": "1", "symbol": "SPY", "price": "580", "order_status": "filled"},
                    {"activity_type": "CFEE", "date": "2026-10-27", "symbol": "BTC/USD", "net_amount": "-0.02"}]

    t = pd.date_range(pd.Timestamp("2026-10-20", tz="UTC"), NOW, freq="15min")
    c = np.full(len(t), 100.0)
    c[400:] = 96.0                        # one 4% fall in an hour, then flat
    df = pd.DataFrame({"symbol": "SOL/USD", "t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": c, "high": c,
                       "low": c, "close": c, "volume": 1.0})
    monkeypatch.setattr(ct.crypto, "fetch", lambda data, coins, start: df)
    state = {"coins": {"date": NOW.date().isoformat(), "list": ["SOL/USD"]}}
    out = ct.diagnose(B(), Data({}), state, now=NOW, days=10, log=lambda *_: None)
    assert "equity $100.50, at the last close $100.00" in out
    assert "FILL sell 1 SPY at 580" in out and "CFEE BTC/USD -0.02" in out
    assert "all 3%+: 1 trades" in out and "all 5%+" not in out


def test_prices_of_coins_worth_a_fraction_of_a_cent_are_sent_as_plain_digits():
    assert ct.number(5.92345e-06) == "0.000005923"                       # was '5.92345e-06': refused
    assert ct.number(5.92345e-06, up=True) == "0.000005924"
    assert ct.number(5.92345e-06, "0.0000000001", up=True) == "0.0000059235"
    assert ct.number(67123.456789, "0.01") == "67123.45" and ct.number(1690000.7, "1") == "1690000"

    class B(ct.CryptoBroker):
        def __init__(self):
            self.sent = []

        def _call(self, method, path, **kw):
            if method == "GET":
                assert path == "/assets/PEPE%2FUSD"
                return {"price_increment": "0.000000001", "min_trade_increment": "1"}
            self.sent.append(kw["json"])
            return {"id": "x"}

    b = B()
    b.buy_limit("PEPE/USD", 1_690_000.7, 5.92345e-06, "t")
    b.stop_sell("PEPE/USD", "1690000", 5.0e-06, 4.7e-06, "t-stop")
    assert b.sent[0]["qty"] == "1690000" and b.sent[0]["limit_price"] == "0.000005924"
    assert b.sent[1]["stop_price"] == "0.000005000" and b.sent[1]["limit_price"] == "0.000004700"
