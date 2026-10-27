"""The $100 crypto account (fake money, Alpaca paper account PA3HK7NRJ): buys the big dips.

From the crypto study (`study crypto`): a big coin that fell 5%+ in the last hour usually bounces; bought
right after and sold a day later it made +2.3% a trade after fees since 2021, in both halves of the
data, far better than buying at random times. Rare: about one trade every two or three days.

Every 15 minutes (GitHub, and the outside timer): coins held a day are sold; then any coin whose last
complete 15-minute bar closed 5%+ below its price an hour earlier is bought for about $25 (up to four at
once, one per coin), with a limit order at most 0.3% above the asking price that fills now or not at all,
and a safety stop at the broker 15% below the fill (the study's coins all survived; coins that
collapsed, like LUNA in 2022, aren't in it, so a dip-buy that keeps falling is cut off). Each trade goes
to Discord, and the evening run adds a summary.

The account used to hold the monthly portfolio at $100; any stocks still in it are sold on the first run.
Paper money only: the broker address is fixed to Alpaca's paper server (trend_bot.paper).
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd

from trend_bot import crypto
from trend_bot.paper import SMALL_KEY_ENV, SMALL_SECRET_ENV, Paper, PaperError

COINS = crypto.SYMBOLS
DIP = 0.05               # fell this much in the last hour (4 complete 15-minute bars)
HOLD = dt.timedelta(days=1)
STOP = 0.15              # the safety stop, below the fill
STOP_LIMIT = 0.20        # ... sold at no less than this far below (a stop on a crashing coin needs room)
SLACK = 0.003            # buy at most 0.3% above the asking price
AMOUNT = 25.0            # dollars a trade
SLOTS = 4
# Two kinds of trade share the account: the tested one, and an experiment the user asked for (more trades a
# day). The study says the fast one loses to the fees (about -0.2% a trade after 0.30%): it runs to see that
# live, small and separate, and its results are reported apart.
KINDS = {"big": {"dip": DIP, "hold": HOLD, "amount": AMOUNT, "slots": 2, "name": "big dip (tested: passes)"},
         "fast": {"dip": 0.02, "hold": dt.timedelta(hours=1), "amount": 10.0, "slots": 4,
                  "name": "fast experiment: 2% dip, sold an hour later (tested: loses to fees)"}}
MIN_TRADE = 5.0          # less cash than this: no trade
FRESH = dt.timedelta(minutes=25)   # a signal from a bar older than this is stale (a run started late)
QUOTES_URL = "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes"
TEST = {"per_trade": 0.0233, "hit": 0.63, "per_day": 0.4}   # the study: 1-day hold, after limit-order fees


def key(symbol: str) -> str:
    """'BTC/USD' and 'BTCUSD' (how positions name it) as one key."""
    return symbol.replace("/", "")


class CryptoBroker(Paper):
    @classmethod
    def from_env(cls, key_env: str = SMALL_KEY_ENV, secret_env: str = SMALL_SECRET_ENV) -> "CryptoBroker | None":
        return super().from_env(key_env, secret_env)

    def buy_limit(self, symbol: str, qty: float, limit: float, tag: str) -> dict:
        return self._call("POST", "/orders", json={"symbol": symbol, "qty": f"{qty:.8f}", "side": "buy",
                                                    "type": "limit", "limit_price": f"{limit:.6g}",
                                                    "time_in_force": "ioc", "client_order_id": tag})

    def stop_sell(self, symbol: str, qty: str, stop: float, limit: float, tag: str) -> dict:
        return self._call("POST", "/orders", json={"symbol": symbol, "qty": qty, "side": "sell",
                                                    "type": "stop_limit", "stop_price": f"{stop:.6g}",
                                                    "limit_price": f"{limit:.6g}", "time_in_force": "gtc",
                                                    "client_order_id": tag})


def latest_quotes(data, symbols: list[str]) -> dict[str, dict]:
    resp = data.http.get(QUOTES_URL, params={"symbols": ",".join(symbols)}, timeout=30)
    resp.raise_for_status()
    return {s: {"bid": q.get("bp"), "ask": q.get("ap")} for s, q in (resp.json().get("quotes") or {}).items()}


def dips(bars: pd.DataFrame, now: dt.datetime, threshold: float = DIP) -> dict[str, float]:
    """{coin: last hour's move} for coins whose last complete 15-minute bar (ended within FRESH of now)
    closed `threshold`+ below the close four bars earlier."""
    out = {}
    for sym, g in bars.sort_values("t").groupby("symbol"):
        t = pd.to_datetime(g["t"], utc=True)
        done = g[(t + pd.Timedelta(minutes=15)) <= pd.Timestamp(now)]
        if len(done) < 5:
            continue
        end = pd.Timestamp(done["t"].iloc[-1]) + pd.Timedelta(minutes=15)
        if pd.Timestamp(now) - end > FRESH:
            continue
        c = done["close"].to_numpy(float)
        move = c[-1] / c[-5] - 1
        if move <= -threshold:
            out[sym] = float(move)
    return out


def run(broker: CryptoBroker, data, state: dict, now: dt.datetime | None = None, log=print,
        sleep=time.sleep) -> list[dict]:
    """One check: sell what's due, sell leftover stocks, buy new big dips. Returns the Discord embeds."""
    now = now or dt.datetime.now(dt.timezone.utc)
    embeds: list[dict] = []
    held = {key(p["symbol"]): p for p in broker.positions()}
    open_trades = state.setdefault("open", {})

    # Stocks from the account's monthly-portfolio days: sold (orders wait for the open if it's shut).
    stocks = [p for p in held.values() if p.get("asset_class") == "us_equity"]
    for p in stocks:
        try:
            broker.close(p["symbol"])
        except PaperError as e:   # e.g. already being sold
            log(f"[crypto] {p['symbol']}: not sold now ({str(e)[:80]})")
    if stocks and not state.get("stocks_sold"):
        state["stocks_sold"] = now.isoformat()
        embeds.append({"title": "🪙 $100 account: now a crypto account", "color": 0xEDA100,
                       "description": f"Sold its {len(stocks)} stocks (from the monthly portfolio) to trade "
                                      "crypto: it buys coins that fell 5%+ in an hour and sells them a day later."})

    # A coin held without a record (the saved state was lost): sold within a day from now, like any trade.
    for k, p in held.items():
        if p.get("asset_class") == "crypto" and k not in open_trades:
            price = float(p.get("avg_entry_price") or 0)
            open_trades[k] = {"symbol": p["symbol"], "time": now.isoformat(), "price": price, "qty": p["qty"],
                              "dip": None, "cost": float(p.get("cost_basis") or 0), "kind": "big"}
            log(f"[crypto] {p['symbol']}: held without a record; sold within a day")

    # Trades that ended: stopped out at the broker, or held their time (a day; an hour for the fast ones).
    for k, tr in list(open_trades.items()):
        pos = held.get(k)
        age = now - dt.datetime.fromisoformat(tr["time"])
        if pos is None:   # the safety stop sold it (or it was sold by hand)
            price = None
            if tr.get("stop_id"):
                try:
                    o = broker.get_order(tr["stop_id"])
                    price = float(o["filled_avg_price"]) if o.get("filled_avg_price") else None
                except PaperError:
                    pass
            embeds.append(_closed(tr, price, now, "safety stop"))
            _log_trade(state, tr, price, now, "stop")
            del open_trades[k]
            continue
        kind = tr.get("kind", "big")
        if age >= KINDS[kind]["hold"]:
            if tr.get("stop_id"):
                try:
                    broker.cancel(tr["stop_id"])
                except PaperError:
                    pass
            try:
                o = broker.close(pos["symbol"])
            except PaperError as e:
                log(f"[crypto] {tr['symbol']}: couldn't sell yet ({str(e)[:80]}); next check tries again")
                continue
            price = _fill(broker, o, sleep) or float(pos.get("current_price") or 0) or None
            if kind == "big":       # the fast experiment's many trades go in the evening summary only
                embeds.append(_closed(tr, price, now, "a day later"))
            _log_trade(state, tr, price, now, "day" if kind == "big" else "hour")
            del open_trades[k]

    # New dips: 5%+ is the tested trade, 2%-5% the fast experiment.
    start = (now - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        found = dips(crypto.fetch(data, COINS, start=start, end=now.strftime("%Y-%m-%dT%H:%M:%SZ")), now,
                     KINDS["fast"]["dip"])
    except Exception as e:  # noqa: BLE001 - no prices this time: the next check tries again
        log(f"[crypto] no prices ({str(e)[:80]})")
        return embeds
    log(f"[crypto] {now:%Y-%m-%d %H:%M} UTC: dips now: "
        + (", ".join(f"{s} {m:+.1%}" for s, m in found.items()) if found else "none"))
    todo = [s for s in sorted(found, key=found.get) if key(s) not in open_trades and key(s) not in held]
    if not todo:
        return embeds
    cash = float(broker.account().get("cash") or 0)
    quotes = latest_quotes(data, todo)
    for sym in todo:
        kind = "big" if found[sym] <= -KINDS["big"]["dip"] else "fast"
        if sum(t.get("kind", "big") == kind for t in open_trades.values()) >= KINDS[kind]["slots"]:
            continue
        amount = min(KINDS[kind]["amount"], cash * 0.98)
        ask = (quotes.get(sym) or {}).get("ask")
        if amount < MIN_TRADE or not ask:
            log(f"[crypto] {sym}: skipped ({'no cash' if amount < MIN_TRADE else 'no price'})")
            continue
        tag = f"cx-{kind}-{now:%Y%m%d%H%M}-{key(sym)}"
        try:
            o = broker.buy_limit(sym, amount / ask, ask * (1 + SLACK), tag)
        except PaperError as e:
            log(f"[crypto] {sym}: refused ({str(e)[:80]})")
            continue
        price = _fill(broker, o, sleep)
        if not price:
            log(f"[crypto] {sym}: the price ran away; not bought")
            continue
        pos = next((p for p in broker.positions() if key(p["symbol"]) == key(sym)), None)
        qty = pos["qty"] if pos else str(o.get("filled_qty") or "0")   # after the fee (paid in the coin)
        tr = {"symbol": sym, "time": now.isoformat(), "price": price, "qty": qty, "dip": found[sym],
              "cost": float(qty) * price, "kind": kind}
        try:
            stop = broker.stop_sell(sym, qty, price * (1 - STOP), price * (1 - STOP_LIMIT), f"{tag}-stop")
            tr["stop_id"] = stop.get("id")
        except PaperError as e:   # no broker stop: the 15-minute check still sells it at the day's end
            log(f"[crypto] {sym}: safety stop refused ({str(e)[:80]})")
        open_trades[key(sym)] = tr
        cash -= tr["cost"]
        log(f"[crypto] {kind}: bought {qty} {sym} at {price:,.6g} (~${tr['cost']:.2f}) after {found[sym]:+.1%} in an hour")
        if kind == "big":
            embeds.append({"title": f"🪙 Bought {sym.split('/')[0]} (big dip)", "color": 0x0CA30C,
                           "description": f"It fell **{found[sym]:.1%}** in the last hour; bought ~${tr['cost']:.2f} at "
                                          f"{price:,.6g}. Sold in 24 hours, or by the safety stop at "
                                          f"{price * (1 - STOP):,.6g} (15% lower). Fake money ($100 crypto account)."})
    return embeds


def _fill(broker, order: dict, sleep, tries: int = 10) -> float | None:
    """The order's average fill price once it's done (None: not filled)."""
    o = order
    for _ in range(tries):
        if o.get("status") in ("filled", "canceled", "expired", "rejected") or o.get("filled_avg_price"):
            break
        sleep(1)
        try:
            o = broker.get_order(order["id"])
        except PaperError:
            break
    return float(o["filled_avg_price"]) if o.get("filled_avg_price") else None


def _closed(tr: dict, price: float | None, now: dt.datetime, why: str) -> dict:
    coin = tr["symbol"].split("/")[0]
    if price is None:
        return {"title": f"🪙 Sold {coin} ({why})", "color": 0x8D8C86,
                "description": f"Sold ({why}); the price wasn't reported yet, the evening summary has it."}
    ret = price / tr["price"] - 1
    return {"title": f"🪙 Sold {coin} ({why}): {ret:+.1%}", "color": 0x0CA30C if ret > 0 else 0xD03B3B,
            "description": f"Bought at {tr['price']:,.6g}, sold at {price:,.6g}: **{ret:+.1%}** "
                           f"(${tr['cost'] * ret:+.2f} before fees). Fake money ($100 crypto account)."}


def _log_trade(state: dict, tr: dict, price: float | None, now: dt.datetime, how: str) -> None:
    state.setdefault("log", []).append({"symbol": tr["symbol"], "in": tr["time"], "out": now.isoformat(),
                                        "entry": tr["price"], "exit": price, "how": how, "cost": tr["cost"],
                                        "ret": (price / tr["price"] - 1) if price else None,
                                        "kind": tr.get("kind", "big")})


def summary(broker: CryptoBroker, state: dict, now: dt.datetime | None = None) -> dict:
    """The evening line: the account, today's trades, all trades so far vs the study."""
    from trend_bot.alerts import GOLD, GREEN, RED, lead

    now = now or dt.datetime.now(dt.timezone.utc)
    acct = broker.account()
    equity, last = float(acct.get("equity") or 0), float(acct.get("last_equity") or 0)
    log = [r for r in state.get("log", []) if r.get("ret") is not None]
    day = [r for r in log if now - dt.datetime.fromisoformat(r["out"]) <= dt.timedelta(days=1)]
    lines = [lead("the $100 crypto account (fake money) buys big coins that fell 5%+ in an hour and sells them a "
                  "day later (the only crypto idea that beat Alpaca's fees in our test), and, as an experiment for "
                  "more trades, 2% dips sold an hour later (the test says those lose to fees)."), ""]
    lines.append(f"Account **${equity:,.2f}**" + (f" ({equity / last - 1:+.2%} since yesterday)" if last else ""))
    held = state.get("open", {})
    lines.append(f"Holding: {', '.join(t['symbol'] for t in held.values())}" if held else "Holding: nothing (waiting for a big dip)")
    big_day = [r for r in day if r.get("kind", "big") == "big"]
    if big_day:
        lines.append("Big dips sold in the last 24 hours: "
                     + ", ".join(f"{r['symbol'].split('/')[0]} {r['ret']:+.1%}" for r in big_day))
    for kind, k in KINDS.items():
        rows = [r for r in log if r.get("kind", "big") == kind]
        today = sum(1 for r in day if r.get("kind", "big") == kind)
        if rows:
            avg = sum(r["ret"] for r in rows) / len(rows)
            lines.append(f"**{k['name']}**: {len(rows)} trades ({today} today), average {avg:+.2%} before fees "
                         f"(fees ~0.3% a trade), right {sum(r['ret'] > 0 for r in rows) / len(rows):.0%}, "
                         f"${sum(r['ret'] * r['cost'] for r in rows):+.2f} before fees")
        else:
            lines.append(f"**{k['name']}**: no finished trades yet")
    lines.append(f"(The test: big dips {TEST['per_trade']:+.1%} a trade after fees, about one every 2-3 days; "
                 "the fast experiment about -0.2% a trade, several a day.)")
    change = equity - last if last else 0
    return {"title": "🪙 $100 crypto account", "description": "\n".join(lines)[:4000],
            "color": GREEN if change > 0 else RED if change < 0 else GOLD}


def load_state(path: str | Path) -> dict:
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else {}
