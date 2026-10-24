"""Day-trading demo account: the intraday patterns from `study intraday`, traded live with fake money
on a second Alpaca paper account (separate from the monthly portfolio's account).

Each weekday, two scheduled jobs share the day (a GitHub job may run at most 6 hours):
- morning (from 9:30 New York time): gap fade at the open, then breakout checks at 10:30 ... 12:00
- afternoon (from 12:30): breakout checks at 12:30 ... 15:30, the market's last half hour at 15:30,
  everything closed at the closing price (market-on-close orders at 15:45, anything left at 15:58),
  and an evening report: today's trades, profit or loss per pattern, the running total, next to each
  pattern's result in the 10-year test.

Up to 20 positions a day (17 stocks, 3 index funds), equal amounts (about 4.9% of the account
each), long or short. Nothing is held overnight. State (what was already traded today) is read
back from the broker's own order list, so either job can stop and the other carries on.

Safety: same fixed paper-trading address as the monthly demo account (paper keys only work there).
Nothing happens without the ALPACA_DAY_KEY_ID / ALPACA_DAY_SECRET_KEY secrets.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import time
from zoneinfo import ZoneInfo

import numpy as np

from trend_bot import intraday
from trend_bot.paper import Paper, PaperError

KEY_ENV, SECRET_ENV = "ALPACA_DAY_KEY_ID", "ALPACA_DAY_SECRET_KEY"
NY = ZoneInfo("America/New_York")
STOCK_SLOTS, ETF_SLOTS = 17, 3
INVESTED = 0.98
GAP_LATEST = dt.time(10, 15)          # a late start still fades the gap until 10:15, not after
MORNING_CHECKS = [dt.time(10, 30), dt.time(11, 0), dt.time(11, 30), dt.time(12, 0)]
AFTERNOON_CHECKS = [dt.time(12, 30), dt.time(13, 0), dt.time(13, 30), dt.time(14, 0), dt.time(14, 30),
                    dt.time(15, 0), dt.time(15, 30)]
MOC_AT, FLAT_AT, SETTLE_AT = dt.time(15, 45), dt.time(15, 58), dt.time(16, 1)
STOP_LOSS = 0.05        # every position has a stop at the broker: out if it moves 5% against us
DAILY_LOSS_LIMIT = 0.03  # the account down 3% on the day: close everything, no more trades today
GUARD_EVERY = 300        # seconds between loss-limit checks while waiting
STALE_MINUTES = 15       # no trade in a stock for this long (halted?): don't enter it
FALLBACK_STOCKS = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "AMD", "NFLX", "JPM", "COST",
                   "LLY", "UNH", "XOM", "BAC", "WMT", "ORCL", "CRM", "ADBE", "INTC", "QCOM", "MU", "PLTR", "COIN",
                   "UBER", "BA", "DIS", "PYPL", "SHOP", "SMCI", "MSTR", "GS", "CAT", "HD", "PEP", "KO", "MRK",
                   "ABBV", "PFE", "CVX", "WFC", "C", "TXN", "AMAT", "LRCX", "PANW"]


class DayBroker(Paper):
    @classmethod
    def from_env(cls) -> "DayBroker | None":
        key, secret = os.environ.get(KEY_ENV), os.environ.get(SECRET_ENV)
        return cls(key, secret) if key and secret else None

    def order(self, symbol: str, qty: int, side: str, tag: str, tif: str = "day",
              stop: float | None = None) -> dict:
        body = {"symbol": symbol, "qty": str(qty), "side": side, "type": "market", "time_in_force": tif,
                "client_order_id": tag}
        if stop:   # a protective stop at the broker, placed with the entry (works even if our job stops)
            body.update(order_class="oto", stop_loss={"stop_price": f"{stop:.2f}"})
        return self._call("POST", "/orders", json=body)

    def stop_order(self, symbol: str, qty: int, side: str, stop: float, tag: str) -> dict:
        """A stand-alone protective stop (for a position that has none)."""
        return self._call("POST", "/orders", json={"symbol": symbol, "qty": str(qty), "side": side, "type": "stop",
                                                    "stop_price": f"{stop:.2f}", "time_in_force": "day",
                                                    "client_order_id": tag})

    def asset(self, symbol: str) -> dict:
        return self._call("GET", f"/assets/{symbol}")

    def cancel_all(self) -> None:
        self._call("DELETE", "/orders")

    def orders_since(self, after: str, status: str = "all") -> list[dict]:
        return self._call("GET", "/orders", params={"status": status, "after": after, "limit": 500,
                                                    "direction": "asc"}) or []

    def close_all(self) -> None:
        self._call("DELETE", "/positions", params={"cancel_orders": "true"})


# --- The signals (pure functions, tested on their own) -----------------------------------------

def gap_signals(opens: dict[str, float], prev: dict[str, float], gap: float = intraday.GAP) -> list[tuple[str, int]]:
    """Against opening gaps bigger than `gap`, biggest first: [(symbol, +1 buy / -1 short)]."""
    g = {s: opens[s] / prev[s] - 1 for s in opens if prev.get(s)}
    return [(s, -int(np.sign(x))) for s, x in sorted(g.items(), key=lambda kv: -abs(kv[1])) if abs(x) > gap]


def breakout_signals(high0: dict[str, float], low0: dict[str, float], last_close: dict[str, float],
                     done: set[str]) -> list[tuple[str, int]]:
    """A completed half hour closed above the first half hour's high (buy) or below its low (short)."""
    out = []
    for s, c in last_close.items():
        if s in done or s not in high0:
            continue
        if c > high0[s]:
            out.append((s, 1))
        elif c < low0[s]:
            out.append((s, -1))
    return out


def momentum_signals(close0: dict[str, float], prev: dict[str, float]) -> list[tuple[str, int]]:
    """Index funds in the direction of the first half hour (previous close to 10:00)."""
    return [(s, int(np.sign(close0[s] / prev[s] - 1))) for s in close0 if prev.get(s) and close0[s] != prev[s]]


def shares(price: float, budget: float) -> int:
    return max(math.floor(budget / price), 0) if price > 0 else 0


def pnl(orders: list[dict]) -> dict[str, dict]:
    """Per pattern from today's filled td- orders: {pattern: {"pnl": $, "trades": n, "symbols": [...]}}.

    Each symbol's entry order carries the pattern in its tag (td-<yyyymmdd>-<pattern>-<symbol>); its money
    in minus money out over the day is the trade's result. A symbol still held (its exit hasn't filled)
    has no result yet: it goes under "open" instead of counting its whole size as a gain or loss."""
    flows: dict[str, float] = {}
    net: dict[str, float] = {}
    pattern_of: dict[str, str] = {}
    for o in orders:
        if o.get("status") != "filled" or not o.get("filled_avg_price"):
            continue
        tag = str(o.get("client_order_id", ""))
        sym, qty, price = o["symbol"], float(o["filled_qty"]), float(o["filled_avg_price"])
        flows[sym] = flows.get(sym, 0.0) + (qty * price if o["side"] == "sell" else -qty * price)
        net[sym] = net.get(sym, 0.0) + (qty if o["side"] == "buy" else -qty)
        parts = tag.split("-")          # td-<yyyymmdd>-<pattern>-<symbol>, exits: td-<yyyymmdd>-x-<symbol>-exit
        if tag.startswith("td-") and len(parts) >= 4 and parts[2] != "x":
            pattern_of.setdefault(sym, parts[2])
    out: dict[str, dict] = {}
    for sym, money in flows.items():
        if abs(net[sym]) > 1e-9:   # not closed yet (its exit hasn't filled): no result, not the position's size
            out.setdefault("open", {"pnl": 0.0, "trades": 0, "symbols": []})["symbols"].append(sym)
            continue
        p = pattern_of.get(sym, "other")
        r = out.setdefault(p, {"pnl": 0.0, "trades": 0, "symbols": []})
        r["pnl"] += money
        r["trades"] += 1
        r["symbols"].append(f"{sym} {money:+,.0f}")
    return out


# --- The live session --------------------------------------------------------------------------

class Session:
    def __init__(self, broker: DayBroker, data: intraday.Data, symbols: list[str], today: dt.date,
                 log=print, sleep=time.sleep, now=lambda: dt.datetime.now(NY), share: dict | None = None,
                 paused: set | None = None):
        self.b, self.data, self.log, self.sleep, self.now = broker, data, log, sleep, now
        self.share, self.paused = share or {}, set(paused or ())
        self.today = today
        self.etfs = [s for s in symbols if s in intraday.ETFS]
        self.stocks = [s for s in symbols if s not in intraday.ETFS]
        self.utc0 = dt.datetime.combine(today, dt.time(9, 0), NY).astimezone(dt.timezone.utc)
        self.entries: list[dict] = []   # for the trade log: what was expected when each order went out
        self.halted = False             # the daily loss limit was hit: no more trades today
        self._assets: dict[str, dict] = {}
        self._last_guard = None

    def at(self, t: dt.time) -> dt.datetime:
        return dt.datetime.combine(self.today, t, NY)

    def wait_until(self, t: dt.time) -> None:
        while (left := (self.at(t) - self.now()).total_seconds()) > 0:
            self.sleep(min(left, 60))
            if self._last_guard is None or (self.now() - self._last_guard).total_seconds() >= GUARD_EVERY:
                self.guard()

    def day_change(self) -> float:
        a = self.b.account()
        last = float(a.get("last_equity") or a["equity"])
        return float(a["equity"]) / last - 1 if last else 0.0

    def guard(self) -> bool:
        """The daily loss limit: if the account is down DAILY_LOSS_LIMIT on the day, close everything and stop."""
        self._last_guard = self.now()
        if self.halted:
            return True
        change = self.day_change()
        if change <= -DAILY_LOSS_LIMIT:
            self.halted = True
            self.b.cancel_all()
            if self.b.positions():
                self.b.close_all()
            self.log(f"[day] daily loss limit: account {change:+.2%} today; everything closed, no more trades today")
        return self.halted

    def protect(self) -> int:
        """Give every open position without a protective stop one (STOP_LOSS from its entry price): covers
        positions opened before stops existed, or a stop that got lost. Returns how many were added."""
        orders = self.b.open_orders(nested=True)
        stopped = {x["symbol"] for o in orders for x in [o, *(o.get("legs") or [])]
                   if x.get("type") in ("stop", "stop_limit", "trailing_stop")}
        added = 0
        for p in self.b.positions():
            if p["symbol"] in stopped:
                continue
            qty, entry = float(p["qty"]), float(p.get("avg_entry_price") or 0)
            if not qty or not entry:
                continue
            long = qty > 0
            stop = entry * (1 - STOP_LOSS) if long else entry * (1 + STOP_LOSS)
            try:
                self.b.stop_order(p["symbol"], abs(int(qty)), "sell" if long else "buy", stop,
                                  f"td-{self.today:%Y%m%d}-x-{p['symbol']}-stop")
                added += 1
            except PaperError as e:
                self.log(f"[day] couldn't add a stop for {p['symbol']} ({str(e)[:80]})")
        if added:
            self.log(f"[day] added protective stops to {added} position(s) that had none")
        return added

    def can_trade(self, sym: str, side: int, last_trade: dt.datetime | None = None) -> str:
        """'' if the stock may be traded now, else why not (checked with the broker before each entry)."""
        if sym not in self._assets:
            try:
                self._assets[sym] = self.b.asset(sym)
            except PaperError:
                self._assets[sym] = {}
        a = self._assets[sym]
        if not a.get("tradable") or a.get("status", "active") != "active":
            return "not tradable"
        if side < 0 and not a.get("shortable"):
            return "can't be shorted"
        if side < 0 and not (a.get("easy_to_borrow") or a.get("borrow_status") == "easy_to_borrow"):
            return "hard to borrow"
        if last_trade is not None and (self.now() - last_trade).total_seconds() > STALE_MINUTES * 60:
            return "no recent trades (halted?)"
        return ""

    def last_trades(self, symbols: list[str]) -> dict[str, dt.datetime]:
        """When each stock last traded (the end of its latest 1-minute bar)."""
        bars = self._bars(symbols, "1Min", self.now() - dt.timedelta(minutes=STALE_MINUTES + 5))
        return {s: dt.datetime.fromisoformat(b[-1]["t"].replace("Z", "+00:00")) + dt.timedelta(minutes=1)
                for s, b in bars.items() if b}

    def today_orders(self) -> list[dict]:
        """Everything this account did today (the account is only used by this bot). Entries carry a
        td-<date> tag; a protective stop that fired is a separate order with the broker's own tag."""
        return self.b.orders_since(self.utc0.isoformat())

    def traded(self) -> set[str]:
        tag = f"td-{self.today:%Y%m%d}"
        return {o["symbol"] for o in self.today_orders() if str(o.get("client_order_id", "")).startswith(tag)}

    def budget(self) -> float:
        return float(self.b.account()["equity"]) * INVESTED / (STOCK_SLOTS + ETF_SLOTS)

    def enter(self, pattern: str, signals: list[tuple[str, int]], prices: dict[str, float], slots: int,
              strength: dict[str, float] | None = None) -> int:
        if pattern in self.paused:
            self.log(f"[day] {pattern}: paused (worse live than its history); no trades")
            return 0
        if self.guard():
            return 0
        done = self.traded()
        used = len(done & set(self.stocks if pattern != "momentum" else self.etfs))
        each, sent = self.budget() * size_factor(self.share, pattern), 0
        room = float(self.b.account()["equity"]) * INVESTED - sum(
            abs(float(p.get("market_value") or 0)) for p in self.b.positions())
        fresh = self.last_trades([s for s, _ in signals if s not in done])
        try:   # the bid and ask when each order goes out, for the trade log (what the spread cost)
            quotes = intraday.latest_quotes(self.data, [s for s, _ in signals if s not in done][:slots])
        except Exception:  # noqa: BLE001 - a missing quote never stops a trade
            quotes = {}
        stale_cutoff = self.now() - dt.timedelta(days=1)
        for sym, side in signals:
            if sym in done or used + sent >= slots or not prices.get(sym):
                continue
            qty = shares(prices[sym], min(each, room))
            if not qty:
                continue
            why = self.can_trade(sym, side, fresh.get(sym, stale_cutoff))
            if why:
                self.log(f"[day] {pattern}: {sym} skipped ({why})")
                continue
            room -= qty * prices[sym]
            stop = prices[sym] * (1 - STOP_LOSS) if side > 0 else prices[sym] * (1 + STOP_LOSS)
            try:
                self.b.order(sym, qty, "buy" if side > 0 else "sell", f"td-{self.today:%Y%m%d}-{pattern}-{sym}",
                             stop=stop)
                sent += 1
                self.entries.append({"date": self.today.isoformat(), "time": self.now().strftime("%H:%M"),
                                     "pattern": pattern, "symbol": sym, "side": side, "qty": qty,
                                     "expected": prices[sym], "signal": (strength or {}).get(sym),
                                     "bid": quotes.get(sym, {}).get("bid"), "ask": quotes.get(sym, {}).get("ask")})
                self.log(f"[day] {pattern}: {'buy' if side > 0 else 'short'} {qty} {sym} (~${qty * prices[sym]:,.0f})")
            except PaperError as e:   # e.g. a stock that can't be shorted today
                self.log(f"[day] {pattern}: {sym} refused ({str(e)[:80]})")
        return sent

    def _bars(self, symbols: list[str], timeframe: str, start: dt.datetime) -> dict[str, list[dict]]:
        df = self.data.bars(symbols, start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            self.now().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            timeframe=timeframe)
        out: dict[str, list[dict]] = {}
        for r in df.sort_values("t").itertuples(index=False):
            out.setdefault(r.symbol, []).append({"t": r.t, "o": r.open, "h": r.high, "l": r.low, "c": r.close})
        return out

    def prev_closes(self, symbols: list[str]) -> dict[str, float]:
        days = self._bars(symbols, "1Day", self.at(dt.time(0, 0)) - dt.timedelta(days=10))
        return {s: [b for b in bars if b["t"][:10] < f"{self.today}"][-1]["c"] for s, bars in days.items()
                if any(b["t"][:10] < f"{self.today}" for b in bars)}

    def first_half_hour(self, symbols: list[str]) -> dict[str, dict]:
        bars = self._bars(symbols, "30Min", self.at(dt.time(9, 30)))
        return {s: b[0] for s, b in bars.items() if b and b[0]["t"].endswith(("13:30:00Z", "14:30:00Z"))}

    def morning(self) -> None:
        if not self.guard():
            self.protect()
        self.wait_until(dt.time(9, 31))
        if self.now().time() <= GAP_LATEST:
            prev = self.prev_closes(self.stocks)
            first = self._bars(self.stocks, "1Min", self.at(dt.time(9, 30)))
            opens = {s: b[0]["o"] for s, b in first.items() if b}
            last = {s: b[-1]["c"] for s, b in first.items() if b}
            self.enter("gap", gap_signals(opens, prev), last, STOCK_SLOTS,
                       {s: opens[s] / prev[s] - 1 for s in opens if prev.get(s)})
        else:
            self.log("[day] started too late for the gap fade today")
        self.breakouts(MORNING_CHECKS)

    def breakouts(self, checks: list[dt.time]) -> None:
        for t in checks:
            if self.now() > self.at(t) + dt.timedelta(minutes=20):
                continue                                         # missed (late start): skip, don't chase
            self.wait_until((dt.datetime.combine(self.today, t) + dt.timedelta(seconds=45)).time())
            first = self.first_half_hour(self.stocks)
            bars = self._bars(self.stocks, "30Min", self.at(dt.time(9, 30)))
            done_bars = {s: [x for x in b if self._end(x) <= self.now()] for s, b in bars.items()}
            last = {s: b[-1]["c"] for s, b in done_bars.items() if len(b) >= 2}
            high0, low0 = {s: b["h"] for s, b in first.items()}, {s: b["l"] for s, b in first.items()}
            sig = breakout_signals(high0, low0, last, self.traded())
            beyond = {s: (last[s] / high0[s] - 1) if side > 0 else (last[s] / low0[s] - 1) for s, side in sig}
            self.enter("breakout", sig, last, STOCK_SLOTS, beyond)

    def _end(self, bar: dict) -> dt.datetime:
        return dt.datetime.fromisoformat(bar["t"].replace("Z", "+00:00")) + dt.timedelta(minutes=30)

    def afternoon(self) -> None:
        if not self.guard():
            self.protect()
        self.breakouts(AFTERNOON_CHECKS[:-1])
        self.wait_until(dt.time(15, 30, 45))
        prev = self.prev_closes(self.etfs)
        first = self.first_half_hour(self.etfs)
        now_px = {s: b[-1]["c"] for s, b in self._bars(self.etfs, "1Min", self.at(dt.time(15, 25))).items() if b}
        close0 = {s: b["c"] for s, b in first.items()}
        self.enter("momentum", momentum_signals(close0, prev), now_px, ETF_SLOTS,
                   {s: close0[s] / prev[s] - 1 for s in close0 if prev.get(s)})
        self.breakouts(AFTERNOON_CHECKS[-1:])
        self.flatten()

    def flatten(self) -> None:
        """Out at the closing price: market-on-close orders at 15:45, then anything left at 15:58.
        The protective stops are cancelled first (they hold the shares the closing orders need)."""
        self.wait_until(MOC_AT)
        self.b.cancel_all()
        self.wait_for(lambda: not self.b.open_orders(), seconds=60)    # cancelling takes a moment
        for p in self.b.positions():
            qty = abs(int(float(p["qty"])))
            side = "sell" if float(p["qty"]) > 0 else "buy"
            tag = f"td-{self.today:%Y%m%d}-x-{p['symbol']}-exit"
            for attempt in (1, 2):
                try:
                    self.b.order(p["symbol"], qty, side, tag, tif="cls")
                    break
                except PaperError as e:
                    if attempt == 2:
                        self.log(f"[day] closing order for {p['symbol']} refused ({str(e)[:80]}); closed at 15:58 instead")
                    else:
                        self.sleep(5)
        self.wait_until(FLAT_AT)
        # Positions with a closing order are still open until it fills at 16:00: leave those alone.
        closing = {o["symbol"] for o in self.b.open_orders() if o.get("time_in_force") == "cls"}
        left = [p["symbol"] for p in self.b.positions() if p["symbol"] not in closing]
        for sym in left:
            for attempt in (1, 2):
                try:
                    self.b.close(sym)
                    break
                except PaperError as e:
                    if attempt == 2:
                        self.log(f"[day] couldn't close {sym} at 15:58 ({str(e)[:80]}); the safety net closes it")
                    else:
                        self.sleep(5)
        if left:
            self.log(f"[day] closed what had no closing order at 15:58: {', '.join(left)}")
            self.sleep(20)
            still = [p["symbol"] for p in self.b.positions() if p["symbol"] not in closing]
            if still:
                self.log(f"[day] ⚠️ still open after closing at 15:58: {', '.join(still)}")

    def wait_for(self, done, seconds: float, step: float = 2) -> bool:
        """Check `done()` every `step` seconds for up to `seconds`; True once it holds."""
        waited = 0.0
        while not done():
            if waited >= seconds:
                return False
            self.sleep(step)
            waited += step
        return True

    def settle(self) -> None:
        """After the close: wait until the closing orders have filled, so the report sees every exit."""
        self.wait_until(SETTLE_AT)
        if not self.wait_for(lambda: not self.b.positions() and not self.b.open_orders(), seconds=600, step=15):
            self.log("[day] some closing orders still hadn't filled 10 minutes after the close")


# --- Learning from the live trades ----------------------------------------------------------------

PATTERN_TEST = {"gap": "gap_fade", "breakout": "opening_range", "momentum": "intraday_momentum"}
SHORT_NAME = {"gap": "gap fade", "breakout": "breakouts", "momentum": "last half hour"}
PATTERN_NAME = {"gap": "Fade big opening gaps", "breakout": "Breakout from the first half hour",
                "momentum": "Market's last half hour follows its first"}
MIN_SHARE, MAX_SHARE = 0.15, 0.60     # each pattern's share of the money stays between these
PRIOR_TRADES = 100                    # the 10-year test counts like this many live trades
SHIFT_AFTER, PAUSE_AFTER, COST_AFTER = 20, 40, 50   # live trades needed before each kind of change
DEFAULT_SD = 0.01                     # a trade's typical spread of results, if the test didn't say


def compare(log: list[dict], tested: dict | None) -> dict[str, dict]:
    """Per pattern: live trades vs the 10-year test's forecast. z: how many 'luck widths' apart."""
    out = {}
    for p, key in PATTERN_TEST.items():
        rets = [r["ret"] for r in log if r.get("pattern") == p and r.get("ret") is not None]
        t = (tested or {}).get(key) or {}
        forecast = t.get("per_trade") if t.get("days") else None
        sd = t.get("sd_trade") if t.get("sd_trade") == t.get("sd_trade") and t.get("sd_trade") else (
            float(np.std(rets, ddof=1)) if len(rets) > 2 else DEFAULT_SD)
        live = float(np.mean(rets)) if rets else None
        z = (live - forecast) / (sd / np.sqrt(len(rets))) if rets and forecast is not None and sd else None
        out[p] = {"n": len(rets), "live": live, "forecast": forecast, "sd": sd, "z": z}
    return out


def verdict_vs_test(c: dict) -> str:
    if not c["n"]:
        return "no live trades yet"
    if c["forecast"] is None:
        return "no 10-year test result yet"
    if c["n"] < 10:
        return "too early to tell"
    if c["z"] < -2:
        return "worse than its history beyond what luck explains"
    if c["z"] > 2:
        return "better than its history (could still be luck)"
    return "within the normal range of its history"


def measured_cost(log: list[dict]) -> float | None:
    """The real cost of getting in, per side, once there are enough trades (never below zero)."""
    slip = [r["slippage"] for r in log if r.get("slippage") is not None]
    return max(float(np.mean(slip)), 0.0) if len(slip) >= COST_AFTER else None


def allocate(cmp: dict[str, dict], cost: float | None) -> tuple[dict[str, float], set[str]]:
    """Each pattern's share of the money, and the paused ones.

    The expected gain per trade is the 10-year forecast blended with the live average (the test
    weighs like PRIOR_TRADES trades, so a few days barely move it), minus the measured cost if
    known. Shares follow the expected gains, only for patterns with SHIFT_AFTER+ live trades, and
    always stay within MIN_SHARE..MAX_SHARE. A pattern clearly worse live than its history
    (PAUSE_AFTER+ trades, z below -2) is paused. A great streak never pauses or boosts beyond the cap."""
    paused = {p for p, c in cmp.items() if c["n"] >= PAUSE_AFTER and c["z"] is not None and c["z"] < -2}
    active = [p for p in PATTERN_TEST if p not in paused]
    if not active:
        return {}, paused
    if not any(cmp[p]["n"] >= SHIFT_AFTER for p in active):
        return {p: 1 / len(active) for p in active}, paused
    edge = {}
    for p in active:
        c = cmp[p]
        prior = c["forecast"] if c["forecast"] is not None else 0.0
        blended = (PRIOR_TRADES * prior + c["n"] * (c["live"] or 0.0)) / (PRIOR_TRADES + c["n"]) \
            if c["n"] >= SHIFT_AFTER else prior
        edge[p] = blended - (2 * cost if cost is not None else 0.0)
    lo = min(edge.values())
    raw = {p: e - lo + 1e-4 for p, e in edge.items()}            # the weakest gets the least, never nothing
    return bounded(raw), paused


def bounded(raw: dict[str, float], lo: float = MIN_SHARE, hi: float = MAX_SHARE) -> dict[str, float]:
    """Shares in proportion to `raw`, adding up to 1, none below `lo` or above `hi`: a share that would
    cross a limit is held at it, and the rest is split among the others in the same proportions."""
    lo, hi = min(lo, 1 / len(raw)), max(hi, 1 / len(raw))
    fixed: dict[str, float] = {}
    while True:
        free = {p: v for p, v in raw.items() if p not in fixed}
        left = 1 - sum(fixed.values())
        share = {p: left * v / sum(free.values()) for p, v in free.items()} if free else {}
        over = {p for p, v in share.items() if v > hi + 1e-12}
        under = {p for p, v in share.items() if v < lo - 1e-12}
        if not over and not under:
            return {**fixed, **share}
        fixed.update({p: hi for p in over} if over else {p: lo for p in under})


def size_factor(share: dict[str, float], pattern: str) -> float:
    """Position size vs an equal split among the active patterns (1.0 = the normal 4.9% of the account).
    A paused pattern's money isn't handed to the others: their sizes stay as if it were still there."""
    return share.get(pattern, 0.0) * len(share) if share else 1.0


LOG_FIELDS = ["date", "time", "pattern", "symbol", "side", "qty", "signal", "expected", "bid", "ask", "spread",
              "entry", "exit", "slippage", "ret", "pnl"]


def trade_log(orders: list[dict], entries: list[dict], today: dt.date) -> list[dict]:
    """One row per trade today: what was expected vs what happened.

    slippage: how much worse the entry price was than the price the bot saw when it decided (a positive
    number is a cost: paid more on a buy, got less on a short). ret: the trade's return, entry to exit."""
    by_sym: dict[str, dict] = {}
    for o in orders:
        tag = str(o.get("client_order_id", ""))
        if o.get("status") != "filled" or not o.get("filled_avg_price"):
            continue
        r = by_sym.setdefault(o["symbol"], {})
        entry = tag.startswith("td-") and not tag.endswith("-exit") and tag.split("-")[2] != "x"
        r["entry" if entry else "exit"] = o   # a fired protective stop has the broker's own tag: an exit
    expected = {e["symbol"]: e for e in entries if e["date"] == today.isoformat()}
    rows = []
    for sym, r in sorted(by_sym.items()):
        if "entry" not in r:
            continue
        e, o = expected.get(sym, {}), r["entry"]
        side = 1 if o["side"] == "buy" else -1
        entry = float(o["filled_avg_price"])
        exit_ = float(r["exit"]["filled_avg_price"]) if "exit" in r else None
        qty = float(o["filled_qty"])
        exp = e.get("expected")
        bid, ask = e.get("bid"), e.get("ask")
        spread = (ask - bid) / ((ask + bid) / 2) if bid and ask and ask >= bid else None
        rows.append({"bid": bid, "ask": ask, "spread": spread,"date": today.isoformat(), "time": e.get("time") or str(o.get("filled_at", ""))[11:16],
                     "pattern": str(o["client_order_id"]).split("-")[2], "symbol": sym, "side": side, "qty": qty,
                     "signal": e.get("signal"), "expected": exp, "entry": entry, "exit": exit_,
                     "slippage": side * (entry / exp - 1) if exp else None,
                     "ret": side * (exit_ / entry - 1) if exit_ else None,
                     "pnl": side * qty * (exit_ - entry) if exit_ else None})
    return rows


def learned(log: list[dict]) -> str:
    """What the live trades say so far, in one line."""
    done = [r for r in log if r.get("ret") is not None]
    if not done:
        return ""
    rets = [r["ret"] for r in done]
    parts = [f"average trade **{np.mean(rets):+.2%}**", f"right {np.mean([x > 0 for x in rets]):.0%} of the time"]
    slip = [r["slippage"] for r in log if r.get("slippage") is not None]
    if slip:
        parts.insert(0, f"entry prices **{abs(np.mean(slip)):.3%} {'worse' if np.mean(slip) >= 0 else 'better'}** "
                        f"than when the bot decided (the 10-year test assumes {intraday.COST['stock'] / 2:.3%} a side)")
    return f"**Learned so far ({len(done)} trades):** " + " · ".join(parts)


def write_csv(log: list[dict], path: str) -> None:
    import csv

    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        w.writeheader()
        w.writerows({k: r.get(k) for k in LOG_FIELDS} for r in log)


def safety_net(broker: DayBroker, now: dt.datetime, log=print) -> dict | None:
    """Independent of the trading jobs: close anything that shouldn't still be open. Returns an alert or None.

    - before the close (from 15:50 New York): positions without a pending closing order (the main
      job didn't place them) are closed now
    - after the close or before the open: any position at all was left overnight; it's closed (at the
      next open if the market is shut) and you're told."""
    from trend_bot.alerts import RED

    positions = broker.positions()
    if not positions:
        return None
    t = now.time()
    in_session = dt.time(9, 30) <= t < dt.time(16, 0)
    closing = {o["symbol"] for o in broker.open_orders() if o.get("time_in_force") == "cls"}
    loose = [p for p in positions if not (in_session and p["symbol"] in closing)]
    if not loose:
        return None
    broker.cancel_all()
    broker.close_all()
    names = ", ".join(p["symbol"] for p in loose)
    when = ("before the close the main job hadn't closed" if in_session else
            "these were still open outside market hours (left overnight)")
    log(f"[day] safety net: {when}: {names}; closing everything")
    return {"title": "🛟 Day trading: safety net closed positions", "color": RED,
            "description": f"**What this means:** a safety check found day-trading positions that should already "
                           f"be closed ({when}): {names}. All were closed"
                           + (" (at the next open, the market is shut)." if not in_session else ".")
                           + " Fake money; the trading job's log on the Actions tab shows what went wrong."}


def report(orders: list[dict], equity: float, state: dict, today: dt.date, tested: dict | None = None,
           last_equity: float | None = None) -> tuple[dict, dict]:
    """Add today's results to the running state (and the trade log); returns (state, Discord embed).

    last_equity: the account's value at the previous close (the broker's own number). The day's change
    in the account is the truth the trade-by-trade results must add up to; a gap is flagged."""
    from trend_bot.alerts import GOLD, GREEN, RED, lead

    rows = trade_log(orders, state.pop("pending", []), today)
    state["log"] = [r for r in state.get("log", []) if r["date"] != today.isoformat()] + rows
    day = pnl(orders)
    still_open = day.pop("open", {}).get("symbols", [])
    first_day = not state.get("days") or set(state["days"]) == {today.isoformat()}
    if first_day:   # (re)set on the first day, so a redone first day fixes the starting value too
        state["start_equity"] = last_equity if last_equity else equity - sum(r["pnl"] for r in day.values())
    state.setdefault("start_equity", equity - sum(r["pnl"] for r in day.values()))
    state.setdefault("days", {})[today.isoformat()] = {p: {"pnl": r["pnl"], "trades": r["trades"]} for p, r in day.items()}
    totals: dict[str, dict] = {}
    for d in state["days"].values():
        for p, r in d.items():
            t = totals.setdefault(p, {"pnl": 0.0, "trades": 0})
            t["pnl"] += r["pnl"]
            t["trades"] += r["trades"]
    names = {"gap": ("gap_fade", "Fade big opening gaps"), "breakout": ("opening_range", "Breakout from the first half hour"),
             "momentum": ("intraday_momentum", "Market's last half hour follows its first"), "other": ("", "Other")}
    today_total = sum(r["pnl"] for r in day.values())
    lines = [lead("the bot's day-trading practice account (fake money) traded today and closed everything by the "
                  "close. A test of whether intraday patterns make money; nothing real was bought or sold."), ""]
    if not day:
        lines.append("No trades today (no pattern triggered).")
    for p, r in sorted(day.items()):
        key, name = names.get(p, ("", p))
        t = totals.get(p, {})
        test = (tested or {}).get(key)
        tline = (f" · 10-year test: {test['per_trade']:+.2%} a trade after costs, "
                 f"{'passed' if intraday.verdict(test).startswith('PASSES') else 'did not pass'}") if test and test.get("days") else ""
        lines.append(f"**{name}**: today **${r['pnl']:+,.0f}** ({r['trades']} trade{'s' if r['trades'] != 1 else ''}: "
                     f"{', '.join(r['symbols'][:8])}); since the start ${t.get('pnl', 0):+,.0f} over "
                     f"{t.get('trades', 0)} trades{tline}")
    start = state["start_equity"]
    lesson = learned(state["log"])
    if lesson:
        lines += ["", lesson]
    cmp = compare(state["log"], tested)
    cost = measured_cost(state["log"])
    share, paused = allocate(cmp, cost)
    newly = paused - set(state.get("paused", []))
    state.update(share=share, paused=sorted(paused), measured_cost=cost)
    lines += ["", "**Forecast vs actual** (10-year test vs live, per trade after costs):"]
    for p, c in cmp.items():
        f = f"{c['forecast']:+.2%}" if c["forecast"] is not None else "–"
        lv = f"{c['live']:+.2%}" if c["live"] is not None else "–"
        lines.append(f"• {PATTERN_NAME[p]}: tested {f}, live {lv} over {c['n']} trade{'s' if c['n'] != 1 else ''}: "
                     f"{verdict_vs_test(c)}")
    if cost is not None:
        lines.append(f"Measured cost of getting in: {cost:.3%} a side (from {COST_AFTER}+ trades; the test assumed "
                     f"{intraday.COST['stock'] / 2:.3%}).")
    if share:
        lines.append("Tomorrow's money per pattern: " + ", ".join(
            f"{SHORT_NAME[p]} {v:.0%}" for p, v in share.items())
                     + ("" if any(c["n"] >= SHIFT_AFTER for c in cmp.values())
                        else f" (equal until a pattern has {SHIFT_AFTER} live trades)"))
    for p in sorted(newly):
        lines.append(f"⏸️ **{PATTERN_NAME[p]} is paused**: after {cmp[p]['n']} live trades it's doing clearly worse "
                     "than its 10-year history. It resumes on its own if its record comes back within range.")
    if still_open:
        lines += ["", f"⚠️ Not closed yet when this was written (no result counted for them): {', '.join(still_open)}"]
    if last_equity and abs((equity - last_equity) - today_total) > max(50.0, 0.0005 * equity):
        lines += ["", f"⚠️ The trades add up to ${today_total:+,.0f} but the account changed "
                      f"${equity - last_equity:+,.0f} today: some fill is missing from the records."]
    lines += ["", f"**Today: ${today_total:+,.0f}** · account ${equity:,.0f} "
                  f"(**{equity / start - 1:+.2%}** since {min(state['days'])}, {len(state['days'])} trading day"
                  f"{'s' if len(state['days']) != 1 else ''})"]
    color = GREEN if today_total > 0 else RED if today_total < 0 else GOLD
    return state, {"title": f"🧪 Day trading: {today:%a %b %d}", "description": "\n".join(lines)[:4000], "color": color}


def load_state(path: str) -> dict:
    try:
        return json.loads(open(path).read())
    except (OSError, ValueError):
        return {}
