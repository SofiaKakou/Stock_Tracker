import datetime as dt

from trend_bot import goldtrade as g

UTC = dt.timezone.utc
# A Tuesday, 10:00 New York (14:00 UTC in November): gold is open.
T0 = dt.datetime(2026, 11, 3, 15, 0, tzinfo=UTC)


def feed(brain, mids, start=T0, step=5, spread=0.2):
    """Feeds mid prices one check at a time; returns [(time, Action)] for the checks that acted."""
    out = []
    for i, m in enumerate(mids):
        now = start + dt.timedelta(seconds=step * i)
        act = brain.decide(now, m - spread / 2, m + spread / 2, now)
        if act:
            out.append((now, act, m))
    return out


def test_session_hours():
    ny = g.NY
    at = lambda y, mo, d, h, mi: dt.datetime(y, mo, d, h, mi, tzinfo=ny)
    assert g.session(at(2026, 11, 3, 10, 0)) == "open"        # Tuesday morning
    assert g.session(at(2026, 11, 3, 16, 50)) == "closing"    # before the daily break
    assert g.session(at(2026, 11, 3, 17, 30)) == "closed"     # the daily break
    assert g.session(at(2026, 11, 3, 18, 10)) == "open"
    assert g.session(at(2026, 11, 6, 17, 1)) == "closed"      # Friday evening
    assert g.session(at(2026, 11, 7, 12, 0)) == "closed"      # Saturday
    assert g.session(at(2026, 11, 8, 17, 59)) == "closed"     # Sunday before the open
    assert g.session(at(2026, 11, 8, 18, 10)) == "open"       # Sunday evening = Monday's session
    assert g.trading_day(at(2026, 11, 8, 18, 10)) == dt.date(2026, 11, 9)


def test_no_trade_while_warming_up_then_buys_the_upturn():
    brain = g.Brain(g.Rules())
    flat_then_up = [4000.0] * 70 + [4000 + 0.5 * i for i in range(1, 30)]
    acts = feed(brain, flat_then_up)
    assert acts and acts[0][1].kind == "buy"
    assert acts[0][0] >= T0 + dt.timedelta(seconds=5 * 61)   # not before the slow average has warmed up


def test_long_only_by_default_and_short_when_asked():
    down = [4000.0] * 70 + [4000 - 0.5 * i for i in range(1, 30)]
    assert feed(g.Brain(g.Rules()), down) == []
    acts = feed(g.Brain(g.Rules(short=True)), down)
    assert acts[0][1].kind == "short"


def open_long(brain, price=4000.0, when=T0, ounces=2):
    brain._new_day(when)
    brain.did_open(1, ounces, price, 1.0, when)
    brain.fast_avg = brain.slow_avg = price
    brain.seen = 1000


def test_safety_stop_and_target():
    brain = g.Brain(g.Rules())
    open_long(brain)
    later = T0 + dt.timedelta(seconds=10)
    act = brain.decide(later, 3993.0, 3993.4, later)        # -0.175% on the bid
    assert act.kind == "sell" and "safety stop" in act.reason
    brain = g.Brain(g.Rules())
    open_long(brain)
    act = brain.decide(later, 4012.5, 4012.9, later)        # +0.31%
    assert act.kind == "sell" and "target" in act.reason


def test_max_hold_and_closes_before_the_daily_break():
    brain = g.Brain(g.Rules())
    open_long(brain)
    late = T0 + dt.timedelta(minutes=31)
    assert "held 30 minutes" in brain.decide(late, 4000, 4000.2, late).reason
    brain = g.Brain(g.Rules())
    before_break = dt.datetime(2026, 11, 3, 16, 50, tzinfo=g.NY)
    open_long(brain, when=before_break - dt.timedelta(minutes=2))
    assert brain.decide(before_break, 4000, 4000.2, before_break).kind == "sell"


def test_closing_works_even_on_an_old_price():
    brain = g.Brain(g.Rules())
    open_long(brain)
    later = T0 + dt.timedelta(minutes=5)
    act = brain.decide(later, 3990.0, 3990.4, later - dt.timedelta(minutes=3))   # the price is 3 minutes old
    assert act is not None and act.kind == "sell"


def test_daily_loss_limit_halts_for_the_day():
    brain = g.Brain(g.Rules(daily_loss=150))
    open_long(brain)
    brain.pnl = -140.0
    now = T0 + dt.timedelta(seconds=10)
    act = brain.decide(now, 3995.0, 3995.4, now)    # -5 x 2 oz - $1 commission = -$11: past -$150 in all
    assert act.kind == "sell" and "daily loss limit" in act.reason
    brain.did_close(3995.0, 1.0, now, act.reason)
    assert brain.halted
    # no new trades the rest of the day, however good the signal
    up = [3996.0] * 70 + [3996 + 0.5 * i for i in range(1, 30)]
    assert feed(brain, up, start=now + dt.timedelta(minutes=5)) == []


def test_new_day_resets_the_limits():
    brain = g.Brain(g.Rules())
    brain._new_day(T0)
    brain.trades, brain.pnl, brain.halted = 40, -200.0, "daily loss limit"
    tomorrow = T0 + dt.timedelta(days=1)
    brain.decide(tomorrow, 4000, 4000.2, tomorrow)
    assert (brain.trades, brain.pnl, brain.halted) == (0, 0.0, "")


def test_wide_spread_and_stale_price_block_new_trades():
    up = [4000.0] * 70 + [4000 + 0.5 * i for i in range(1, 30)]
    assert feed(g.Brain(g.Rules()), up, spread=4.0) == []    # 0.1% spread, over the 0.05% limit
    brain = g.Brain(g.Rules())
    for i, m in enumerate(up):
        now = T0 + dt.timedelta(seconds=5 * i)
        assert brain.decide(now, m - 0.1, m + 0.1, now - dt.timedelta(minutes=1)) is None


def test_pnl_counts_both_commissions():
    brain = g.Brain(g.Rules())
    open_long(brain, price=4000.0, ounces=2)        # $1 commission in
    row = brain.did_close(4010.0, 1.5, T0 + dt.timedelta(minutes=3), "target")
    assert row["pnl"] == 2 * 10 - 1.0 - 1.5
    assert row["commission"] == 2.5 and brain.pnl == row["pnl"] and brain.side == 0
    brain = g.Brain(g.Rules())
    brain._new_day(T0)
    brain.did_open(-1, 2, 4000.0, 0.0, T0)
    assert brain.did_close(3990.0, 0.0, T0, "x")["pnl"] == 20.0   # a short gains when gold falls


def test_size_is_whole_ounces():
    brain = g.Brain(g.Rules(amount=10_000))
    assert brain.size(4000.0) == 2
    assert brain.size(20_000.0) == 1


class FakeBroker:
    """Stands in for IB Gateway: fills every order at its limit price."""

    def __init__(self):
        self.pos, self.orders, self.stops, self.price = 0.0, [], [], (4000.0, 4000.2)
        self.stop_trade, self.stop_hit = None, None

    def quote(self):
        return (*self.price, g._now())

    def position(self):
        return self.pos

    def sleep(self, s):
        pass

    def cancel_open_orders(self):
        self.stops, self.stop_trade = [], None

    def trade(self, action, ounces, limit, wait=15):
        self.orders.append((action, ounces, round(limit, 2)))
        self.pos += ounces if action == "BUY" else -ounces
        return ounces, limit, 0.5

    def place_stop(self, action, ounces, price):
        self.stops.append((action, ounces, round(price, 2)))
        self.stop_trade = object()

    def stop_fill(self):
        return self.stop_hit


def test_runner_opens_with_a_broker_stop_and_closes(tmp_path):
    broker, brain = FakeBroker(), g.Brain(g.Rules())
    runner = g.Runner(broker, brain, tmp_path / "gold.csv")
    now = g._now()
    runner._open(g.Action("buy", "test"), 4000.0, 4000.2, now)
    assert broker.pos == 2 and broker.orders[0][0] == "BUY"
    assert broker.stops == [("SELL", 2, round(brain.entry * (1 - 0.0015), 2))]
    runner._close(g.Action("sell", "test"), 4005.0, 4005.2, now)
    assert broker.pos == 0 and broker.stops == []      # the broker stop is cancelled before selling
    assert (tmp_path / "gold.csv").read_text().count("\n") == 2   # header + one trade


def test_runner_books_a_broker_stop_fill(tmp_path):
    broker, brain = FakeBroker(), g.Brain(g.Rules())
    runner = g.Runner(broker, brain, tmp_path / "gold.csv")
    runner._open(g.Action("buy", "test"), 4000.0, 4000.2, g._now())
    broker.pos, broker.stop_hit = 0.0, (3994.0, 0.5)    # the stop filled while we weren't looking
    runner.step()
    assert brain.side == 0 and brain.pnl < 0
    assert "safety stop at the broker" in (tmp_path / "gold.csv").read_text()


def test_runner_starts_flat(tmp_path):
    broker = FakeBroker()
    broker.pos = 3.0
    g.Runner(broker, g.Brain(g.Rules()), tmp_path / "gold.csv").start()
    assert broker.pos == 0 and broker.orders[0][:2] == ("SELL", 3.0)


def test_dry_run_sends_no_orders(tmp_path):
    broker, brain = FakeBroker(), g.Brain(g.Rules())
    runner = g.Runner(broker, brain, tmp_path / "gold.csv", dry_run=True)
    runner._open(g.Action("buy", "test"), 4000.0, 4000.2, g._now())
    runner._close(g.Action("sell", "test"), 4001.0, 4001.2, g._now())
    assert broker.orders == [] and broker.stops == [] and brain.trades == 1


def test_refuses_without_ib_async_or_gateway():
    try:
        g.IBGold(port=1)
    except g.GoldError:
        pass
    else:
        raise AssertionError("should not connect")
