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
MOC_AT, FLAT_AT = dt.time(15, 45), dt.time(15, 58)
FALLBACK_STOCKS = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "AMD", "NFLX", "JPM", "COST",
                   "LLY", "UNH", "XOM", "BAC", "WMT", "ORCL", "CRM", "ADBE", "INTC", "QCOM", "MU", "PLTR", "COIN",
                   "UBER", "BA", "DIS", "PYPL", "SHOP", "SMCI", "MSTR", "GS", "CAT", "HD", "PEP", "KO", "MRK",
                   "ABBV", "PFE", "CVX", "WFC", "C", "TXN", "AMAT", "LRCX", "PANW"]


class DayBroker(Paper):
    @classmethod
    def from_env(cls) -> "DayBroker | None":
        key, secret = os.environ.get(KEY_ENV), os.environ.get(SECRET_ENV)
        return cls(key, secret) if key and secret else None

    def order(self, symbol: str, qty: int, side: str, tag: str, tif: str = "day") -> dict:
        return self._call("POST", "/orders", json={"symbol": symbol, "qty": str(qty), "side": side,
                                                    "type": "market", "time_in_force": tif,
                                                    "client_order_id": tag})

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
    in minus money out over the day is the trade's result."""
    flows: dict[str, float] = {}
    pattern_of: dict[str, str] = {}
    for o in orders:
        if o.get("status") != "filled" or not o.get("filled_avg_price"):
            continue
        tag = str(o.get("client_order_id", ""))
        sym, qty, price = o["symbol"], float(o["filled_qty"]), float(o["filled_avg_price"])
        flows[sym] = flows.get(sym, 0.0) + (qty * price if o["side"] == "sell" else -qty * price)
        parts = tag.split("-")          # td-<yyyymmdd>-<pattern>-<symbol>, exits: td-<yyyymmdd>-x-<symbol>-exit
        if tag.startswith("td-") and len(parts) >= 4 and parts[2] != "x":
            pattern_of.setdefault(sym, parts[2])
    out: dict[str, dict] = {}
    for sym, money in flows.items():
        p = pattern_of.get(sym, "other")
        r = out.setdefault(p, {"pnl": 0.0, "trades": 0, "symbols": []})
        r["pnl"] += money
        r["trades"] += 1
        r["symbols"].append(f"{sym} {money:+,.0f}")
    return out


# --- The live session --------------------------------------------------------------------------

class Session:
    def __init__(self, broker: DayBroker, data: intraday.Data, symbols: list[str], today: dt.date,
                 log=print, sleep=time.sleep, now=lambda: dt.datetime.now(NY)):
        self.b, self.data, self.log, self.sleep, self.now = broker, data, log, sleep, now
        self.today = today
        self.etfs = [s for s in symbols if s in intraday.ETFS]
        self.stocks = [s for s in symbols if s not in intraday.ETFS]
        self.utc0 = dt.datetime.combine(today, dt.time(9, 0), NY).astimezone(dt.timezone.utc)

    def at(self, t: dt.time) -> dt.datetime:
        return dt.datetime.combine(self.today, t, NY)

    def wait_until(self, t: dt.time) -> None:
        while (left := (self.at(t) - self.now()).total_seconds()) > 0:
            self.sleep(min(left, 60))

    def today_orders(self) -> list[dict]:
        return [o for o in self.b.orders_since(self.utc0.isoformat()) if
                str(o.get("client_order_id", "")).startswith(f"td-{self.today:%Y%m%d}")]

    def traded(self) -> set[str]:
        return {o["symbol"] for o in self.today_orders()}

    def budget(self) -> float:
        return float(self.b.account()["equity"]) * INVESTED / (STOCK_SLOTS + ETF_SLOTS)

    def enter(self, pattern: str, signals: list[tuple[str, int]], prices: dict[str, float], slots: int) -> int:
        done = self.traded()
        used = len(done & set(self.stocks if pattern != "momentum" else self.etfs))
        each, sent = self.budget(), 0
        for sym, side in signals:
            if sym in done or used + sent >= slots or not prices.get(sym):
                continue
            qty = shares(prices[sym], each)
            if not qty:
                continue
            try:
                self.b.order(sym, qty, "buy" if side > 0 else "sell", f"td-{self.today:%Y%m%d}-{pattern}-{sym}")
                sent += 1
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
        self.wait_until(dt.time(9, 31))
        if self.now().time() <= GAP_LATEST:
            prev = self.prev_closes(self.stocks)
            first = self._bars(self.stocks, "1Min", self.at(dt.time(9, 30)))
            opens = {s: b[0]["o"] for s, b in first.items() if b}
            last = {s: b[-1]["c"] for s, b in first.items() if b}
            self.enter("gap", gap_signals(opens, prev), last, STOCK_SLOTS)
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
            sig = breakout_signals({s: b["h"] for s, b in first.items()}, {s: b["l"] for s, b in first.items()},
                                   last, self.traded())
            self.enter("breakout", sig, last, STOCK_SLOTS)

    def _end(self, bar: dict) -> dt.datetime:
        return dt.datetime.fromisoformat(bar["t"].replace("Z", "+00:00")) + dt.timedelta(minutes=30)

    def afternoon(self) -> None:
        self.breakouts(AFTERNOON_CHECKS[:-1])
        self.wait_until(dt.time(15, 30, 45))
        prev = self.prev_closes(self.etfs)
        first = self.first_half_hour(self.etfs)
        now_px = {s: b[-1]["c"] for s, b in self._bars(self.etfs, "1Min", self.at(dt.time(15, 25))).items() if b}
        self.enter("momentum", momentum_signals({s: b["c"] for s, b in first.items()}, prev), now_px, ETF_SLOTS)
        self.breakouts(AFTERNOON_CHECKS[-1:])
        self.flatten()

    def flatten(self) -> None:
        """Out at the closing price: market-on-close orders at 15:45, then anything left at 15:58."""
        self.wait_until(MOC_AT)
        for p in self.b.positions():
            qty = abs(int(float(p["qty"])))
            side = "sell" if float(p["qty"]) > 0 else "buy"
            try:
                self.b.order(p["symbol"], qty, side, f"td-{self.today:%Y%m%d}-x-{p['symbol']}-exit", tif="cls")
            except PaperError as e:
                self.log(f"[day] closing order for {p['symbol']} refused ({str(e)[:80]}); closed at 15:58 instead")
        self.wait_until(FLAT_AT)
        if self.b.positions():
            self.b.close_all()
            self.log("[day] closed what was left at 15:58")


def report(orders: list[dict], equity: float, state: dict, today: dt.date, tested: dict | None = None) -> tuple[dict, dict]:
    """Add today's results to the running state; returns (state, Discord embed)."""
    from trend_bot.alerts import GOLD, GREEN, RED, lead

    day = pnl(orders)
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
