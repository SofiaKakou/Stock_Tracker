"""Demo account: the portfolio to follow, traded with fake money on Alpaca's paper-trading account.

The backtest and the live record use the bot's own price data. This sends the same monthly buys and
sells to a broker's practice account (fake money, real prices), so you can see how the findings do
with real orders: whole shares, the next morning's opening prices, and cash left over.

Safety: the address is fixed to Alpaca's paper-trading server. Paper keys only work there and real
money keys don't, so this can never trade real money. Nothing happens until the ALPACA_API_KEY_ID and
ALPACA_API_SECRET_KEY secrets exist (setup: GUIDE.md, "Demo account").

Each night (after the monthly update, if there was one):
- if the portfolio to follow changed since the last sync, the account is brought to it: stocks that
  left are sold, the rest set to equal amounts with whole-share market orders. Orders sent after
  the close fill at the next day's open. Small differences (under REBALANCE_BAND of the target)
  are left alone, so a month without changes means almost no trades.
- the account's value is saved, for the report: since the first sync vs the S&P 500.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import sqlite3

from trend_bot.db import get_meta, set_meta

PAPER_URL = "https://paper-api.alpaca.markets/v2"   # fixed on purpose: paper keys only work here
KEY_ENV, SECRET_ENV = "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"
INVESTED = 0.98          # keep 2% in cash so whole-share rounding and price moves never overdraw
REBALANCE_BAND = 0.10    # leave a holding alone if it's within 10% of its target amount


class PaperError(RuntimeError):
    pass


class Paper:
    """The few Alpaca trading calls the bot needs (plain HTTP, no extra package)."""

    def __init__(self, key: str, secret: str, session=None):
        import requests

        self.http = session or requests.Session()
        self.http.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    @classmethod
    def from_env(cls) -> "Paper | None":
        key, secret = os.environ.get(KEY_ENV), os.environ.get(SECRET_ENV)
        return cls(key, secret) if key and secret else None

    def _call(self, method: str, path: str, **kw):
        resp = self.http.request(method, PAPER_URL + path, timeout=30, **kw)
        if resp.status_code >= 400:
            raise PaperError(f"{method} {path}: {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.text else None

    def account(self) -> dict:
        return self._call("GET", "/account")

    def positions(self) -> list[dict]:
        return self._call("GET", "/positions")

    def open_orders(self) -> list[dict]:
        return self._call("GET", "/orders", params={"status": "open", "limit": 500})

    def order(self, symbol: str, qty: int, side: str, tag: str) -> dict:
        return self._call("POST", "/orders", json={"symbol": symbol, "qty": str(qty), "side": side,
                                                    "type": "market", "time_in_force": "day",
                                                    "client_order_id": tag})

    def close(self, symbol: str) -> dict:
        return self._call("DELETE", f"/positions/{symbol}")

    def clock(self) -> dict:
        return self._call("GET", "/clock")

    def get_order(self, order_id: str) -> dict:
        return self._call("GET", f"/orders/{order_id}")

    def cancel(self, order_id: str) -> None:
        self._call("DELETE", f"/orders/{order_id}")


def test_trade(client: Paper, ticker: str = "SPY", wait: float = 60, sleep=None, log=print) -> bool:
    """Buy 1 share and sell it straight back, to prove orders work end to end (fake money).

    Only while the market is open, so no order is left waiting (that would hold up the nightly sync).
    An order that hasn't filled within `wait` seconds is cancelled. True if both filled."""
    import time

    sleep = sleep or time.sleep
    clock = client.clock()
    if not clock.get("is_open"):
        log(f"[demo] the market is closed (next open {clock.get('next_open', '?')}); the test trade only runs "
            "while it's open, so no order is left waiting.")
        return False

    def fill(side: str) -> float | None:
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        o = client.order(symbol(ticker), 1, side, f"tb-test-{side}-{stamp}")
        for _ in range(int(wait / 2) + 1):
            o = client.get_order(o["id"])
            if o.get("status") == "filled":
                return float(o["filled_avg_price"])
            if o.get("status") in ("canceled", "expired", "rejected"):
                break
            sleep(2)
        if o.get("status") not in ("canceled", "expired", "rejected"):
            client.cancel(o["id"])
        log(f"[demo] the test {side} of 1 {ticker} didn't fill (status: {o.get('status')}); cancelled.")
        return None

    bought = fill("buy")
    if bought is None:
        return False
    log(f"[demo] bought 1 {ticker} at ${bought:,.2f}")
    sold = fill("sell")
    if sold is None:
        log(f"[demo] still holding the 1 {ticker} share; the nightly sync sells it (it isn't in the portfolio).")
        return False
    log(f"[demo] sold 1 {ticker} at ${sold:,.2f} ({sold - bought:+.2f}). Orders work end to end.")
    return True


def symbol(ticker: str) -> str:
    """Our tickers (Yahoo style, BRK-B) to Alpaca's (BRK.B)."""
    return ticker.replace("-", ".")


def plan(targets: list[str], prices: dict[str, float], holdings: dict[str, float], equity: float,
         band: float = REBALANCE_BAND) -> list[tuple[str, str, int]]:
    """Orders that bring the account to equal amounts in `targets`: [(symbol, 'sell'|'buy'|'close', qty)].

    holdings: shares held per Alpaca symbol. Sells come first (they pay for the buys)."""
    want = {symbol(t): prices[t] for t in targets if prices.get(t)}
    each = equity * INVESTED / max(len(want), 1)
    sells, buys = [], []
    for s, qty in holdings.items():
        if s not in want and qty:
            sells.append((s, "close", int(qty)))
    for s, price in want.items():
        have = holdings.get(s, 0.0)
        if abs(have * price - each) <= band * each:
            continue
        diff = math.floor(each / price) - int(have)
        if diff > 0:
            buys.append((s, "buy", diff))
        elif diff < 0:
            sells.append((s, "sell", -diff))
    return sells + buys


def _prices(con: sqlite3.Connection, tickers: list[str]) -> dict[str, float]:
    out = {}
    for t in tickers:
        row = con.execute("SELECT close FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 1", (t,)).fetchone()
        if row and row[0]:
            out[t] = float(row[0])
    return out


def sync(con: sqlite3.Connection, client: Paper, today: dt.date | None = None, dry_run: bool = False,
         log=print) -> dict:
    """Bring the demo account to the portfolio to follow (if it changed) and save the account's value.

    dry_run: work out the orders ("planned") but send nothing and save nothing.
    Returns {"traded": [...], "failed": [...], "skipped": reason or "", "status": {...}}."""
    from trend_bot import ideas

    today = today or dt.date.today()
    acct = client.account()
    equity = float(acct["equity"])
    out = {"traded": [], "failed": [], "skipped": ""}
    port = ideas.load_portfolio(con)
    if not port:
        out["skipped"] = "no portfolio to follow yet"
    elif get_meta(con, "paper_synced") == port["date"]:
        out["skipped"] = f"already matches the portfolio of {port['date']}"
    elif client.open_orders():
        out["skipped"] = "earlier orders haven't filled yet (they fill at the next open)"
    else:
        targets = [p["ticker"] for p in port["buys"] + port["holds"]]
        holdings = {p["symbol"]: float(p["qty"]) for p in client.positions()}
        orders = plan(targets, _prices(con, targets), holdings, equity)
        if dry_run:
            return {**out, "planned": orders, "status": {"equity": equity}}
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        for s, side, qty in orders:
            try:
                if side == "close":
                    client.close(s)
                else:
                    client.order(s, qty, side, f"tb-{port['date']}-{s}-{side}-{stamp}"[:48])
                out["traded"].append((s, side, qty))
            except PaperError as e:   # e.g. a stock the broker doesn't offer: noted, the rest goes ahead
                out["failed"].append((s, side, qty, str(e)))
        set_meta(con, "paper_synced", port["date"])
        log(f"[demo] {len(out['traded'])} order(s) sent for the portfolio of {port['date']}"
            + (f", {len(out['failed'])} refused" if out["failed"] else ""))

    if dry_run:
        return {**out, "planned": [], "status": {"equity": equity}}
    status = json.loads(get_meta(con, "paper_status") or "{}")
    if out["traded"] and "start_date" not in status:
        status.update(start_date=today.isoformat(), start_equity=equity)
    status.update(date=today.isoformat(), equity=equity, cash=float(acct.get("cash") or 0))
    set_meta(con, "paper_status", json.dumps(status))
    con.commit()
    out["status"] = status
    return out


def record(con: sqlite3.Connection, benchmark: str = "SPY") -> dict | None:
    """The demo account since its first trades vs the S&P 500 over the same days (None before that)."""
    s = json.loads(get_meta(con, "paper_status") or "{}")
    if not s.get("start_date") or not s.get("start_equity"):
        return None
    first = con.execute("SELECT close FROM prices WHERE ticker = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                        (benchmark, s["start_date"])).fetchone()   # the close on the night the orders went in
    last = con.execute("SELECT close FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 1", (benchmark,)).fetchone()
    spy = (last[0] / first[0] - 1) if first and last and first[0] else None
    return {"start": s["start_date"], "as_of": s["date"], "equity": s["equity"],
            "account": s["equity"] / s["start_equity"] - 1, "spy": spy}


def line(con: sqlite3.Connection) -> str:
    """One line for the report and email: how the demo account is doing."""
    r = record(con)
    if not r:
        return ""
    vs = f" vs S&P 500 **{r['spy']:+.1%}**" if r["spy"] is not None else ""
    return (f"🧪 **Demo account** (fake money on Alpaca, real prices): **${r['equity']:,.0f}**, "
            f"**{r['account']:+.1%}** since {r['start']}{vs}.")


def embed(result: dict, port_date: str) -> dict | None:
    """Discord message on the nights the demo account traded (nothing otherwise)."""
    from trend_bot.alerts import GOLD, lead

    if not result["traded"] and not result["failed"]:
        return None
    words = {"close": "sell all", "sell": "sell", "buy": "buy"}
    lines = [lead("the bot's practice account (fake money) is following this month's portfolio. "
                  "The orders fill when the market opens; nothing real is bought or sold."), ""]
    lines += [f"• {words[side]} {qty:,} {s}" if side != "close" else f"• sell all {s}"
              for s, side, qty in result["traded"]]
    if result["failed"]:
        lines += ["", "Refused by the broker (left out):"] + [f"• {s}: {why[:80]}" for s, _, _, why in result["failed"]]
    st = result["status"]
    lines += ["", f"Account value now: ${st['equity']:,.0f} (cash ${st['cash']:,.0f}). Portfolio of {port_date}."]
    return {"title": "🧪 Demo account: orders sent", "description": "\n".join(lines)[:4000], "color": GOLD}
