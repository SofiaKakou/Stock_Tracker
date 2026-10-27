import datetime as dt

import pandas as pd
import pytest

from trend_bot import daytrade
from trend_bot.paper import PaperError

DAY = dt.date(2026, 10, 26)   # a Monday, New York winter time not yet (EDT)


@pytest.fixture(autouse=True)
def no_earnings_calendar(monkeypatch):
    """No Yahoo in tests: no stock reports earnings unless a test says so."""
    monkeypatch.setattr(daytrade.intraday, "_yahoo_earnings", lambda symbol, limit=60: [])
    monkeypatch.setattr(daytrade, "_yahoo_sector", lambda symbol: None)


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
        self.orders, self.held, self.closed_all, self.cancelled, self.closed = [], {}, False, 0, []
        self.equity, self.last_equity, self.assets = equity, last_equity, assets or {}

    def account(self):
        return {"equity": str(self.equity), "last_equity": str(self.last_equity)}

    def asset(self, symbol):
        return self.assets.get(symbol, {"tradable": True, "status": "active", "shortable": symbol != "NOSHORT",
                                        "easy_to_borrow": symbol != "NOSHORT"})

    def cancel_all(self):
        self.cancelled += 1
        for o in self.orders:
            o["cancelled"] = True

    def open_orders(self, nested=False):
        return [o for o in self.orders if (o.get("tif") == "cls" or o.get("type") == "stop") and not o.get("cancelled")]

    def order(self, symbol, qty, side, tag, tif="day", stop=None, limit=None):
        if symbol == "NOSHORT" and side == "sell":
            raise PaperError("422 not shortable")
        self.orders.append({"symbol": symbol, "qty": qty, "side": side, "client_order_id": tag, "tif": tif,
                            "stop": stop, "limit": limit, "time_in_force": tif})
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

    def close(self, symbol):
        self.closed.append(symbol)
        self.held.pop(symbol, None)


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
    assert {"GAPPY", "BREAKY", "SPY"} <= set(broker.closed)                            # closed at 15:50
    assert not broker.positions() and dt.time(15, 50) <= clock.now().time() < daytrade.FLAT_AT


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


def rows(pattern, rets, slip=0.0002, per_day=5):
    """Live trades, `per_day` of them on each trading day (the loop also needs enough days)."""
    days = pd.bdate_range("2026-08-03", periods=len(rets) // per_day + 1)
    return [{"date": str(days[i // per_day].date()), "pattern": pattern, "symbol": f"S{i}", "ret": r,
             "slippage": slip} for i, r in enumerate(rets)]


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
    assert share == pytest.approx({p: 1 / len(daytrade.PATTERN_TEST) for p in daytrade.PATTERN_TEST}) and not paused

    log = rows("gap", [0.004] * 30) + rows("breakout", [0.0] * 30) + rows("momentum", [0.0002] * 30)
    share, paused = daytrade.allocate(daytrade.compare(log, TESTED), None)
    assert share["gap"] > share["momentum"] and abs(sum(share.values()) - 1) < 1e-9
    assert all(daytrade.MIN_SHARE - 1e-9 <= v <= daytrade.MAX_SHARE + 1e-9 for v in share.values())

    huge = rows("gap", [0.5] * 200) + rows("breakout", [0.0] * 30) + rows("momentum", [0.0] * 30)
    share, _ = daytrade.allocate(daytrade.compare(huge, TESTED), None)
    cap = min(daytrade.MAX_SHARE, 1 - daytrade.MIN_SHARE * (len(share) - 1))   # the others need their minimum
    assert share["gap"] == pytest.approx(cap) and sum(share.values()) == pytest.approx(1)   # never more than the cap

    broken = rows("breakout", [-0.01] * 50) + rows("gap", [0.001] * 25)
    share, paused = daytrade.allocate(daytrade.compare(broken, TESTED), None)
    assert paused == {"breakout"} and "breakout" not in share
    assert daytrade.size_factor(share, "gap") == pytest.approx(share["gap"] * len(share))   # equal split = 1.0 each


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
    state = {"log": rows("breakout", [-0.01] * 50, slip=0.0006) + rows("gap", [0.001] * 25), "days": {}}
    state, e = daytrade.report([], 100_000.0, state, DAY, TESTED)
    d = e["description"]
    assert "Forecast vs actual" in d and "worse than its history" in d and "Measured cost of getting in: 0.047%" in d
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


# --- What went wrong on the first live day (2026-10-23) ------------------------------------------

def test_a_trade_whose_exit_hasnt_filled_yet_counts_nothing_not_its_whole_size():
    # The report ran at 15:58, before the closing orders filled at 16:00: each position's full size
    # (about $4,700) showed up as a gain or loss and the account looked 40% up.
    t = "td-20261023"
    orders = [{"symbol": "IWM", "side": "sell", "status": "filled", "filled_qty": "17", "filled_avg_price": "276",
               "client_order_id": f"{t}-momentum-IWM"},
              {"symbol": "IWM", "side": "buy", "status": "new", "filled_qty": "0", "filled_avg_price": None,
               "client_order_id": f"{t}-x-IWM-exit"}]
    r = daytrade.pnl(orders)
    assert "momentum" not in r and r["open"]["symbols"] == ["IWM"]
    state, e = daytrade.report(orders, 100_230.0, {}, dt.date(2026, 10, 23), last_equity=100_000.0)
    assert state["start_equity"] == 100_000.0 and "Not closed yet" in e["description"] and "IWM" in e["description"]
    assert "+0.23%" in e["description"]                     # the account's own change, not +40%
    assert "some fill is missing" in e["description"]       # $0 of trades vs +$230 in the account


def test_the_trades_must_add_up_to_the_accounts_change():
    t = "td-20261026"
    orders = [{"symbol": "SPY", "side": "buy", "status": "filled", "filled_qty": "6", "filled_avg_price": "760",
               "client_order_id": f"{t}-momentum-SPY"},
              {"symbol": "SPY", "side": "sell", "status": "filled", "filled_qty": "6", "filled_avg_price": "765",
               "client_order_id": f"{t}-x-SPY-exit"}]
    _, e = daytrade.report(orders, 100_030.0, {}, DAY, last_equity=100_000.0)
    assert "missing" not in e["description"] and "$+30" in e["description"]


def test_redoing_the_first_day_fixes_the_starting_value():
    state = {"start_equity": 71_498.0, "days": {"2026-10-23": {"breakout": {"pnl": 23_707.0, "trades": 17}}}}
    state, e = daytrade.report([], 100_230.0, state, dt.date(2026, 10, 23), last_equity=100_000.0)
    assert state["start_equity"] == 100_000.0 and "+40" not in e["description"]


class StickyBroker(FakeBroker):
    """Like the paper account on 2026-10-26: some closes don't go through the first time."""

    def __init__(self, sticky):
        super().__init__()
        self.sticky = dict(sticky)     # symbol: how many close attempts fail

    def close(self, symbol):
        if self.sticky.get(symbol, 0) > 0:
            self.sticky[symbol] -= 1
            raise PaperError("403 insufficient qty available (held_for_orders)")
        super().close(symbol)


def test_every_position_is_closed_before_the_close_and_checked():
    # Market-on-close orders didn't fill on the paper account (37 positions stayed open overnight):
    # now plain market closes at 15:50, checked, retried, and a last sweep at 15:58.
    clock = Clock(dt.datetime(2026, 10, 26, 15, 40, tzinfo=daytrade.NY))
    broker = StickyBroker({"AAPL": 1, "MSFT": 3})        # AAPL closes on round 2; MSFT only at 15:58
    broker.held = {"AAPL": 14, "MSFT": -10, "NVDA": 5}
    s = daytrade.Session(broker, FakeData(clock), ["AAPL", "MSFT", "NVDA"], DAY, log=lambda *_: None,
                         sleep=clock.sleep, now=clock.now)
    s.flatten()
    assert not broker.positions() and broker.closed.count("MSFT") == 1
    assert not any(o.get("tif") == "cls" for o in broker.orders)                         # no market-on-close
    assert clock.now().time() >= daytrade.FLAT_AT                                        # MSFT needed the sweep


def test_a_stop_attached_to_its_entry_order_counts_as_protection():
    class Nested(FakeBroker):
        def open_orders(self, nested=False):
            leg = {"symbol": "AAPL", "type": "stop"}
            return [{"symbol": "AAPL", "type": "market", "legs": [leg]}] if nested else []

    broker = Nested()
    broker.held = {"AAPL": 14}
    s = daytrade.Session(broker, None, ["AAPL"], DAY, log=lambda *_: None)
    assert s.protect() == 0 and not [o for o in broker.orders if o.get("type") == "stop"]


def test_a_past_days_report_can_be_redone_from_that_days_orders(tmp_path, monkeypatch):
    import json

    from trend_bot.cli import main

    t = "td-20261023"
    friday = [{"symbol": "SPY", "side": "buy", "status": "filled", "filled_qty": "6", "filled_avg_price": "760",
               "client_order_id": f"{t}-momentum-SPY", "submitted_at": "2026-10-23T19:30:00Z"},
              {"symbol": "SPY", "side": "sell", "status": "filled", "filled_qty": "6", "filled_avg_price": "765",
               "client_order_id": f"{t}-x-SPY-exit", "submitted_at": "2026-10-23T19:45:00Z"}]
    later = [{"symbol": "QQQ", "side": "buy", "status": "filled", "filled_qty": "1", "filled_avg_price": "1",
              "client_order_id": "td-20261026-momentum-QQQ", "submitted_at": "2026-10-26T19:30:00Z"}]
    broker = FakeBroker(equity=100_030.0)
    broker.orders = friday + later
    monkeypatch.setattr(daytrade.DayBroker, "from_env", classmethod(lambda cls: broker))
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"start_equity": 71_498.0, "days": {"2026-10-23": {"momentum": {"pnl": 4_563.0, "trades": 1}}}}))
    assert main(["daytrade", "--date", "2026-10-23", "--state", str(state),          # a date alone means: report
                 "--intraday-db", str(tmp_path / "none.db"), "--log-csv", str(tmp_path / "l.csv")]) == 0
    s = json.loads(state.read_text())
    assert s["days"] == {"2026-10-23": {"momentum": {"pnl": 30.0, "trades": 1}}}
    assert s["start_equity"] == pytest.approx(100_000.0)


def test_a_refused_close_at_1558_is_retried_and_reported():
    class Stubborn(FakeBroker):
        def __init__(self):
            super().__init__()
            self.tries = 0

        def order(self, symbol, qty, side, tag, tif="day", stop=None, limit=None):
            if tif == "cls":
                raise PaperError("403 too late for a closing order")
            super().order(symbol, qty, side, tag, tif, stop, limit)

        def close(self, symbol):
            self.tries += 1
            if self.tries == 1:
                raise PaperError("403 held_for_orders")
            super().close(symbol)

    clock = Clock(dt.datetime(2026, 10, 26, 15, 57, tzinfo=daytrade.NY))
    broker = Stubborn()
    broker.held = {"PFE": 50}
    s = daytrade.Session(broker, FakeData(clock), ["PFE"], DAY, log=lambda *_: None, sleep=clock.sleep, now=clock.now)
    s.flatten()
    assert broker.tries == 2 and broker.closed == ["PFE"] and not broker.held


def test_positions_held_over_the_weekend_are_settled_into_their_own_day():
    state = {"start_equity": 100_000.0,
             "log": [{"date": "2026-10-23", "symbol": "PFE", "pattern": "breakout", "side": 1, "qty": 100,
                      "entry": 25.0, "exit": None, "ret": None, "pnl": None}],
             "days": {"2026-10-23": {"breakout": {"pnl": 311.0, "trades": 11}}}}
    earlier = [{"id": "s1", "symbol": "PFE", "side": "sell", "status": "filled", "filled_qty": "100",
                "filled_avg_price": "25.5", "client_order_id": "safety-close",
                "submitted_at": "2026-10-23T20:15:00Z", "filled_at": "2026-10-26T13:30:01Z"}]   # queued Friday night
    state, e = daytrade.report([], 100_361.0, state, DAY, last_equity=100_311.0, earlier=earlier)
    row = state["log"][0]
    assert row["exit"] == 25.5 and row["pnl"] == pytest.approx(50) and row["ret"] == pytest.approx(0.02)
    assert state["days"]["2026-10-23"]["breakout"] == {"pnl": pytest.approx(361.0), "trades": 12}
    d = e["description"]
    assert "from earlier days" in d and "PFE +50" in d and "Not closed yet" not in d and "missing" not in d


def test_a_position_already_being_closed_gets_no_new_stop():
    broker = FakeBroker()
    broker.held = {"PFE": 100}
    # A closing order queued for the open (like the safety net's on Friday night), not a stop:
    broker.open_orders = lambda nested=False: [{"symbol": "PFE", "side": "sell", "type": "market"}]
    s = daytrade.Session(broker, None, ["PFE"], DAY, log=lambda *_: None)
    assert s.protect() == 0


def test_each_part_runs_once_a_day_even_with_backup_schedules(tmp_path, monkeypatch):
    import json

    from trend_bot.cli import main
    from trend_bot.commands import outputs

    broker = FakeBroker()
    broker.clock = lambda: {"is_open": True}
    monkeypatch.setattr(daytrade.DayBroker, "from_env", classmethod(lambda cls: broker))
    ran = []
    monkeypatch.setattr(daytrade.Session, "morning", lambda self: ran.append("morning"))
    state = tmp_path / "s.json"
    args = ["daytrade", "--state", str(state), "--intraday-db", str(tmp_path / "none.db"),
            "--log-csv", str(tmp_path / "l.csv")]

    class Late(dt.datetime):          # GitHub started the morning part an hour late: 10:17
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 10, 26, 10, 17, tzinfo=tz)

    monkeypatch.setattr(outputs, "_dt", dt)
    monkeypatch.setattr(dt, "datetime", Late)
    assert main(args) == 0 and main(args) == 0          # the backup schedule fires too
    assert ran == ["morning"] and json.loads(state.read_text())["ran"] == {"2026-10-26": ["morning"]}


def test_one_busy_day_cant_shift_the_money_or_pause_a_pattern():
    one_day = rows("breakout", [-0.01] * 60, per_day=60) + rows("gap", [0.004] * 60, per_day=60)
    cmp = daytrade.compare(one_day, TESTED)
    assert cmp["breakout"]["n"] == 60 and cmp["breakout"]["days"] == 1
    share, paused = daytrade.allocate(cmp, None)
    assert not paused and share == pytest.approx({p: 1 / len(daytrade.PATTERN_TEST) for p in daytrade.PATTERN_TEST})
    spread = rows("breakout", [-0.01] * 60, per_day=6) + rows("gap", [0.004] * 60, per_day=6)
    share, paused = daytrade.allocate(daytrade.compare(spread, TESTED), None)    # the same over 10 days
    assert paused == {"breakout"}


def test_up_to_37_positions_of_the_usual_size_within_twice_the_account():
    broker = FakeBroker()
    s = daytrade.Session(broker, None, [], DAY, log=lambda *_: None)
    assert s.budget() == pytest.approx(100_000 * daytrade.INVESTED / 20)        # about $4,900 each, as before
    assert daytrade.STOCK_SLOTS + daytrade.ETF_SLOTS == 37 and daytrade.BUYING_POWER == 2.0


# --- Short the big gap-ups (tested only) ----------------------------------------------------------

def test_only_big_gap_ups_in_big_enough_stocks_are_shorted():
    opens = {"HOT": 12.5, "MILD": 11.0, "PENNY": 3.0, "THIN": 13.0, "DEAL": 17.0}
    prev = {"HOT": 10.0, "MILD": 10.0, "PENNY": 2.0, "THIN": 10.0, "DEAL": 10.0}
    vol = {"HOT": 2e7, "MILD": 2e7, "PENNY": 2e7, "THIN": 1e5, "DEAL": 2e7}
    # +25% shorted; +10% too small; under $5; barely traded; +70% (a takeover or a data error) left alone
    assert daytrade.gapup_signals(opens, prev, vol) == [("HOT", -1)]


class GapData:
    """A stock that opens 25% up today, with 25 normal days before."""

    def bars(self, symbols, start, end, timeframe="30Min"):
        rows = []
        for s in symbols:
            if timeframe == "1Day":
                for d in pd.bdate_range("2026-09-21", "2026-10-23"):
                    rows.append((s, f"{d.date()}T04:00:00Z", 10.0, 10.0, 10.0, 10.0, 2e6))
            elif timeframe == "1Min":
                rows.append((s, "2026-10-26T13:30:00Z", 12.5, 12.6, 12.4, 12.5, 1e5))
        return pd.DataFrame(rows, columns=["symbol", "t", "open", "high", "low", "close", "volume"])


def test_a_market_wide_gap_up_is_shorted_with_a_wider_stop(monkeypatch):
    from trend_bot import intraday

    monkeypatch.setattr(intraday, "movers", lambda data, top=50: ["SPY", "HOT"])
    clock = Clock(dt.datetime(2026, 10, 26, 9, 31, 30, tzinfo=daytrade.NY))
    broker = FakeBroker()
    s = daytrade.Session(broker, GapData(), ["AAPL"], DAY, log=lambda *_: None, sleep=clock.sleep, now=clock.now)
    assert s.gapups() == 1
    o = broker.orders[-1]
    assert o["client_order_id"] == "td-20261026-gapup-HOT" and o["side"] == "sell"
    assert o["stop"] == pytest.approx(12.5 * (1 + daytrade.GAPUP_STOP))      # 15% above, not the usual 5%
    assert s.gapups() == 0                                                    # not twice


def test_no_screener_means_the_pattern_just_sits_out(monkeypatch):
    from trend_bot import intraday

    def broken(data, top=50):
        raise RuntimeError("403 not in your plan")

    monkeypatch.setattr(intraday, "movers", broken)
    s = daytrade.Session(FakeBroker(), GapData(), [], DAY, log=lambda *_: None)
    assert s.gapups() == 0


# --- Lower costs and no one big bet ------------------------------------------------------------------

def test_entries_are_limit_orders_near_the_quote_with_the_stop_from_its_middle(monkeypatch):
    clock = Clock(dt.datetime(2026, 10, 26, 9, 17, tzinfo=daytrade.NY))
    broker = FakeBroker()
    monkeypatch.setattr(daytrade.intraday, "latest_quotes", lambda data, syms: {
        "GAPPY": {"bid": 104.9, "ask": 105.1}, "BREAKY": {"bid": 102.9, "ask": 103.1}})
    s = daytrade.Session(broker, FakeData(clock), ["SPY", "GAPPY", "BREAKY"], DAY, log=lambda *_: None,
                         sleep=clock.sleep, now=clock.now)
    s.morning()
    by = {o["symbol"]: o for o in broker.orders}
    assert by["GAPPY"]["limit"] == pytest.approx(104.9 * (1 - daytrade.LIMIT_SLACK))    # a short: at least this
    assert by["BREAKY"]["limit"] == pytest.approx(103.1 * (1 + daytrade.LIMIT_SLACK))   # a buy: at most this
    assert by["BREAKY"]["stop"] == pytest.approx(103.0 * 0.95)
    assert {e["symbol"]: e["expected"] for e in s.entries}["GAPPY"] == pytest.approx(105.0)


def test_without_a_quote_the_entry_is_a_market_order():
    clock = Clock(dt.datetime(2026, 10, 26, 9, 17, tzinfo=daytrade.NY))
    broker = FakeBroker()
    s = daytrade.Session(broker, FakeData(clock), ["SPY", "GAPPY", "BREAKY"], DAY, log=lambda *_: None,
                         sleep=clock.sleep, now=clock.now)
    s.morning()
    assert all(o["limit"] is None for o in broker.orders if o["client_order_id"].startswith("td-"))


def test_entries_that_dont_fill_in_five_minutes_are_cancelled():
    clock = Clock(dt.datetime(2026, 10, 26, 10, 0, tzinfo=daytrade.NY))

    class Waiting(FakeBroker):
        def __init__(self):
            super().__init__()
            self.gone = []

        def open_orders(self, nested=False):
            return [{"id": "old", "client_order_id": "td-20261026-gap-AAA", "submitted_at": "2026-10-26T13:50:00Z"},
                    {"id": "new", "client_order_id": "td-20261026-gap-BBB", "submitted_at": "2026-10-26T13:58:00Z"},
                    {"id": "exit", "client_order_id": "td-20261026-x-CCC", "submitted_at": "2026-10-26T13:00:00Z"}]

        def cancel(self, oid):
            self.gone.append(oid)

    broker = Waiting()
    s = daytrade.Session(broker, None, [], DAY, log=lambda *_: None, now=clock.now)
    assert s.cancel_stale_entries() == 1 and broker.gone == ["old"]     # 10 minutes old; the exit stays


def test_the_account_never_gets_too_one_sided(monkeypatch):
    clock = Clock(dt.datetime(2026, 10, 26, 9, 17, tzinfo=daytrade.NY))

    class Long(FakeBroker):     # already $60,000 long on a $100,000 account
        def positions(self):
            return [{"symbol": "OLD", "qty": "600", "avg_entry_price": "100", "market_value": "60000"}]

    broker = Long()
    out = []
    s = daytrade.Session(broker, FakeData(clock), ["SPY", "GAPPY", "BREAKY"], DAY, log=out.append,
                         sleep=clock.sleep, now=clock.now)
    s.morning()
    sides = {o["symbol"]: o["side"] for o in broker.orders}
    assert sides.get("GAPPY") == "sell"            # a short evens it out: allowed
    assert "BREAKY" not in sides                    # another long: skipped
    assert any("too one-sided" in m for m in out)


def test_open_positions_are_not_reported_as_no_trades():
    orders = [{"symbol": "AAA", "side": "buy", "filled_qty": "10", "filled_avg_price": "100", "status": "filled",
               "client_order_id": "td-20261026-gap-AAA", "filled_at": "2026-10-26T13:35:00Z"}]
    _, embed = daytrade.report(orders, 100_000.0, {}, DAY)
    assert "No trades today" not in embed["description"] and "results pending" in embed["description"]


def test_the_safety_net_warns_once_when_the_closes_are_already_queued_for_the_open():
    class Queued(FakeBroker):
        def open_orders(self, nested=False):
            return [{"symbol": "AAA", "type": "market", "time_in_force": "day"}]

    broker = Queued()
    broker.held = {"AAA": 10}
    night = dt.datetime(2026, 10, 26, 20, 30, tzinfo=daytrade.NY)
    assert daytrade.safety_net(broker, night, log=lambda *_: None) is None and not broker.closed_all
    broker.held["BBB"] = 5                                                  # one not queued yet: acts again
    assert daytrade.safety_net(broker, night, log=lambda *_: None) and broker.closed_all


# --- Short yesterday's big losers (tested only) ------------------------------------------------------

def daily(closes, vols):
    days = pd.bdate_range(end="2026-10-23", periods=len(closes))
    return [{"t": f"{d.date()}T04:00:00Z", "c": c, "v": v} for d, c, v in zip(days, closes, vols)]


def test_only_big_falls_on_big_volume_in_tradable_stocks_count_as_losers():
    normal = [20.0] * 24
    days = {"CRASH": daily(normal + [16.0], [1e6] * 24 + [5e6]),        # -20% on 5x volume
            "QUIET": daily(normal + [16.0], [1e6] * 25),                # -20% on normal volume: a price error?
            "SMALL": daily(normal + [17.5], [1e6] * 24 + [5e6]),        # -12.5%: not big enough
            "PENNY": daily([4.0] * 24 + [3.0], [1e7] * 24 + [5e7]),     # under $5
            "GONE": daily(normal + [6.0], [1e6] * 24 + [5e6]),          # -70%: data error or delisting
            "WORSE": daily(normal + [14.0], [1e6] * 24 + [4e6])}        # -30%: first
    got = daytrade.loser_signals(days, DAY)
    assert [s for s, _, _ in got] == ["WORSE", "CRASH"] and all(side == -1 for _, side, _ in got)


class LoserData:
    def bars(self, symbols, start, end, timeframe="30Min"):
        rows = []
        for s in symbols:
            if timeframe == "1Day":
                for b in daily([20.0] * 24 + [16.0 if s in ("CRASH", "EWZ") else 20.0], [1e6] * 24 + [5e6]):
                    rows.append((s, b["t"], b["c"], b["c"], b["c"], b["c"], b["v"]))
            elif timeframe == "1Min":
                rows.append((s, "2026-10-26T13:30:00Z", 15.8, 15.9, 15.7, 15.8, 1e5))
        return pd.DataFrame(rows, columns=["symbol", "t", "open", "high", "low", "close", "volume"])


def test_yesterdays_big_loser_is_shorted_at_the_open_with_a_wide_stop():
    class Listed(FakeBroker):
        def list_assets(self):
            ok = {"tradable": True, "shortable": True, "easy_to_borrow": True, "exchange": "NASDAQ"}
            return [{"symbol": "CRASH", **ok}, {"symbol": "FINE", **ok}, {"symbol": "SPY", **ok},
                    {"symbol": "EWZ", "name": "iShares MSCI Brazil ETF", **ok},          # a fund: not one company
                    {"symbol": "HARD", **ok, "easy_to_borrow": False}, {"symbol": "OTC", **ok, "exchange": "OTC"}]

    clock = Clock(dt.datetime(2026, 10, 26, 9, 20, tzinfo=daytrade.NY))
    broker = Listed()
    s = daytrade.Session(broker, LoserData(), ["AAPL"], DAY, log=lambda *_: None, sleep=clock.sleep, now=clock.now)
    found = s.find_losers()
    assert [x[0] for x in found] == ["CRASH"]
    clock.t = dt.datetime(2026, 10, 26, 9, 31, 30, tzinfo=daytrade.NY)
    assert s.losers(found) == 1
    o = broker.orders[-1]
    assert o["client_order_id"] == "td-20261026-loser-CRASH" and o["side"] == "sell"
    assert o["stop"] == pytest.approx(15.8 * (1 + daytrade.LOSER_STOP))
    assert s.losers(found) == 0                                                    # not twice


def test_no_market_scan_means_the_pattern_just_sits_out():
    s = daytrade.Session(FakeBroker(), LoserData(), [], DAY, log=lambda *_: None)    # no list_assets() to call
    assert s.find_losers() == [] and s.losers([]) == 0


def test_a_day_without_gap_ups_doesnt_ask_for_prices_of_nothing(monkeypatch):
    from trend_bot import intraday

    class Strict(GapData):
        def bars(self, symbols, start, end, timeframe="30Min"):
            if not symbols:                                  # what Alpaca does: 400 Bad Request
                raise RuntimeError("400 Client Error: Bad Request (symbols=)")
            return super().bars(symbols, start, end, timeframe)

    monkeypatch.setattr(intraday, "movers", lambda data, top=50: ["CALM"])
    clock = Clock(dt.datetime(2026, 10, 26, 9, 31, 30, tzinfo=daytrade.NY))
    s = daytrade.Session(FakeBroker(), Strict(), ["AAPL"], DAY, log=lambda *_: None, sleep=clock.sleep, now=clock.now)
    monkeypatch.setattr(daytrade, "gapup_signals", lambda *a: [])
    assert s.gapups() == 0 and s.enter("gap", [], {}, 3) == 0


def test_one_patterns_error_doesnt_stop_the_morning(monkeypatch):
    clock = Clock(dt.datetime(2026, 10, 26, 9, 17, tzinfo=daytrade.NY))
    broker, out = FakeBroker(), []
    s = daytrade.Session(broker, FakeData(clock), ["SPY", "GAPPY", "BREAKY"], DAY, log=out.append,
                         sleep=clock.sleep, now=clock.now)

    def boom():
        raise RuntimeError("screener down")

    monkeypatch.setattr(s, "gapups", boom)
    s.morning()
    assert any("gapup: stopped with an error" in m for m in out)
    assert "td-20261026-breakout-BREAKY" in [o["client_order_id"] for o in broker.orders]   # the morning went on


def test_funds_are_told_apart_from_companies_by_name():
    assert daytrade.is_fund("iShares MSCI Brazil ETF") and daytrade.is_fund("Direxion Daily Semiconductor Bull 3X Shares")
    assert daytrade.is_fund("Invesco QQQ Trust, Series 1") and daytrade.is_fund("ProShares UltraPro QQQ")
    assert not daytrade.is_fund("Northern Trust Corporation") and not daytrade.is_fund("XP Inc. Class A Common Stock")



def test_a_gap_made_by_an_earnings_report_isnt_faded(monkeypatch):
    reports = {"GAPPY": [pd.Timestamp("2026-10-23 16:05", tz="America/New_York"),    # Friday after the close
                         pd.Timestamp("2026-07-24 16:05", tz="America/New_York")],
               "BREAKY": [pd.Timestamp("2026-10-27 07:00", tz="America/New_York")]}  # tomorrow: not today
    monkeypatch.setattr(daytrade.intraday, "_yahoo_earnings", lambda symbol, limit=60: reports.get(symbol, []))
    clock = Clock(dt.datetime(2026, 10, 26, 9, 17, tzinfo=daytrade.NY))
    broker, out = FakeBroker(), []
    s = daytrade.Session(broker, FakeData(clock), ["SPY", "GAPPY", "BREAKY"], DAY, log=out.append,
                         sleep=clock.sleep, now=clock.now)
    s.morning()
    tags = [o["client_order_id"] for o in broker.orders]
    assert "td-20261026-gap-GAPPY" not in tags and any("earnings today" in m and "GAPPY" in m for m in out)
    assert "td-20261026-breakout-BREAKY" in tags                      # other patterns unaffected


def test_report_days_count_after_the_close_for_the_next_trading_day():
    from trend_bot import intraday

    assert intraday.report_day(pd.Timestamp("2026-10-23 16:05", tz="America/New_York")) == "2026-10-26"   # Fri -> Mon
    assert intraday.report_day(pd.Timestamp("2026-10-26 07:00", tz="America/New_York")) == "2026-10-26"
    assert intraday.reporting_today(["A", "SPY"], DAY, fetch=lambda s: (_ for _ in ()).throw(RuntimeError("down")),
                                    log=lambda *_: None) == set()     # Yahoo down: nothing skipped



def test_no_more_than_about_three_positions_one_way_in_one_sector():
    clock = Clock(dt.datetime(2026, 10, 26, 10, 0, tzinfo=daytrade.NY))

    class Held(FakeBroker):     # already short two chip stocks (~$9,800)
        def positions(self):
            return [{"symbol": "AAA", "qty": "-49", "market_value": "-4900"},
                    {"symbol": "BBB", "qty": "-49", "market_value": "-4900"}]

    broker, out = Held(), []
    sectors = {"AAA": "Technology", "BBB": "Technology", "CCC": "Technology", "DDD": "Technology",
               "EEE": "Technology", "FIN": "Financial Services"}
    s = daytrade.Session(broker, None, ["CCC", "DDD", "EEE", "FIN"], DAY, log=out.append, now=clock.now,
                         sectors=sectors)
    s.last_trades = lambda syms: {x: clock.now() for x in syms}
    prices = {x: 100.0 for x in ("CCC", "DDD", "EEE", "FIN")}
    s.enter("gap", [("CCC", -1), ("DDD", -1), ("EEE", 1), ("FIN", -1)], prices, 10)
    sides = {o["symbol"]: o["side"] for o in broker.orders}
    assert sides == {"CCC": "sell", "EEE": "buy", "FIN": "sell"}       # DDD would be a 4th tech short: skipped
    assert any("too much in Technology" in m and "DDD" in m for m in out)
    assert sectors["AAA"] == "Technology"                               # remembered, not looked up again



def test_a_held_over_trade_takes_its_queued_close_not_todays_trade_in_the_same_stock():
    """Oct 27: WDC was left open Monday (closed by the safety net's order at Tuesday's open) and traded
    again Tuesday. The old row must take the safety net's fill; Tuesday's trade keeps its own exit."""
    today = dt.date(2026, 10, 27)
    state = {"start_equity": 100_000.0,
             "log": [{"date": "2026-10-26", "symbol": "WDC", "pattern": "breakout", "side": 1, "qty": 12,
                      "entry": 380.0, "exit": None, "ret": None, "pnl": None}],
             "days": {"2026-10-26": {"breakout": {"pnl": 0.0, "trades": 0}}}}
    earlier = [{"id": "s1", "symbol": "WDC", "side": "sell", "status": "filled", "filled_qty": "12",
                "filled_avg_price": "382", "submitted_at": "2026-10-26T20:15:00Z", "filled_at": "2026-10-27T13:30:01Z"}]
    orders = [{"symbol": "WDC", "side": "buy", "status": "filled", "filled_qty": "12", "filled_avg_price": "384.32",
               "client_order_id": "td-20261027-breakout-WDC", "submitted_at": "2026-10-27T15:00:58Z"},
              {"symbol": "WDC", "side": "sell", "status": "filled", "filled_qty": "12", "filled_avg_price": "386.0",
               "submitted_at": "2026-10-27T19:50:01Z"}]
    state, e = daytrade.report(orders, 100_044.0, state, today, last_equity=100_000.0, earlier=earlier)
    old = [r for r in state["log"] if r["date"] == "2026-10-26"][0]
    new = [r for r in state["log"] if r["date"] == "2026-10-27"][0]
    assert old["exit"] == 382.0 and old["pnl"] == pytest.approx(24)
    assert new["exit"] == 386.0 and new["pnl"] == pytest.approx(12 * (386 - 384.32))
    assert "Not closed yet" not in e["description"]
    again, _ = daytrade.report(orders, 100_044.0, state, today, last_equity=100_000.0, earlier=earlier)   # a redo
    assert again["days"]["2026-10-26"]["breakout"] == {"pnl": pytest.approx(24), "trades": 1}        # not doubled


def test_rows_settled_from_todays_own_orders_before_the_fix_are_put_back():
    today = dt.date(2026, 10, 27)
    state = {"start_equity": 100_000.0,
             "log": [{"date": "2026-10-26", "symbol": "WDC", "pattern": "breakout", "side": 1, "qty": 12,
                      "entry": 380.0, "exit": 386.0, "ret": 386 / 380 - 1, "pnl": 72.0}],   # took today's exit
             "days": {"2026-10-26": {"breakout": {"pnl": 72.0, "trades": 1}}}}
    orders = [{"symbol": "WDC", "side": "sell", "status": "filled", "filled_qty": "12", "filled_avg_price": "386.0",
               "submitted_at": "2026-10-27T19:50:01Z"}]
    earlier = [{"id": "s1", "symbol": "WDC", "side": "sell", "status": "filled", "filled_qty": "12",
                "filled_avg_price": "382", "submitted_at": "2026-10-26T20:15:00Z", "filled_at": "2026-10-27T13:30:01Z"}]
    state, _ = daytrade.report(orders, 100_000.0, state, today, earlier=earlier)
    old = [r for r in state["log"] if r["date"] == "2026-10-26"][0]
    assert old["exit"] == 382.0 and state["days"]["2026-10-26"]["breakout"] == {"pnl": pytest.approx(24), "trades": 1}
