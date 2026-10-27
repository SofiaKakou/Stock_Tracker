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

    def get_order(self, oid):
        return {"id": oid, "status": "filled", "filled_avg_price": "85"}

    def buy_limit(self, symbol, qty, limit, tag):
        self.orders.append(("buy", symbol, qty, limit, tag))
        price = limit / (1 + ct.SLACK)
        self.held[symbol.replace("/", "")] = {"symbol": symbol.replace("/", ""), "qty": f"{qty * 0.9985:.8f}",
                                             "asset_class": "crypto", "current_price": str(price)}
        return {"id": "b1", "status": "filled", "filled_avg_price": str(price), "filled_qty": str(qty)}

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


def test_a_big_dip_is_bought_with_a_safety_stop(market):
    market({"BTC/USD": -0.01, "SOL/USD": -0.06})
    broker, state = Broker(), {}
    out = ct.run(broker, Data(None), state, now=NOW, log=lambda *_: None, sleep=lambda s: None)
    buy = [o for o in broker.orders if o[0] == "buy"]
    assert [o[1] for o in buy] == ["SOL/USD"] and buy[0][2] * 95.0 == pytest.approx(ct.AMOUNT)
    assert buy[0][3] == pytest.approx(95.0 * (1 + ct.SLACK))                       # at most 0.3% above the ask
    stop = [o for o in broker.orders if o[0] == "stop"][0]
    assert stop[3] == pytest.approx(95.0 * 0.85) and stop[2] == state["open"]["SOLUSD"]["qty"]   # the qty after the fee
    assert "Bought SOL" in out[0]["title"]


def test_held_a_day_it_is_sold_and_logged(market):
    market({})
    t0 = NOW - dt.timedelta(days=1, minutes=1)
    broker = Broker([{"symbol": "SOLUSD", "qty": "0.26", "asset_class": "crypto", "current_price": "99"}])
    state = {"open": {"SOLUSD": {"symbol": "SOL/USD", "time": t0.isoformat(), "price": 95.0, "qty": "0.26",
                                 "cost": 24.7, "stop_id": "s1"}}}
    out = ct.run(broker, Data(None), state, now=NOW, log=lambda *_: None, sleep=lambda s: None)
    assert broker.cancelled == ["s1"] and broker.closed == ["SOLUSD"] and not state["open"]
    assert state["log"][0]["ret"] == pytest.approx(99 / 95 - 1) and "+4.2%" in out[0]["title"]


def test_not_yet_a_day_it_is_kept_and_a_stopped_one_is_recorded(market):
    market({})
    broker = Broker([{"symbol": "ETHUSD", "qty": "0.01", "asset_class": "crypto"}])
    state = {"open": {"ETHUSD": {"symbol": "ETH/USD", "time": (NOW - dt.timedelta(hours=5)).isoformat(),
                                 "price": 2500.0, "qty": "0.01", "cost": 25.0},
                      "SOLUSD": {"symbol": "SOL/USD", "time": (NOW - dt.timedelta(hours=2)).isoformat(),
                                 "price": 100.0, "qty": "0.25", "cost": 25.0, "stop_id": "s2"}}}
    out = ct.run(broker, Data(None), state, now=NOW, log=lambda *_: None, sleep=lambda s: None)
    assert list(state["open"]) == ["ETHUSD"] and not broker.closed
    assert state["log"][0]["how"] == "stop" and state["log"][0]["ret"] == pytest.approx(-0.15)
    assert "safety stop" in out[0]["title"]


def test_leftover_stocks_are_sold_once_announced(market):
    market({})
    broker = Broker([{"symbol": "AAPL", "qty": "0.02", "asset_class": "us_equity"},
                     {"symbol": "MSFT", "qty": "0.01", "asset_class": "us_equity"}])
    state = {}
    out = ct.run(broker, Data(None), state, now=NOW, log=lambda *_: None, sleep=lambda s: None)
    assert sorted(broker.closed) == ["AAPL", "MSFT"] and state["stocks_sold"] and "now a crypto account" in out[0]["title"]


def test_no_more_than_four_at_once_and_a_stale_signal_is_ignored(market):
    market({f"C{i}/USD": -0.06 for i in range(6)})
    broker, state = Broker(cash=1000.0), {}
    ct.run(broker, Data(None), state, now=NOW, log=lambda *_: None, sleep=lambda s: None)
    assert len(state["open"]) == ct.SLOTS
    late = NOW + dt.timedelta(minutes=40)                       # GitHub started the check 40 minutes late
    assert ct.dips(bars({"X/USD": -0.06}), late) == {}


def test_the_evening_summary_reads_well():
    state = {"open": {}, "log": [{"symbol": "SOL/USD", "in": "2026-10-26T15:02:00+00:00", "out": NOW.isoformat(),
                                  "entry": 95.0, "exit": 99.0, "how": "day", "cost": 24.7, "ret": 99 / 95 - 1}]}
    e = ct.summary(Broker(), state, now=NOW)
    assert "$100.50" in e["description"] and "SOL +4.2%" in e["description"] and "All trades: 1" in e["description"]
