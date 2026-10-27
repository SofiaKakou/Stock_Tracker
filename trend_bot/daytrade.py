"""Day-trading demo account: the intraday patterns from `study intraday`, traded live with fake money
on a second Alpaca paper account (separate from the monthly portfolio's account).

Each weekday, two scheduled jobs share the day (a GitHub job may run at most 6 hours):
- morning (from 9:30 New York time): gap fade at the open, then breakout checks at 10:30 ... 12:00
- afternoon (from 12:30): breakout checks at 12:30 ... 15:30, the market's last half hour at 15:30,
  everything closed with market orders at 15:50 (checked and retried; a last sweep at 15:58),
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
import re
import time
from zoneinfo import ZoneInfo

import numpy as np

from trend_bot import intraday
from trend_bot.paper import Paper, PaperError

KEY_ENV, SECRET_ENV = "ALPACA_DAY_KEY_ID", "ALPACA_DAY_SECRET_KEY"
NY = ZoneInfo("America/New_York")
STOCK_SLOTS, ETF_SLOTS = 34, 3   # positions at once: up to 34 stocks (gap fade + breakouts) and the 3 funds
SIZE_SLOTS = 20       # each position is 1/20 of the account (about $4,900 on $100,000)
BUYING_POWER = 2.0    # all positions together up to 2x the account: intraday buying power, never overnight
INVESTED = 0.98
GAP_LATEST = dt.time(10, 15)          # a late start still fades the gap until 10:15, not after
MORNING_CHECKS = [dt.time(10, 30), dt.time(11, 0), dt.time(11, 30), dt.time(12, 0)]
AFTERNOON_CHECKS = [dt.time(12, 30), dt.time(13, 0), dt.time(13, 30), dt.time(14, 0), dt.time(14, 30),
                    dt.time(15, 0), dt.time(15, 30)]
CLOSE_AT, FLAT_AT, SETTLE_AT = dt.time(15, 50), dt.time(15, 58), dt.time(16, 1)
STOP_LOSS = 0.05        # every position has a stop at the broker: out if it moves 5% against us
LIMIT_SLACK = 0.002     # entries are limit orders at most 0.2% past the quote (a market order can fill far worse)
STALE_ENTRY_MINUTES = 5  # an entry that hasn't filled in 5 minutes is cancelled (the moment has passed)
NET_LIMIT = 0.5          # longs minus shorts at most 50% of the account: not one big bet on the market's direction
# Short the big gap-ups (tested only; the only idea that passed the 20-year swing study after costs):
# any stock in the market that opens 20%+ above yesterday's close (price $5+, $5M+ traded a day),
# sold short at the open if the broker can lend it, a 15% stop above, bought back by the close.
GAPUP_MIN, GAPUP_MAX, GAPUP_STOP, GAPUP_SLOTS = 0.20, 0.60, 0.15, 3
GAPUP_MIN_PRICE, GAPUP_MIN_DOLLAR_VOL = 5.0, 5e6
# The swing study's result (20 years, after costs; early/late: per trade before / from 2016), in the
# shape of the day-trading study's results so the scorecard and the learning loop treat it the same.
GAPUP_TEST = {"trades": 2662, "days": 2662, "per_trade": 0.0035, "sd_trade": 0.06, "early": 0.0145,
              "late": 0.0024, "yearly": 0.0035 * 2662 / 20, "t": 3.1, "luck": 0.0}
# Short yesterday's big losers (tested only; passed the 20-year swing study, "big news days"): a stock
# that closed 15%+ down yesterday on 3x+ its usual volume (big bad news) tends to keep falling. Sold
# short at the open (if the broker can lend it), a 25% stop above, bought back by the close.
LOSER_MIN, LOSER_MAX, LOSER_VOLUME_X, LOSER_STOP, LOSER_SLOTS = 0.15, 0.60, 3.0, 0.25, 3
# With its stop (the swing study's "as real trades" lines): a 15% stop is hit by the jumps crashed stocks
# make on the way down (+0.31% a trade, could be luck); 25% passes (+0.39%, t = 2.2).
LOSER_TEST = {"trades": 6787, "days": 6787, "per_trade": 0.0039, "sd_trade": 0.06, "early": 0.0016,
              "late": 0.0045, "yearly": 0.0039 * 6787 / 20, "t": 2.2, "luck": 0.0}
LOSER_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
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
              stop: float | None = None, limit: float | None = None) -> dict:
        body = {"symbol": symbol, "qty": str(qty), "side": side, "type": "limit" if limit else "market",
                "time_in_force": tif, "client_order_id": tag}
        if limit:   # pay at most this (a buy) or get at least this (a sale): caps a bad fill
            body["limit_price"] = f"{limit:.2f}"
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

    def list_assets(self) -> list[dict]:
        """Every active US stock the broker lists."""
        return self._call("GET", "/assets", params={"status": "active", "asset_class": "us_equity"}) or []

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


def gapup_signals(opens: dict[str, float], prev: dict[str, float], dollar_vol: dict[str, float]) -> list[tuple[str, int]]:
    """Stocks that opened GAPUP_MIN-GAPUP_MAX above yesterday's close (a bigger jump is usually a data
    error or a takeover), price and trading big enough: [(symbol, -1)] (short), biggest jump first."""
    g = {s: opens[s] / prev[s] - 1 for s in opens if prev.get(s)}
    ok = [s for s, x in g.items() if GAPUP_MIN <= x < GAPUP_MAX and prev[s] >= GAPUP_MIN_PRICE
          and dollar_vol.get(s, 0) >= GAPUP_MIN_DOLLAR_VOL]
    return [(s, -1) for s in sorted(ok, key=lambda s: -g[s])]


FUND_WORDS = re.compile(r"\b(ETF|ETN|Fund|iShares|SPDR|ProShares|Direxion|Leveraged|Inverse|[23]X|Trust, Series)\b", re.I)


def is_fund(name: str) -> bool:
    """A fund rather than a company, from its name (the broker's list doesn't say): the losers short is about
    one company's bad news, not a whole market or country falling (Oct 27: EWZ, the Brazil fund)."""
    return bool(FUND_WORDS.search(name or ""))


def loser_signals(days: dict[str, list[dict]], today: dt.date) -> list[tuple[str, int, float]]:
    """From daily bars ({symbol: [{"t", "c", "v"}...]}): stocks whose last day before `today` closed
    LOSER_MIN-LOSER_MAX down on LOSER_VOLUME_X+ their usual volume, priced $5+ and trading $5M+ a day
    before it: [(symbol, -1, the day's move)], biggest fall first."""
    out = []
    for s, bars in days.items():
        b = [x for x in bars if x["t"][:10] < f"{today}"]
        if len(b) < 22:
            continue
        news, before = b[-1], b[-21:-1]
        move = news["c"] / before[-1]["c"] - 1
        usual_vol = float(np.mean([x["v"] for x in before]))
        dvol = float(np.mean([x["c"] * x["v"] for x in before]))
        if (-LOSER_MAX < move <= -LOSER_MIN and usual_vol > 0 and news["v"] >= LOSER_VOLUME_X * usual_vol
                and before[-1]["c"] >= GAPUP_MIN_PRICE and dvol >= GAPUP_MIN_DOLLAR_VOL):
            out.append((s, -1, move))
    return sorted(out, key=lambda x: x[2])


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
        flat = [x for o in orders for x in [o, *(o.get("legs") or [])]]
        stopped = {x["symbol"] for x in flat if x.get("type") in ("stop", "stop_limit", "trailing_stop")}
        added = 0
        for p in self.b.positions():
            qty, entry = float(p["qty"]), float(p.get("avg_entry_price") or 0)
            closing = "sell" if qty > 0 else "buy"
            if p["symbol"] in stopped or any(x["symbol"] == p["symbol"] and x.get("side") == closing for x in flat):
                continue                    # protected, or already being closed (e.g. left over the weekend)
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

    def cancel_stale_entries(self) -> int:
        """Entry orders still unfilled after STALE_ENTRY_MINUTES (a limit the price ran away from): cancelled."""
        tag, n = f"td-{self.today:%Y%m%d}-", 0
        for o in self.b.open_orders():
            cid, when = str(o.get("client_order_id", "")), o.get("submitted_at")
            if not cid.startswith(tag) or "-x-" in cid or not when:
                continue
            age = self.now() - dt.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
            if age.total_seconds() > STALE_ENTRY_MINUTES * 60:
                try:
                    self.b.cancel(o["id"])
                    n += 1
                except PaperError:
                    pass
        if n:
            self.log(f"[day] cancelled {n} entry order(s) that didn't fill within {STALE_ENTRY_MINUTES} minutes")
        return n

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
        return float(self.b.account()["equity"]) * INVESTED / SIZE_SLOTS

    def enter(self, pattern: str, signals: list[tuple[str, int]], prices: dict[str, float], slots: int,
              strength: dict[str, float] | None = None) -> int:
        if pattern in self.paused:
            self.log(f"[day] {pattern}: paused (worse live than its history); no trades")
            return 0
        if not signals:
            return 0
        if self.guard():
            return 0
        self.cancel_stale_entries()
        done = self.traded()
        if pattern in ("gapup", "loser"):    # any stock in the market: count this pattern's own trades
            used = sum(1 for o in self.today_orders()
                       if str(o.get("client_order_id", "")).startswith(f"td-{self.today:%Y%m%d}-{pattern}-"))
        else:
            used = len(done & set(self.stocks if pattern != "momentum" else self.etfs))
        each, sent = self.budget() * size_factor(self.share, pattern), 0
        account = self.b.account()
        positions = self.b.positions()
        room = float(account["equity"]) * INVESTED * BUYING_POWER - sum(
            abs(float(p.get("market_value") or 0)) for p in positions)
        net = sum(float(p.get("market_value") or 0) for p in positions)      # shorts count negative
        net_cap = float(account["equity"]) * NET_LIMIT
        if account.get("daytrading_buying_power"):        # never more than the broker allows
            room = min(room, float(account["daytrading_buying_power"]) * INVESTED)
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
            if abs(net + side * qty * prices[sym]) > net_cap and abs(net + side * qty * prices[sym]) > abs(net):
                self.log(f"[day] {pattern}: {sym} skipped (the account would be too one-sided: "
                         f"{'long' if net > 0 else 'short'} ${abs(net):,.0f})")
                continue
            q = quotes.get(sym, {})
            bid, ask = q.get("bid"), q.get("ask")
            quoted = bid and ask and ask >= bid
            # The price at the moment of the order: the quote's middle (the half-hour bar can be a minute old).
            expected = (bid + ask) / 2 if quoted else prices[sym]
            limit = (ask * (1 + LIMIT_SLACK) if side > 0 else bid * (1 - LIMIT_SLACK)) if quoted else None
            loss = {"gapup": GAPUP_STOP, "loser": LOSER_STOP}.get(pattern, STOP_LOSS)
            stop = expected * (1 - loss) if side > 0 else expected * (1 + loss)
            try:
                self.b.order(sym, qty, "buy" if side > 0 else "sell", f"td-{self.today:%Y%m%d}-{pattern}-{sym}",
                             stop=stop, limit=limit)
                sent += 1
                room -= qty * prices[sym]
                net += side * qty * prices[sym]
                self.entries.append({"date": self.today.isoformat(), "time": self.now().strftime("%H:%M"),
                                     "pattern": pattern, "symbol": sym, "side": side, "qty": qty,
                                     "expected": expected, "signal": (strength or {}).get(sym),
                                     "bid": bid, "ask": ask})
                self.log(f"[day] {pattern}: {'buy' if side > 0 else 'short'} {qty} {sym} (~${qty * prices[sym]:,.0f})")
            except PaperError as e:   # e.g. a stock that can't be shorted today
                self.log(f"[day] {pattern}: {sym} refused ({str(e)[:80]})")
        return sent

    def _bars(self, symbols: list[str], timeframe: str, start: dt.datetime) -> dict[str, list[dict]]:
        if not symbols:     # Alpaca refuses a request for no symbols
            return {}
        df = self.data.bars(symbols, start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            self.now().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            timeframe=timeframe)
        out: dict[str, list[dict]] = {}
        for r in df.sort_values("t").itertuples(index=False):
            out.setdefault(r.symbol, []).append({"t": r.t, "o": r.open, "h": r.high, "l": r.low, "c": r.close,
                                                 "v": r.volume})
        return out

    def prev_closes(self, symbols: list[str]) -> dict[str, float]:
        days = self._bars(symbols, "1Day", self.at(dt.time(0, 0)) - dt.timedelta(days=10))
        return {s: [b for b in bars if b["t"][:10] < f"{self.today}"][-1]["c"] for s, bars in days.items()
                if any(b["t"][:10] < f"{self.today}" for b in bars)}

    def gapups(self) -> int:
        """Short the market's big gap-ups (see GAPUP_*). Candidates from the market's top risers."""
        try:
            cands = [s for s in intraday.movers(self.data) if s not in intraday.ETFS][:50]
        except Exception as e:  # noqa: BLE001 - no screener today: the pattern sits out
            self.log(f"[day] gapup: couldn't get today's top risers ({str(e)[:80]})")
            return 0
        if not cands:
            return 0
        days = self._bars(cands, "1Day", self.at(dt.time(0, 0)) - dt.timedelta(days=35))
        before = {s: [b for b in bars if b["t"][:10] < f"{self.today}"] for s, bars in days.items()}
        prev = {s: b[-1]["c"] for s, b in before.items() if b}
        dvol = {s: float(np.mean([x["c"] * x["v"] for x in b[-20:]])) for s, b in before.items() if len(b) >= 20}
        first = self._bars(cands, "1Min", self.at(dt.time(9, 30)))
        opens = {s: b[0]["o"] for s, b in first.items() if b}
        last = {s: b[-1]["c"] for s, b in first.items() if b}
        sig = gapup_signals(opens, prev, dvol)
        return self.enter("gapup", sig, last, GAPUP_SLOTS, {s: opens[s] / prev[s] - 1 for s, _ in sig})

    def find_losers(self, chunk: int = 300) -> list[tuple[str, int, float]]:
        """Yesterday's big losers in the whole market (see LOSER_*), from 35 days of daily bars."""
        try:
            names = sorted(a["symbol"] for a in self.b.list_assets()
                           if a.get("tradable") and a.get("shortable") and a.get("easy_to_borrow")
                           and a.get("exchange") in LOSER_EXCHANGES and a["symbol"].isalpha()
                           and a["symbol"] not in intraday.ETFS and not is_fund(a.get("name", "")))
            start = self.at(dt.time(0, 0)) - dt.timedelta(days=35)
            days: dict[str, list[dict]] = {}
            for i in range(0, len(names), chunk):
                days.update(self._bars(names[i:i + chunk], "1Day", start))
        except Exception as e:  # noqa: BLE001 - no list today: the pattern sits out
            self.log(f"[day] loser: couldn't scan the market ({str(e)[:80]})")
            return []
        found = loser_signals(days, self.today)
        self.log(f"[day] loser: {len(names):,} stocks scanned, {len(found)} closed {LOSER_MIN:.0%}+ down on big volume"
                 + (f": {', '.join(s for s, _, _ in found[:10])}" if found else ""))
        return found

    def losers(self, found: list[tuple[str, int, float]]) -> int:
        """Short yesterday's big losers at the open."""
        if not found:
            return 0
        syms = [s for s, _, _ in found][:20]
        first = self._bars(syms, "1Min", self.at(dt.time(9, 30)))
        last = {s: b[-1]["c"] for s, b in first.items() if b}
        return self.enter("loser", [(s, side) for s, side, _ in found if s in syms], last, LOSER_SLOTS,
                          {s: m for s, _, m in found})

    def first_half_hour(self, symbols: list[str]) -> dict[str, dict]:
        bars = self._bars(symbols, "30Min", self.at(dt.time(9, 30)))
        return {s: b[0] for s, b in bars.items() if b and b[0]["t"].endswith(("13:30:00Z", "14:30:00Z"))}

    def morning(self) -> None:
        if not self.guard():
            self.protect()
        found = self.find_losers() if self.now().time() <= GAP_LATEST else []   # scanned before the open
        self.wait_until(dt.time(9, 31))
        if self.now().time() <= GAP_LATEST:
            prev = self.prev_closes(self.stocks)
            first = self._bars(self.stocks, "1Min", self.at(dt.time(9, 30)))
            opens = {s: b[0]["o"] for s, b in first.items() if b}
            last = {s: b[-1]["c"] for s, b in first.items() if b}
            self.enter("gap", gap_signals(opens, prev), last, STOCK_SLOTS,
                       {s: opens[s] / prev[s] - 1 for s in opens if prev.get(s)})
            for name, part in (("gapup", self.gapups), ("loser", lambda: self.losers(found))):
                try:        # one pattern's problem mustn't stop the others (or the rest of the day)
                    part()
                except Exception as e:  # noqa: BLE001
                    self.log(f"[day] ⚠️ {name}: stopped with an error, the other patterns carry on ({str(e)[:120]})")
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
        """Out before the close: every position closed with a market order at 15:50, checked, and closed
        again if it's still there; a last sweep at 15:58. (Market-on-close orders were tried first, but on
        the paper account they hadn't filled 10 minutes after the close: 37 positions stayed open.)
        The protective stops are cancelled first (they hold the shares the closing orders need)."""
        self.wait_until(CLOSE_AT)
        self.b.cancel_all()
        self.wait_for(lambda: not self.b.open_orders(), seconds=60)    # cancelling takes a moment
        for attempt in (1, 2, 3):
            left = self.close_positions()
            if not left:
                return
            self.log(f"[day] still open after closing (round {attempt}): {', '.join(left)}")
            self.sleep(30)
        self.wait_until(FLAT_AT)
        self.b.cancel_all()                                          # whatever is blocking them
        left = self.close_positions()
        if left:
            self.log(f"[day] ⚠️ still open at 15:58: {', '.join(left)}; the safety net closes them")

    def close_positions(self) -> list[str]:
        """Close every open position with a market order; then return the ones still open."""
        for p in self.b.positions():
            try:
                self.b.close(p["symbol"])
            except PaperError as e:
                self.log(f"[day] couldn't close {p['symbol']} ({str(e)[:80]})")
        self.wait_for(lambda: not self.b.positions(), seconds=60, step=5)
        return [p["symbol"] for p in self.b.positions()]

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

PATTERN_TEST = {"gap": "gap_fade", "breakout": "opening_range", "momentum": "intraday_momentum",
                "gapup": "short_gap_ups", "loser": "short_losers"}
SHORT_NAME = {"gap": "gap fade", "breakout": "breakouts", "momentum": "last half hour", "gapup": "gap-up shorts",
              "loser": "loser shorts"}
PATTERN_NAME = {"gap": "Fade big opening gaps", "breakout": "Breakout from the first half hour",
                "momentum": "Market's last half hour follows its first",
                "gapup": "Short the big gap-ups (tested only)",
                "loser": "Short yesterday's big losers (tested only)"}
MIN_SHARE, MAX_SHARE = 0.15, 0.60     # each pattern's share of the money stays between these
PRIOR_TRADES = 100                    # the 10-year test counts like this many live trades
SHIFT_AFTER, PAUSE_AFTER, COST_AFTER = 20, 40, 50   # live trades needed before each kind of change
# ... and trading days: trades on the same day move together (one market), so one day, however many
# trades it has, can't shift the money or pause a pattern on its own. (Costs need no days: a trade's
# spread doesn't depend on the market's direction.)
SHIFT_DAYS, PAUSE_DAYS = 5, 10
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
        days = len({r["date"] for r in log if r.get("pattern") == p and r.get("ret") is not None})
        out[p] = {"n": len(rets), "days": days, "live": live, "forecast": forecast, "sd": sd, "z": z}
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
    known. Shares follow the expected gains, only for patterns with SHIFT_AFTER+ live trades over
    SHIFT_DAYS+ trading days, and always stay within MIN_SHARE..MAX_SHARE. A pattern clearly worse live than its history
    (PAUSE_AFTER+ trades over PAUSE_DAYS+ days, z below -2) is paused. A great streak never pauses or boosts beyond the cap."""
    def ready(c, trades, days):
        return c["n"] >= trades and c.get("days", days) >= days

    paused = {p for p, c in cmp.items() if ready(c, PAUSE_AFTER, PAUSE_DAYS) and c["z"] is not None and c["z"] < -2}
    active = [p for p in PATTERN_TEST if p not in paused]
    if not active:
        return {}, paused
    if not any(ready(cmp[p], SHIFT_AFTER, SHIFT_DAYS) for p in active):
        return {p: 1 / len(active) for p in active}, paused
    edge = {}
    for p in active:
        c = cmp[p]
        prior = c["forecast"] if c["forecast"] is not None else 0.0
        blended = (PRIOR_TRADES * prior + c["n"] * (c["live"] or 0.0)) / (PRIOR_TRADES + c["n"]) \
            if ready(c, SHIFT_AFTER, SHIFT_DAYS) else prior
        edge[p] = blended - (2 * cost if cost is not None else 0.0)
    lo = min(edge.values())
    raw = {p: e - lo + 1e-4 for p, e in edge.items()}            # the weakest gets the least, never nothing
    return bounded(raw), paused


def bounded(raw: dict[str, float], lo: float = MIN_SHARE, hi: float = MAX_SHARE) -> dict[str, float]:
    """Shares in proportion to `raw`, adding up to 1, none below `lo` or above `hi`: a share that would
    cross a limit is held at it, and the rest is split among the others in the same proportions."""
    lo, hi = min(lo, 1 / len(raw)), max(hi, 1 / len(raw))
    hi = min(hi, 1 - lo * (len(raw) - 1))   # the others at their minimum must still fit (4 patterns: 55%)
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

    - just before the close (from 15:56 New York, after the trading job's 15:50 closing): anything
      still open is closed now
    - after the close or before the open: any position at all was left overnight; it's closed (at the
      next open if the market is shut) and you're told."""
    from trend_bot.alerts import RED

    positions = broker.positions()
    if not positions:
        return None
    t = now.time()
    in_session = dt.time(9, 30) <= t < dt.time(16, 0)
    orders = broker.open_orders()
    closing = {o["symbol"] for o in orders if o.get("time_in_force") == "cls"}
    loose = [p for p in positions if not (in_session and p["symbol"] in closing)]
    if not loose:
        return None
    queued = {o["symbol"] for o in orders if o.get("type") == "market"}
    if not in_session and all(p["symbol"] in queued for p in loose):   # an earlier run already queued them
        log(f"[day] safety net: {len(loose)} position(s) already queued to close at the next open; nothing new")
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


def settle_carried(state: dict, orders: list[dict], today: dt.date) -> tuple[list[dict], list[str], float]:
    """Trades from earlier days still open (held overnight, e.g. closed by the safety net at the next open):
    today's closing fill completes their row in the log and their day's totals. Returns (today's other
    orders, what was settled, its $ total)."""
    open_rows = [r for r in state.get("log", []) if r["date"] < today.isoformat() and r.get("exit") is None
                 and r.get("entry")]
    rest, done, total = list(orders), [], 0.0
    for r in open_rows:
        want = "sell" if r["side"] > 0 else "buy"
        o = next((o for o in rest if o["symbol"] == r["symbol"] and o.get("side") == want
                  and o.get("status") == "filled" and o.get("filled_avg_price")), None)
        if o is None:
            continue
        rest.remove(o)
        exit_ = float(o["filled_avg_price"])
        r.update(exit=exit_, ret=r["side"] * (exit_ / r["entry"] - 1), pnl=r["side"] * r["qty"] * (exit_ - r["entry"]))
        d = state.setdefault("days", {}).setdefault(r["date"], {}).setdefault(r["pattern"], {"pnl": 0.0, "trades": 0})
        d["pnl"] += r["pnl"]
        d["trades"] += 1
        done.append(f"{r['symbol']} {r['pnl']:+,.0f} (from {r['date']})")
        total += r["pnl"]
    return rest, done, total


def report(orders: list[dict], equity: float, state: dict, today: dt.date, tested: dict | None = None,
           last_equity: float | None = None) -> tuple[dict, dict]:
    """Add today's results to the running state (and the trade log); returns (state, Discord embed).

    last_equity: the account's value at the previous close (the broker's own number). The day's change
    in the account is the truth the trade-by-trade results must add up to; a gap is flagged."""
    from trend_bot.alerts import GOLD, GREEN, RED, lead

    tested = {**(tested or {}), "short_gap_ups": GAPUP_TEST, "short_losers": LOSER_TEST}
    orders, carried, carried_pnl = settle_carried(state, orders, today)
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
             "momentum": ("intraday_momentum", "Market's last half hour follows its first"),
             "gapup": ("short_gap_ups", PATTERN_NAME["gapup"]), "loser": ("short_losers", PATTERN_NAME["loser"]),
             "other": ("", "Other")}
    today_total = sum(r["pnl"] for r in day.values()) + carried_pnl
    lines = [lead("the bot's day-trading practice account (fake money) traded today and closed everything by the "
                  "close. A test of whether intraday patterns make money; nothing real was bought or sold."), ""]
    if not day and still_open:
        lines.append(f"{len(still_open)} trade{'s' if len(still_open) != 1 else ''} placed today, results pending: "
                     "not closed yet when this was written (see below).")
    elif not day and not carried:
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
        lines.append(f"• {PATTERN_NAME[p]}: tested {f}, live {lv} over {c['n']} trade{'s' if c['n'] != 1 else ''} "
                     f"on {c.get('days', 0)} day{'s' if c.get('days', 0) != 1 else ''}: "
                     f"{verdict_vs_test(c)}")
    if cost is not None:
        lines.append(f"Measured cost of getting in: {cost:.3%} a side (from {COST_AFTER}+ trades; the test assumed "
                     f"{intraday.COST['stock'] / 2:.3%}).")
    if share:
        lines.append("Tomorrow's money per pattern: " + ", ".join(
            f"{SHORT_NAME[p]} {v:.0%}" for p, v in share.items())
                     + ("" if any(c["n"] >= SHIFT_AFTER and c.get("days", 0) >= SHIFT_DAYS for c in cmp.values())
                        else f" (equal until a pattern has {SHIFT_AFTER} live trades over {SHIFT_DAYS} trading days)"))
    for p in sorted(newly):
        lines.append(f"⏸️ **{PATTERN_NAME[p]} is paused**: after {cmp[p]['n']} live trades it's doing clearly worse "
                     "than its 10-year history. It resumes on its own if its record comes back within range.")
    if carried:
        lines += ["", f"Closed today, from earlier days (held overnight): {', '.join(carried)}: ${carried_pnl:+,.0f}"]
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
