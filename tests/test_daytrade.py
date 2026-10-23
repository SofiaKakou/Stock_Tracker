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
    def __init__(self, equity=100_000.0, last_equity=100_000.0, assets=None):
        self.orders, self.held, self.closed_all, self.cancelled = [], {}, False, 0
        self.equity, self.last_equity, self.assets = equity, last_equity, assets or {}

    def account(self):
        return {"equity": str(self.equity), "last_equity": str(self.last_equity)}

    def asset(self, symbol):
        return self.assets.get(symbol, {"tradable": True, "status": "active", "shortable": symbol != "NOSHORT",
                                        "easy_to_borrow": symbol != "NOSHORT"})

    def cancel_all(self):
        self.cancelled += 1

    def open_orders(self):
        return [o for o in self.orders if o.get("tif") == "cls" or o.get("type") == "stop"]

    def order(self, symbol, qty, side, tag, tif="day", stop=None):
        if symbol == "NOSHORT" and side == "sell":
            raise PaperError("422 not shortable")
        self.orders.append({"symbol": symbol, "qty": qty, "side": side, "client_order_id": tag, "tif": tif,
                            "stop": stop, "time_in_force": tif})
        if tif == "day":
            self.held[symbol] = self.held.get(symbol, 0) + (qty if side == "buy" else -qty)
        else:
            self.held.pop(symbol, None)    # market-on-close: flat

    def orders_since(self, after, status="all"):
        return self.orders

    def positions(self):
        return [{"symbol": s, "qty": str(q), "avg_entry_price": "100"} for s, q in self.held.items() if q]

    def stop_order(self, symbol, qty, side, stop, tag):
        self.orders.append({"symbol": symbol, "qty": qty, "side": side, "client_order_id": tag, "tif": "day",
                            "type": "stop", "stop": stop, "time_in_force": "day"})

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
                if s != "STALE":   # the latest minute: it's still trading (STALE stopped at 9:30)
                    last = (pd.Timestamp(end) - pd.Timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if last > "2026-10-26T13:30:00Z":
                        rows.append((s, last, o, o, o, o, 1))
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


def test_trade_log_compares_expected_and_actual_prices(tmp_path):
    t = "td-20261026"
    orders = [
        {"symbol": "AAA", "side": "buy", "status": "filled", "filled_qty": "10", "filled_avg_price": "100.10",
         "client_order_id": f"{t}-breakout-AAA", "filled_at": "2026-10-26T14:30:50Z"},
        {"symbol": "AAA", "side": "sell", "status": "filled", "filled_qty": "10", "filled_avg_price": "102",
         "client_order_id": f"{t}-x-AAA-exit"},
        {"symbol": "BBB", "side": "sell", "status": "filled", "filled_qty": "4", "filled_avg_price": "49.95",
         "client_order_id": f"{t}-gap-BBB", "filled_at": "2026-10-26T13:31:10Z"},
        {"symbol": "BBB", "side": "buy", "status": "filled", "filled_qty": "4", "filled_avg_price": "49",
         "client_order_id": f"{t}-x-BBB-exit"}]
    entries = [{"date": "2026-10-26", "time": "10:30", "pattern": "breakout", "symbol": "AAA", "side": 1, "qty": 10,
                "expected": 100.0, "signal": 0.004}]                       # BBB: from before the log existed
    rows = {r["symbol"]: r for r in daytrade.trade_log(orders, entries, DAY)}
    assert rows["AAA"]["slippage"] == pytest.approx(0.001) and rows["AAA"]["ret"] == pytest.approx(102 / 100.1 - 1)
    assert rows["AAA"]["pnl"] == pytest.approx(19) and rows["AAA"]["signal"] == 0.004 and rows["AAA"]["time"] == "10:30"
    assert rows["BBB"]["slippage"] is None and rows["BBB"]["ret"] == pytest.approx(-(49 / 49.95 - 1))
    assert rows["BBB"]["pattern"] == "gap" and rows["BBB"]["time"] == "13:31"

    state, e = daytrade.report(orders, 100_000.0, {"pending": entries}, DAY)
    assert len(state["log"]) == 2 and "pending" not in state
    assert "Learned so far (2 trades)" in e["description"] and "0.100% worse" in e["description"]
    path = tmp_path / "log.csv"
    daytrade.write_csv(state["log"], str(path))
    text = path.read_text()
    assert text.splitlines()[0].startswith("date,time,pattern,symbol") and "AAA" in text and len(text.splitlines()) == 3
    state, _ = daytrade.report(orders, 100_000.0, state, DAY)                # the same day again: not doubled
    assert len(state["log"]) == 2


def test_the_session_records_what_each_order_expected():
    clock = Clock(dt.datetime(2026, 10, 26, 9, 17, tzinfo=daytrade.NY))
    broker = FakeBroker()
    s = daytrade.Session(broker, FakeData(clock), ["SPY", "GAPPY", "BREAKY"], DAY, log=lambda *_: None,
                         sleep=clock.sleep, now=clock.now)
    s.morning()
    e = {x["symbol"]: x for x in s.entries}
    assert e["GAPPY"]["pattern"] == "gap" and e["GAPPY"]["signal"] == pytest.approx(0.05) and e["GAPPY"]["side"] == -1
    assert e["BREAKY"]["signal"] == pytest.approx(103 / 101 - 1) and e["BREAKY"]["expected"] == 103.0


def rows(pattern, rets, slip=0.0002):
    return [{"date": "2026-10-20", "pattern": pattern, "symbol": f"S{i}", "ret": r, "slippage": slip}
            for i, r in enumerate(rets)]


TESTED = {"gap_fade": {"days": 100, "per_trade": 0.001, "sd_trade": 0.01},
          "opening_range": {"days": 100, "per_trade": 0.0005, "sd_trade": 0.01},
          "intraday_momentum": {"days": 100, "per_trade": 0.0002, "sd_trade": 0.003}}


def test_forecast_vs_actual():
    log = rows("gap", [0.002] * 5) + rows("breakout", [-0.01] * 60) + rows("momentum", [0.0003, 0.0001] * 10)
    c = daytrade.compare(log, TESTED)
    assert daytrade.verdict_vs_test(c["gap"]) == "too early to tell"
    assert c["breakout"]["z"] < -2 and "worse than its history" in daytrade.verdict_vs_test(c["breakout"])
    assert "within the normal range" in daytrade.verdict_vs_test(c["momentum"])
    assert daytrade.verdict_vs_test(daytrade.compare([], None)["gap"]) == "no live trades yet"


def test_money_shifts_slowly_within_limits_and_a_broken_pattern_pauses():
    few = rows("gap", [0.05] * 5)                                   # a lucky few days: nothing changes
    share, paused = daytrade.allocate(daytrade.compare(few, TESTED), None)
    assert share == pytest.approx({"gap": 1 / 3, "breakout": 1 / 3, "momentum": 1 / 3}) and not paused

    log = rows("gap", [0.004] * 30) + rows("breakout", [0.0] * 30) + rows("momentum", [0.0002] * 30)
    share, paused = daytrade.allocate(daytrade.compare(log, TESTED), None)
    assert share["gap"] > share["momentum"] and abs(sum(share.values()) - 1) < 1e-9
    assert all(daytrade.MIN_SHARE - 1e-9 <= v <= daytrade.MAX_SHARE + 1e-9 for v in share.values())

    huge = rows("gap", [0.5] * 200) + rows("breakout", [0.0] * 30) + rows("momentum", [0.0] * 30)
    share, _ = daytrade.allocate(daytrade.compare(huge, TESTED), None)
    assert share["gap"] == pytest.approx(daytrade.MAX_SHARE)        # never more than the cap

    broken = rows("breakout", [-0.01] * 45) + rows("gap", [0.001] * 25)
    share, paused = daytrade.allocate(daytrade.compare(broken, TESTED), None)
    assert paused == {"breakout"} and "breakout" not in share
    assert daytrade.size_factor(share, "gap") == pytest.approx(share["gap"] * 2)   # equal split = 1.0 each


def test_measured_cost_after_enough_trades_lowers_the_expected_gain():
    assert daytrade.measured_cost(rows("gap", [0.0] * 49)) is None
    assert daytrade.measured_cost(rows("gap", [0.0] * 50, slip=0.0006)) == pytest.approx(0.0006)
    assert daytrade.measured_cost(rows("gap", [0.0] * 50, slip=-0.001)) == 0.0     # never a negative cost


def test_a_paused_pattern_doesnt_trade_and_sizes_follow_the_shares():
    clock = Clock(dt.datetime(2026, 10, 26, 9, 17, tzinfo=daytrade.NY))
    broker = FakeBroker()
    s = daytrade.Session(broker, FakeData(clock), ["SPY", "GAPPY", "BREAKY"], DAY, log=lambda *_: None,
                         sleep=clock.sleep, now=clock.now, share={"breakout": 0.6, "momentum": 0.4},
                         paused={"gap"})
    s.morning()
    tags = [o["client_order_id"] for o in broker.orders]
    assert not any("-gap-" in t for t in tags) and "td-20261026-breakout-BREAKY" in tags
    qty = next(o["qty"] for o in broker.orders if o["symbol"] == "BREAKY")
    assert qty == daytrade.shares(103.0, 100_000 * daytrade.INVESTED / 20 * 0.6 * 2)    # 1.2x the normal size


def test_the_report_shows_the_learning_lines():
    state = {"log": rows("breakout", [-0.01] * 45, slip=0.0006) + rows("gap", [0.001] * 25), "days": {}}
    state, e = daytrade.report([], 100_000.0, state, DAY, TESTED)
    d = e["description"]
    assert "Forecast vs actual" in d and "worse than its history" in d and "Measured cost of getting in: 0.046%" in d
    assert "Breakout from the first half hour is paused" in d and state["paused"] == ["breakout"]
    assert "Tomorrow's money per pattern" in d


def test_bounded_shares_respect_the_limits_exactly():
    s = daytrade.bounded({"a": 100.0, "b": 1.0, "c": 1.0})
    assert s["a"] == pytest.approx(0.6) and s["b"] == pytest.approx(0.2) and sum(s.values()) == pytest.approx(1)
    s = daytrade.bounded({"a": 100.0, "b": 50.0, "c": 0.0001})
    assert s["c"] == pytest.approx(0.15) and s["a"] <= 0.6 + 1e-9 and sum(s.values()) == pytest.approx(1)
    assert daytrade.bounded({"a": 1.0, "b": 9.0}) == pytest.approx({"a": 0.4, "b": 0.6})


def test_every_entry_carries_a_protective_stop():
    clock = Clock(dt.datetime(2026, 10, 26, 9, 17, tzinfo=daytrade.NY))
    broker = FakeBroker()
    s = daytrade.Session(broker, FakeData(clock), ["SPY", "GAPPY", "BREAKY"], DAY, log=lambda *_: None,
                         sleep=clock.sleep, now=clock.now)
    s.morning()
    by = {o["symbol"]: o for o in broker.orders}
    assert by["GAPPY"]["side"] == "sell" and by["GAPPY"]["stop"] == pytest.approx(105 * 1.05)   # short: stop above
    assert by["BREAKY"]["side"] == "buy" and by["BREAKY"]["stop"] == pytest.approx(103 * 0.95)  # long: stop below


def test_the_daily_loss_limit_closes_everything_and_stops_trading():
    clock = Clock(dt.datetime(2026, 10, 26, 9, 17, tzinfo=daytrade.NY))
    broker = FakeBroker(equity=96_500.0, last_equity=100_000.0)          # down 3.5% on the day
    broker.held = {"OLD": 10}
    out = []
    s = daytrade.Session(broker, FakeData(clock), ["SPY", "GAPPY", "BREAKY"], DAY, log=out.append,
                         sleep=clock.sleep, now=clock.now)
    s.morning()
    assert s.halted and broker.closed_all and not broker.orders
    assert any("daily loss limit" in m for m in out)


def test_pre_trade_checks_skip_what_shouldnt_be_traded():
    clock = Clock(dt.datetime(2026, 10, 26, 10, 31, tzinfo=daytrade.NY))
    hard = {"tradable": True, "status": "active", "shortable": True, "easy_to_borrow": False}
    broker = FakeBroker(assets={"HARD": hard, "GONE": {"tradable": False, "status": "inactive"}})
    out = []
    s = daytrade.Session(broker, FakeData(clock), ["SPY"], DAY, log=out.append, sleep=clock.sleep, now=clock.now)
    px = {"HARD": 50.0, "GONE": 50.0, "STALE": 50.0, "FINE": 50.0, "NOSHORT": 50.0}
    s.enter("breakout", [("HARD", -1), ("GONE", 1), ("STALE", 1), ("NOSHORT", -1), ("FINE", 1)], px, 17)
    assert [o["symbol"] for o in broker.orders] == ["FINE"]
    text = "\n".join(out)
    assert "HARD skipped (hard to borrow)" in text and "GONE skipped (not tradable)" in text
    assert "STALE skipped (no recent trades (halted?))" in text and "NOSHORT skipped (can't be shorted)" in text


def test_the_safety_net_closes_only_what_should_already_be_closed():
    broker = FakeBroker()
    ny = lambda h, m: dt.datetime(2026, 10, 26, h, m, tzinfo=daytrade.NY)
    assert daytrade.safety_net(broker, ny(15, 52), log=lambda *_: None) is None          # nothing open
    broker.held = {"AAA": 10, "BBB": -5}
    broker.orders = [{"symbol": "AAA", "tif": "cls", "time_in_force": "cls"}, {"symbol": "BBB", "tif": "cls", "time_in_force": "cls"}]
    assert daytrade.safety_net(broker, ny(15, 52), log=lambda *_: None) is None          # closing orders in place
    broker.orders = [{"symbol": "AAA", "tif": "cls", "time_in_force": "cls"}]
    e = daytrade.safety_net(broker, ny(15, 52), log=lambda *_: None)                     # BBB has none: close all
    assert e and "BBB" in e["description"] and broker.closed_all
    broker.held, broker.closed_all = {"AAA": 10}, False
    e = daytrade.safety_net(broker, ny(9, 20), log=lambda *_: None)                      # left overnight
    assert "left overnight" in e["description"] and "next open" in e["description"] and broker.closed_all


def test_stopped_out_trades_are_counted_with_the_brokers_exit_order():
    t = "td-20261026"
    orders = [{"symbol": "AAA", "side": "buy", "status": "filled", "filled_qty": "10", "filled_avg_price": "100",
               "client_order_id": f"{t}-breakout-AAA"},
              {"symbol": "AAA", "side": "sell", "status": "filled", "filled_qty": "10", "filled_avg_price": "95",
               "client_order_id": "a1b2c3-stop-leg"}]
    row, = daytrade.trade_log(orders, [], DAY)
    assert row["exit"] == 95 and row["ret"] == pytest.approx(-0.05)
    assert daytrade.pnl(orders)["breakout"]["pnl"] == pytest.approx(-50)


def test_positions_without_a_stop_get_one_once():
    clock = Clock(dt.datetime(2026, 10, 26, 12, 17, tzinfo=daytrade.NY))
    broker = FakeBroker()
    broker.held = {"AAA": 10, "BBB": -5}
    s = daytrade.Session(broker, FakeData(clock), ["SPY"], DAY, log=lambda *_: None, sleep=clock.sleep, now=clock.now)
    assert s.protect() == 2
    stops = {o["symbol"]: o for o in broker.orders if o.get("type") == "stop"}
    assert stops["AAA"]["side"] == "sell" and stops["AAA"]["stop"] == pytest.approx(95)
    assert stops["BBB"]["side"] == "buy" and stops["BBB"]["stop"] == pytest.approx(105)
    assert s.protect() == 0                                     # already protected
