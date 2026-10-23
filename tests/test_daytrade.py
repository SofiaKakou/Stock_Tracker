import datetime as dt

import pandas as pd
import pytest

from trend_bot import daytrade
from trend_bot.paper import PaperError

DAY = dt.date(2026, 10, 26)   # a Monday, New York winter time not yet (EDT)


def test_signals():
    assert daytrade.gap_signals({"A": 103.0, "B": 99.0, "C": 95.0}, {"A": 100.0, "B": 100.0, "C": 100.0}) == \
        [("C", 1), ("A", -1)]                                     # biggest gap first; fade it
    assert daytrade.breakout_signals({"A": 10, "B": 10, "C": 10}, {"A": 9, "B": 9, "C": 9},
                                     {"A": 10.5, "B": 8.5, "C": 9.5}, done={"B"}) == [("A", 1)]
    assert daytrade.momentum_signals({"SPY": 101.0, "QQQ": 99.0}, {"SPY": 100.0, "QQQ": 100.0}) == [("SPY", 1), ("QQQ", -1)]
    assert daytrade.shares(250.0, 4900) == 19 and daytrade.shares(0, 4900) == 0


def test_pnl_per_pattern_from_the_days_orders():
    t = "td-20261026"
    orders = [
        {"symbol": "AAA", "side": "buy", "status": "filled", "filled_qty": "10", "filled_avg_price": "100", "client_order_id": f"{t}-gap-AAA"},
        {"symbol": "AAA", "side": "sell", "status": "filled", "filled_qty": "10", "filled_avg_price": "103", "client_order_id": f"{t}-x-AAA-exit"},
        {"symbol": "BBB", "side": "sell", "status": "filled", "filled_qty": "5", "filled_avg_price": "50", "client_order_id": f"{t}-breakout-BBB"},
        {"symbol": "BBB", "side": "buy", "status": "filled", "filled_qty": "5", "filled_avg_price": "52", "client_order_id": f"{t}-x-BBB-exit"},
        {"symbol": "CCC", "side": "buy", "status": "canceled", "filled_qty": "0", "filled_avg_price": None, "client_order_id": f"{t}-gap-CCC"},
    ]
    r = daytrade.pnl(orders)
    assert r["gap"]["pnl"] == pytest.approx(30) and r["breakout"]["pnl"] == pytest.approx(-10)
    assert r["gap"]["trades"] == 1 and "CCC" not in str(r)


def test_report_keeps_running_totals_and_shows_the_test_result():
    t = "td-20261026"
    orders = [{"symbol": "SPY", "side": "buy", "status": "filled", "filled_qty": "6", "filled_avg_price": "760",
               "client_order_id": f"{t}-momentum-SPY"},
              {"symbol": "SPY", "side": "sell", "status": "filled", "filled_qty": "6", "filled_avg_price": "765",
               "client_order_id": f"{t}-x-SPY-exit"}]
    tested = {"intraday_momentum": {"days": 10, "per_trade": 0.0005, "early": 0.1, "late": 0.1, "t": 3, "luck": 0.01,
                                    "yearly": 0.1}}
    state, e = daytrade.report(orders, 100_030.0, {}, DAY, tested)
    assert state["start_equity"] == pytest.approx(100_000) and "$+30" in e["description"]
    assert "10-year test: +0.05% a trade after costs, passed" in e["description"]
    state, e = daytrade.report([], 100_030.0, state, DAY + dt.timedelta(days=1))
    assert "No trades today" in e["description"] and "2 trading days" in e["description"]
    assert "since the start" not in e["description"]


class Clock:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += dt.timedelta(seconds=s)


class FakeBroker:
    def __init__(self):
        self.orders, self.held, self.closed_all = [], {}, False

    def account(self):
        return {"equity": "100000"}

    def order(self, symbol, qty, side, tag, tif="day"):
        if symbol == "NOSHORT" and side == "sell":
            raise PaperError("422 not shortable")
        self.orders.append({"symbol": symbol, "qty": qty, "side": side, "client_order_id": tag, "tif": tif})
        if tif == "day":
            self.held[symbol] = self.held.get(symbol, 0) + (qty if side == "buy" else -qty)
        else:
            self.held.pop(symbol, None)    # market-on-close: flat

    def orders_since(self, after, status="all"):
        return self.orders

    def positions(self):
        return [{"symbol": s, "qty": str(q)} for s, q in self.held.items() if q]

    def close_all(self):
        self.closed_all = True
        self.held = {}


class FakeData:
    """Prices that make one stock gap up, one break out, and the market rise in its first half hour."""

    def __init__(self, clock):
        self.clock = clock

    def bars(self, symbols, start, end, timeframe="30Min"):
        rows = []
        for s in symbols:
            if timeframe == "1Day":
                rows.append((s, "2026-10-23T04:00:00Z", 100.0, 100.0, 100.0, 100.0, 1))
            elif timeframe == "1Min":
                o = {"GAPPY": 105.0}.get(s, 100.0)
                rows.append((s, "2026-10-26T13:30:00Z", o, o, o, o, 1))
            else:
                t0 = pd.Timestamp("2026-10-26T13:30:00Z")
                for k in range(13):
                    t = t0 + pd.Timedelta(minutes=30 * k)
                    if t >= pd.Timestamp(end):
                        break
                    c = {"BREAKY": 100.0 if k == 0 else 103.0, "SPY": 101.0}.get(s, 100.0)
                    rows.append((s, t.strftime("%Y-%m-%dT%H:%M:%SZ"), 100.0, 101.0 if k == 0 else c, 99.0, c, 1))
        return pd.DataFrame(rows, columns=["symbol", "t", "open", "high", "low", "close", "volume"])


def test_a_whole_simulated_day_trades_each_pattern_once_and_ends_flat():
    clock = Clock(dt.datetime(2026, 10, 26, 9, 17, tzinfo=daytrade.NY))
    broker = FakeBroker()
    symbols = ["SPY", "QQQ", "IWM", "GAPPY", "BREAKY", "QUIET", "NOSHORT"]
    s = daytrade.Session(broker, FakeData(clock), symbols, DAY, log=lambda *_: None, sleep=clock.sleep, now=clock.now)
    s.morning()
    tags = [o["client_order_id"] for o in broker.orders]
    assert "td-20261026-gap-GAPPY" in tags and broker.orders[0]["side"] == "sell"      # gapped up 5%: short
    assert "td-20261026-breakout-BREAKY" in tags                                     # closed above 101
    assert clock.now().time() < dt.time(12, 5)
    clock.t = dt.datetime(2026, 10, 26, 12, 17, tzinfo=daytrade.NY)                     # the afternoon job
    s.afternoon()
    tags = [o["client_order_id"] for o in broker.orders]
    assert tags.count("td-20261026-breakout-BREAKY") == 1                              # never twice
    assert "td-20261026-momentum-SPY" in tags and "td-20261026-momentum-QQQ" not in tags   # QQQ was flat
    exits = [o for o in broker.orders if o["tif"] == "cls"]
    assert {o["symbol"] for o in exits} == {"GAPPY", "BREAKY", "SPY"}
    assert not broker.positions() and clock.now().time() >= daytrade.FLAT_AT


def test_a_late_morning_start_skips_the_gap_fade_and_missed_checks():
    clock = Clock(dt.datetime(2026, 10, 26, 11, 10, tzinfo=daytrade.NY))
    broker = FakeBroker()
    s = daytrade.Session(broker, FakeData(clock), ["SPY", "GAPPY", "BREAKY"], DAY, log=lambda *_: None,
                         sleep=clock.sleep, now=clock.now)
    s.morning()
    tags = [o["client_order_id"] for o in broker.orders]
    assert not any("-gap-" in t for t in tags) and "td-20261026-breakout-BREAKY" in tags   # 11:00 check: still within 20 minutes


def test_command_does_nothing_without_keys(tmp_path, monkeypatch, capsys):
    from trend_bot.cli import main

    monkeypatch.delenv(daytrade.KEY_ENV, raising=False)
    assert main(["daytrade", "--state", str(tmp_path / "s.json")]) == 0
    assert "isn't set up" in capsys.readouterr().out
