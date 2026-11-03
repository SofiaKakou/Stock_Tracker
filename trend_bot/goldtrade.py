"""The gold trader: spot gold (XAUUSD) on an Interactive Brokers PAPER account, checked every few seconds.

Runs on your own computer, not on GitHub: IBKR only takes orders through its IB Gateway (or TWS) program,
which has to be open and logged in the whole time. Setup: GUIDE.md, "The gold trader (IBKR)".

The rule (simple on purpose, untested on history: the live paper results decide):
- every 5 seconds it reads gold's bid and ask and updates two averages of the middle price, a fast one
  (about the last minute) and a slow one (about the last 5 minutes)
- the fast average crossing above the slow one = gold is starting to rise: buy about $10,000 of gold
  (and, with --short, crossing below = sell short)
- out at whichever comes first: 0.15% against us (the safety stop, also placed AT THE BROKER, so it works
  even if this program or your computer stops), 0.30% in our favour, the averages crossing back (after at
  least a minute), or 30 minutes

Safety:
- paper only: it refuses to run unless every account it can see is a paper account (IDs start with "DU")
- daily loss limit: down $150 on the day (closed trades plus the open one) -> closed, no more trades today
- at most 40 trades a day, 2 minutes' rest after each, no new trade when the spread is unusually wide or the
  price is more than 30 seconds old
- spot gold stops trading 17:00-18:00 New York every day and over the weekend: no new trades from 16:45,
  anything open is closed at 16:50
- it starts flat: anything left open from an earlier run (a crash) is closed first, and you're told
"""

from __future__ import annotations

import csv
import datetime as dt
import math
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
PAPER_PREFIX = "DU"      # IBKR paper account IDs start with DU; real ones with U
LOG_FIELDS = ["opened", "closed", "side", "ounces", "entry", "exit", "commission", "pnl", "reason"]


class GoldError(RuntimeError):
    pass


@dataclass
class Rules:
    every: float = 5.0                    # seconds between checks
    fast: int = 12                        # fast average: 12 checks = about a minute
    slow: int = 60                        # slow average: about 5 minutes
    amount: float = 10_000.0              # dollars of gold a trade (fake money)
    stop: float = 0.0015                  # out 0.15% against us (also a stop order at the broker)
    target: float = 0.0030                # out 0.30% in our favour
    min_hold: dt.timedelta = dt.timedelta(minutes=1)      # the averages crossing back only counts after this
    max_hold: dt.timedelta = dt.timedelta(minutes=30)
    cooldown: dt.timedelta = dt.timedelta(minutes=2)      # rest after each trade
    max_spread: float = 0.0005            # no new trade if ask/bid - 1 is wider than 0.05%
    stale: dt.timedelta = dt.timedelta(seconds=30)        # a price older than this isn't traded on
    max_trades: int = 40                  # a day
    daily_loss: float = 150.0             # dollars: closed trades plus the open one
    short: bool = False                   # also sell short when the averages cross down


def session(now: dt.datetime) -> str:
    """'open', 'closing' (no new trades, close what's open) or 'closed', for spot gold in New York time.

    Spot gold trades Sunday 18:00 to Friday 17:00 New York, with a break 17:00-18:00 every day."""
    ny = now.astimezone(NY)
    minutes = ny.hour * 60 + ny.minute
    wd = ny.weekday()   # Monday 0 ... Sunday 6
    if wd == 5 or (wd == 6 and minutes < 18 * 60 + 5) or (wd == 4 and minutes >= 17 * 60):
        return "closed"
    if 17 * 60 <= minutes < 18 * 60 + 5:
        return "closed"
    if 16 * 60 + 45 <= minutes < 17 * 60:
        return "closing"
    return "open"


def trading_day(now: dt.datetime) -> dt.date:
    """A gold trading day starts at 18:00 New York (Sunday evening's session counts as Monday)."""
    return (now.astimezone(NY) + dt.timedelta(hours=6)).date()


@dataclass
class Action:
    kind: str             # "buy", "short", "sell" (close a long), "cover" (close a short)
    reason: str


@dataclass
class Brain:
    """Decides what to do from the prices; knows nothing about IBKR (so it can be tested on its own)."""

    rules: Rules = field(default_factory=Rules)
    fast_avg: float | None = None
    slow_avg: float | None = None
    seen: int = 0
    side: int = 0                         # 1 long, -1 short, 0 flat
    ounces: float = 0.0
    entry: float = 0.0
    entry_commission: float = 0.0
    opened: dt.datetime | None = None
    last_exit: dt.datetime | None = None
    day: dt.date | None = None
    trades: int = 0
    pnl: float = 0.0                      # today's closed trades, after commissions
    halted: str = ""                      # why no more trades today

    def _new_day(self, now: dt.datetime) -> bool:
        d = trading_day(now)
        if d != self.day:
            self.day, self.trades, self.pnl, self.halted = d, 0, 0.0, ""
            return True
        return False

    def unrealized(self, bid: float, ask: float) -> float:
        if not self.side:
            return 0.0
        price = bid if self.side > 0 else ask
        return self.side * (price - self.entry) * self.ounces - self.entry_commission

    def decide(self, now: dt.datetime, bid: float, ask: float, quoted: dt.datetime | None) -> Action | None:
        r = self.rules
        self._new_day(now)
        good = bid > 0 and ask >= bid and not math.isnan(bid) and not math.isnan(ask)
        fresh = good and quoted is not None and now - quoted <= r.stale
        state = session(now)

        # 1. closing what's open (these work even on an old price: getting out matters more)
        if self.side:
            if state != "open":
                return Action(self._close_kind(), "gold's daily break is near" if state == "closing" else "market closed")
            if good:
                if self.pnl + self.unrealized(bid, ask) <= -r.daily_loss:
                    self.halted = f"daily loss limit (${r.daily_loss:,.0f}) reached"
                    return Action(self._close_kind(), self.halted)
                price = bid if self.side > 0 else ask
                move = self.side * (price / self.entry - 1)
                if move <= -r.stop:
                    return Action(self._close_kind(), f"safety stop ({r.stop:.2%} against us)")
                if move >= r.target:
                    return Action(self._close_kind(), f"target reached (+{r.target:.2%})")
            if self.opened and now - self.opened >= r.max_hold:
                return Action(self._close_kind(), f"held {int(r.max_hold.total_seconds() // 60)} minutes")

        if not fresh:
            return None
        prev_fast, prev_slow = self.fast_avg, self.slow_avg
        mid = (bid + ask) / 2
        self.fast_avg = mid if prev_fast is None else prev_fast + 2 / (r.fast + 1) * (mid - prev_fast)
        self.slow_avg = mid if prev_slow is None else prev_slow + 2 / (r.slow + 1) * (mid - prev_slow)
        self.seen += 1
        if self.seen <= r.slow or prev_fast is None:
            return None   # still warming up (about 5 minutes after starting)
        up = prev_fast <= prev_slow and self.fast_avg > self.slow_avg
        down = prev_fast >= prev_slow and self.fast_avg < self.slow_avg

        if self.side:
            crossed_back = down if self.side > 0 else up
            if crossed_back and self.opened and now - self.opened >= r.min_hold:
                return Action(self._close_kind(), "the averages crossed back")
            return None

        # 2. opening a new trade
        if state != "open" or self.halted or self.trades >= r.max_trades:
            return None
        if self.last_exit and now - self.last_exit < r.cooldown:
            return None
        if ask / bid - 1 > r.max_spread:
            return None
        if up:
            return Action("buy", "fast average crossed above the slow one")
        if down and r.short:
            return Action("short", "fast average crossed below the slow one")
        return None

    def _close_kind(self) -> str:
        return "sell" if self.side > 0 else "cover"

    def size(self, price: float) -> int:
        """Whole ounces worth about the trade amount (at least 1)."""
        return max(1, int(self.rules.amount // price))

    def did_open(self, side: int, ounces: float, price: float, commission: float, when: dt.datetime) -> None:
        self.side, self.ounces, self.entry, self.entry_commission, self.opened = side, ounces, price, commission, when
        self.trades += 1

    def did_close(self, price: float, commission: float, when: dt.datetime, reason: str) -> dict:
        """Books the finished trade; returns its row for the log."""
        pnl = self.side * (price - self.entry) * self.ounces - self.entry_commission - commission
        row = {"opened": self.opened.isoformat(timespec="seconds") if self.opened else "",
               "closed": when.isoformat(timespec="seconds"), "side": "long" if self.side > 0 else "short",
               "ounces": self.ounces, "entry": round(self.entry, 2), "exit": round(price, 2),
               "commission": round(self.entry_commission + commission, 2), "pnl": round(pnl, 2), "reason": reason}
        self.pnl += pnl
        self.side, self.ounces, self.entry, self.entry_commission, self.opened = 0, 0.0, 0.0, 0.0, None
        self.last_exit = when
        return row


def append_log(path: str | Path, row: dict) -> None:
    path = Path(path)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def trade_embed(row: dict, brain: Brain) -> dict:
    from trend_bot.alerts import GREEN, RED

    sign = "+" if row["pnl"] >= 0 else "-"
    return {"title": f"🥇 Gold {row['side']}: {sign}${abs(row['pnl']):,.2f}",
            "description": (f"{row['ounces']:g} oz, in at ${row['entry']:,.2f}, out at ${row['exit']:,.2f} "
                            f"({row['reason']}). Today: {brain.trades} trades, ${brain.pnl:+,.2f}. Fake money."),
            "color": GREEN if row["pnl"] >= 0 else RED}


def note_embed(text: str) -> dict:
    from trend_bot.alerts import GOLD

    return {"title": "🥇 Gold trader", "description": text, "color": GOLD}


# --- the IBKR side -------------------------------------------------------------------------------

class IBGold:
    """The few IB Gateway calls the trader needs (via the ib_async package). Paper accounts only."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4002, client_id: int = 17, symbol: str = "XAUUSD"):
        try:
            from ib_async import IB, Commodity
        except ImportError as e:   # only needed on the computer that trades
            raise GoldError("the ib_async package isn't installed: pip install -r requirements-gold.txt") from e
        self.ib = IB()
        try:
            self.ib.connect(host, port, clientId=client_id, timeout=20)
        except Exception as e:
            raise GoldError(f"couldn't reach IB Gateway on {host}:{port} ({e}). Is it open, logged in to the "
                            "PAPER account, with the API turned on?") from e
        accounts = self.ib.managedAccounts()
        if not accounts or not all(a.startswith(PAPER_PREFIX) for a in accounts):
            self.ib.disconnect()
            raise GoldError(f"refusing to trade: {accounts or 'no account'} isn't a paper account (paper IDs start "
                            f"with {PAPER_PREFIX}). Log in to IB Gateway with your paper username.")
        self.account = accounts[0]
        found = self.ib.qualifyContracts(Commodity(symbol, "SMART", "USD"))
        if not found:
            self.ib.disconnect()
            raise GoldError(f"IBKR didn't recognise {symbol}; your account may need trading permission for metals "
                            "(Settings -> Account Settings -> Trading Permissions).")
        self.contract = found[0]
        self.ticker = self.ib.reqMktData(self.contract, "", False, False)
        self.stop_trade = None

    def quote(self) -> tuple[float, float, dt.datetime | None]:
        t = self.ticker
        bid, ask = float(t.bid or math.nan), float(t.ask or math.nan)
        when = t.time if isinstance(t.time, dt.datetime) else None
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return bid, ask, when

    def position(self) -> float:
        return sum(p.position for p in self.ib.positions(self.account) if p.contract.conId == self.contract.conId)

    def sleep(self, seconds: float) -> None:
        self.ib.sleep(seconds)

    def cancel_open_orders(self) -> None:
        for tr in self.ib.openTrades():
            if tr.contract.conId == self.contract.conId:
                self.ib.cancelOrder(tr.order)
        self.ib.sleep(1)
        self.stop_trade = None

    def trade(self, action: str, ounces: float, limit: float, wait: float = 15) -> tuple[float, float, float]:
        """A limit order that may pay up to `limit`; waits up to `wait` seconds. (ounces filled, price, commission)"""
        from ib_async import LimitOrder

        tr = self.ib.placeOrder(self.contract, LimitOrder(action, ounces, round(limit, 2), tif="DAY"))
        waited = 0.0
        while not tr.isDone() and waited < wait:
            self.ib.sleep(0.5)
            waited += 0.5
        if not tr.isDone():
            self.ib.cancelOrder(tr.order)
            self.ib.sleep(2)
        return float(tr.orderStatus.filled), float(tr.orderStatus.avgFillPrice or 0), _commission(tr)

    def place_stop(self, action: str, ounces: float, price: float) -> None:
        """The safety stop at the broker (good until cancelled): works even if this program stops."""
        from ib_async import StopOrder

        self.stop_trade = self.ib.placeOrder(self.contract, StopOrder(action, ounces, round(price, 2), tif="GTC"))

    def stop_fill(self) -> tuple[float, float] | None:
        """(price, commission) if the broker's safety stop filled."""
        tr = self.stop_trade
        if tr is not None and tr.orderStatus.status == "Filled":
            return float(tr.orderStatus.avgFillPrice), _commission(tr)
        return None

    def close(self) -> None:
        self.ib.disconnect()


def _commission(tr) -> float:
    total = 0.0
    for f in tr.fills:
        c = getattr(getattr(f, "commissionReport", None), "commission", 0.0) or 0.0
        if 0 <= c < 1e9:   # IBKR sends a huge number for "not known yet"
            total += c
    return total


# --- the loop --------------------------------------------------------------------------------------

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Runner:
    """Glues the Brain to the broker: checks every few seconds, trades, logs, tells Discord."""

    SLIP = 0.0005   # a limit order may pay up to 0.05% past the current price (so it fills, but never wildly)

    def __init__(self, broker: IBGold, brain: Brain, log_path: str | Path, notify=None, dry_run: bool = False):
        self.broker, self.brain, self.log_path = broker, brain, log_path
        self.notify = notify or (lambda embed: None)
        self.dry_run = dry_run

    def say(self, text: str) -> None:
        print(f"{_now():%Y-%m-%d %H:%M:%S} {text}", flush=True)

    def start(self) -> None:
        b = self.broker
        if self.dry_run:
            self.say("Dry run: real prices, no orders sent.")
            return
        b.cancel_open_orders()
        left = b.position()
        if left:
            self.say(f"Found {left:g} oz of gold left open from an earlier run: closing it first.")
            bid, ask, _ = b.quote()
            if left > 0 and bid > 0:
                b.trade("SELL", left, bid * (1 - self.SLIP))
            elif left < 0 and ask > 0:
                b.trade("BUY", -left, ask * (1 + self.SLIP))
            self.notify(note_embed(f"Started; {abs(left):g} oz left open from an earlier run were closed."))

    def step(self) -> None:
        b, brain = self.broker, self.brain
        now = _now()
        bid, ask, quoted = b.quote()
        if not self.dry_run and brain.side:
            hit = b.stop_fill()
            if hit:
                row = brain.did_close(hit[0], hit[1], now, "safety stop at the broker")
                b.stop_trade = None
                self._booked(row)
                return
        act = brain.decide(now, bid, ask, quoted)
        if act is None:
            return
        if act.kind in ("buy", "short"):
            self._open(act, bid, ask, now)
        else:
            self._close(act, bid, ask, now)

    def _open(self, act: Action, bid: float, ask: float, now: dt.datetime) -> None:
        b, brain, r = self.broker, self.brain, self.brain.rules
        side = 1 if act.kind == "buy" else -1
        price = ask if side > 0 else bid
        ounces = brain.size(price)
        if self.dry_run:
            filled, fill, commission = ounces, price, 0.0
        else:
            limit = ask * (1 + self.SLIP) if side > 0 else bid * (1 - self.SLIP)
            filled, fill, commission = b.trade("BUY" if side > 0 else "SELL", ounces, limit)
        if not filled:
            self.say(f"{act.kind} not filled in time; skipped.")
            brain.last_exit = now   # rest before trying again
            return
        brain.did_open(side, filled, fill, commission, now)
        if not self.dry_run:
            b.place_stop("SELL" if side > 0 else "BUY", filled, fill * (1 - side * r.stop))
        self.say(f"{'Bought' if side > 0 else 'Sold short'} {filled:g} oz at ${fill:,.2f} ({act.reason}).")

    def _close(self, act: Action, bid: float, ask: float, now: dt.datetime) -> None:
        b, brain = self.broker, self.brain
        side = brain.side
        if self.dry_run:
            fill, commission = (bid if side > 0 else ask), 0.0
        else:
            hit = b.stop_fill()
            b.cancel_open_orders()          # the safety stop first, so it can't fill as well
            ounces = abs(b.position())
            if not ounces:                  # the stop filled just before
                fill, commission = hit if hit else ((bid if side > 0 else ask), 0.0)
                self._booked(brain.did_close(fill, commission, now, "safety stop at the broker"))
                return
            if side > 0:
                filled, fill, commission = b.trade("SELL", ounces, (bid or brain.entry) * (1 - 4 * self.SLIP))
            else:
                filled, fill, commission = b.trade("BUY", ounces, (ask or brain.entry) * (1 + 4 * self.SLIP))
            if filled < ounces:             # not out: put the safety stop back and try again next check
                left = abs(b.position())
                if left:
                    b.place_stop("SELL" if side > 0 else "BUY", left, brain.entry * (1 - side * brain.rules.stop))
                self.say(f"Closing didn't fill ({act.reason}); retrying.")
                return
        self._booked(brain.did_close(fill, commission, now, act.reason))

    def _booked(self, row: dict) -> None:
        append_log(self.log_path, row)
        self.say(f"Closed {row['side']} {row['ounces']:g} oz at ${row['exit']:,.2f}: ${row['pnl']:+,.2f} "
                 f"({row['reason']}). Today: {self.brain.trades} trades, ${self.brain.pnl:+,.2f}.")
        self.notify(trade_embed(row, self.brain))
        if self.brain.halted:
            self.notify(note_embed(f"No more trades today: {self.brain.halted}."))

    def run(self, minutes: float | None = None) -> None:
        self.start()
        until = _now() + dt.timedelta(minutes=minutes) if minutes else None
        self.say(f"Watching gold every {self.brain.rules.every:g}s (first trade possible after ~5 minutes of prices).")
        last_state = None
        while until is None or _now() < until:
            state = session(_now())
            if state != last_state:
                self.say({"open": "Gold market open.", "closing": "Gold's daily break is near: no new trades.",
                          "closed": "Gold market closed; waiting."}[state])
                last_state = state
            self.step()
            self.broker.sleep(self.brain.rules.every)

    def summary(self) -> dict:
        b = self.brain
        return note_embed(f"Stopped. Today: {b.trades} trades, ${b.pnl:+,.2f} after commissions (fake money)."
                          + (" Still holding gold: its safety stop stays at the broker." if b.side else ""))
