"""The $100 crypto account (fake money, Alpaca paper account PA3HK7NRJ): buys dips, sells a day later.

From the crypto study (`study crypto --all-coins`, 15-minute prices of Alpaca's 36 coins since 2021): a coin
that fell sharply in the last hour usually bounces. Bought right after and sold a day later, 3%+ dips made
+0.34% a trade after limit-order fees (about 3.7 a day over all coins), 4%+ +0.81%, 5%+ +1.57%: smaller
dips, more trades, less each. The user chose 3%+ for more trades.

Every 15 minutes (GitHub, and the outside timer): coins held their time are sold; then any coin (all of the
broker's, except the stable ones) whose last complete 15-minute bar closed 3%+ below its price an hour
earlier is bought for about $16 (up to five at once, one per coin), with a limit order at most 0.3% above
the asking price that fills now or not at all, and a safety stop at the broker 15% below the fill (coins
that collapsed, like LUNA in 2022, aren't in the data, so a dip-buy that keeps falling is cut off). Next to
it, a small experiment: 2%-3% dips sold an hour later (expected to lose to the fees). Each dip trade goes
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

COINS = crypto.SYMBOLS   # if the broker's list can't be read
# Coins built not to move (dollar and gold tokens): a "dip" there is a data glitch, never a bounce.
STABLE = {"USDT", "USDC", "USDG", "PAXG", "DAI", "PYUSD", "TUSD", "USDP", "FDUSD", "XAUT", "USDE"}
DIP = 0.03               # fell this much in the last hour (4 complete 15-minute bars)
HOLD = dt.timedelta(days=1)
STOP = 0.15              # the safety stop, below the fill
STOP_LIMIT = 0.20        # ... sold at no less than this far below (a stop on a crashing coin needs room)
SLACK = 0.003            # buy at most 0.3% above the asking price
AMOUNT = 16.0            # dollars a trade
SLOTS = 4
# Two kinds of trade share the account. The dip (all 36 of Alpaca's coins since 2021, `study crypto
# --all-coins`): 3%+ in an hour, held a day, +0.34% a trade after limit-order fees, about 3.7 a day; the user
# chose it over 4%+ (+0.81%, 1.9 a day) and 5%+ (+1.57%, 1.1 a day) for more trades. Our orders take the
# asking price (the 0.25% fee, not 0.15%), at which the test makes about +0.14% a trade: thin. And an
# experiment the user asked for: 2%-3% dips sold an hour later, which the test says lose (-0.21% a trade).
KINDS = {"big": {"dip": DIP, "hold": HOLD, "amount": AMOUNT, "slots": 5, "name": "dip 3%+, held a day (tested: passes, thin)"},
         "fast": {"dip": 0.02, "hold": dt.timedelta(hours=1), "amount": 10.0, "slots": 2,
                  "name": "fast experiment: 2% dip, sold an hour later (tested: loses to fees)"}}
MIN_TRADE = 5.0          # less cash than this: no trade
FRESH = dt.timedelta(minutes=25)   # a signal from a bar older than this is stale (a run started late)
QUOTES_URL = "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes"
TEST = {"per_trade": 0.0034, "hit": 0.53, "per_day": 3.7}   # the study: 3%+ dips held a day, after limit-order fees


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


def coins(broker, state: dict, today: dt.date) -> list[str]:
    """Every coin the broker trades against the dollar, except the stable ones; read once a day."""
    have = state.get("coins") or {}
    if have.get("date") == today.isoformat() and have.get("list"):
        return have["list"]
    try:
        found = [s for s in crypto.all_coins(broker) if s.split("/")[0] not in STABLE]
    except Exception:  # noqa: BLE001 - the broker's list can't be read: the big ones
        return have.get("list") or COINS
    state["coins"] = {"date": today.isoformat(), "list": found}
    return found or COINS


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
                                      "crypto: it buys coins that fell sharply in an hour and sells them a day later."})

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

    # New dips: 3%+ is the tested trade, 2%-3% the fast experiment.
    start = (now - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        found = dips(crypto.fetch(data, coins(broker, state, now.date()), start=start,
                                  end=now.strftime("%Y-%m-%dT%H:%M:%SZ")), now, KINDS["fast"]["dip"])
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
            embeds.append({"title": f"🪙 Bought {sym.split('/')[0]} (dip)", "color": 0x0CA30C,
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
    lines = [lead("the $100 crypto account (fake money) buys coins that fell 3%+ in an hour and sells them a day "
                  "later (beat Alpaca's fees in our test, thinly), and, as an experiment, 2% dips sold an hour later "
                  "(the test says those lose to fees)."), ""]
    lines.append(f"Account **${equity:,.2f}**" + (f" ({equity / last - 1:+.2%} since yesterday)" if last else ""))
    held = state.get("open", {})
    lines.append(f"Holding: {', '.join(t['symbol'] for t in held.values())}" if held else "Holding: nothing (waiting for a dip)")
    big_day = [r for r in day if r.get("kind", "big") == "big"]
    if big_day:
        lines.append("Dip trades sold in the last 24 hours: "
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
    lines.append(f"(The test: 3%+ dips {TEST['per_trade']:+.2%} a trade after limit-order fees, about "
                 f"{TEST['per_day']:.0f} a day over all coins; the fast experiment about -0.2% a trade.)")
    change = equity - last if last else 0
    return {"title": "🪙 $100 crypto account", "description": "\n".join(lines)[:4000],
            "color": GREEN if change > 0 else RED if change < 0 else GOLD}


def load_state(path: str | Path) -> dict:
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else {}


def diagnose(broker: CryptoBroker, data, state: dict, now: dt.datetime | None = None, days: int = 90,
             log=print) -> str:
    """A look under the hood (nothing traded): the account's own record of the last week (every fill,
    fee and transfer, to explain a change in its value), and how many dips the market actually gave
    lately: per week over the last `days` days, the dips each rule would have bought and how they did."""
    import numpy as np

    now = now or dt.datetime.now(dt.timezone.utc)
    acct = broker.account()
    lines = [f"Account: equity ${float(acct.get('equity') or 0):,.2f}, at the last close "
             f"${float(acct.get('last_equity') or 0):,.2f}, cash ${float(acct.get('cash') or 0):,.2f}"]
    for p in broker.positions():
        lines.append(f"  holding {p['symbol']} {p.get('qty')} worth ${float(p.get('market_value') or 0):,.2f} "
                     f"({float(p.get('unrealized_pl') or 0):+,.2f})")
    since = (now - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    acts = broker._call("GET", "/account/activities", params={"after": since, "direction": "asc", "page_size": 100}) or []
    lines += ["", f"The account's own record since {since[:10]} ({len(acts)} entries):"]
    for a in acts:
        when = str(a.get("transaction_time") or a.get("date") or "")[:16]
        if a.get("activity_type") == "FILL":
            lines.append(f"  {when} {a['activity_type']} {a.get('side')} {a.get('qty')} {a.get('symbol')} at "
                         f"{a.get('price')} ({a.get('order_status')})")
        else:
            lines.append(f"  {when} {a.get('activity_type')} {a.get('symbol') or ''} {a.get('net_amount') or a.get('qty') or ''} "
                         f"{a.get('description') or ''}".rstrip())
    lines.append("Log: " + (", ".join(f"{r['symbol']} {r.get('ret', 0):+.1%}" for r in state.get("log", [])) or "no trades"))

    symbols = coins(broker, state, now.date())
    start = (now - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"[crypto] 15-minute bars for {len(symbols)} coins since {start[:10]}...")
    df = crypto.fetch(data, symbols, start=start)
    if df.empty:
        return "\n".join(lines + ["", "No crypto bars came back."])
    df = df.sort_values(["symbol", "t"]).reset_index(drop=True)
    recent, rows = [], []
    for sym, g in df.groupby("symbol", sort=True):
        g = g.reset_index(drop=True)
        sig = crypto.signals(g)
        c, t = g["close"].to_numpy(float), pd.to_datetime(g["t"], utc=True)
        hour = np.full(len(c), np.nan)
        hour[4:] = c[4:] / c[:-4] - 1
        last3 = (t >= pd.Timestamp(now) - pd.Timedelta(days=3)).to_numpy()
        if last3.any() and np.isfinite(hour[last3]).any():
            i = int(np.nanargmin(np.where(last3, hour, np.nan)))
            recent.append((float(hour[i]), sym, t.iloc[i]))
        for name in ("dip", "dip 3%", "dip 4%", "big dip"):
            tr = crypto.trades(g, sig[name], crypto.HOLDS["1 day"])
            rows += [(name, pd.Timestamp(x), r) for x, r in zip(tr["t"], tr["ret"])]
    lines += ["", f"Biggest one-hour falls in the last 3 days ({len(recent)} coins; the bot buys at -3%):"]
    lines += [f"  {s.split('/')[0]:<8} {m:+.1%} (at {w:%a %H:%M} UTC)" for m, s, w in sorted(recent)[:12]]
    tr = pd.DataFrame(rows, columns=["rule", "t", "ret"])
    if not tr.empty:
        tr["week"] = pd.to_datetime(tr["t"], utc=True).dt.strftime("%G-W%V")
        names = {"dip": "2%+", "dip 3%": "3%+", "dip 4%": "4%+", "big dip": "5%+"}
        lines += ["", f"Dips bought and sold a day later, last {days} days (all coins, one at a time per coin; "
                      "per trade before fees, fees about 0.3-0.5% a trade):",
                  "  week      " + "  ".join(f"{names[n]:>15}" for n in names)]
        for week, w in tr.groupby("week"):
            cells = []
            for n in names:
                r = w.loc[w["rule"] == n, "ret"]
                cells.append(f"{len(r):>3} {r.mean():+6.2%}" if len(r) else f"{0:>3} {'':>6}")
            lines.append(f"  {week}  " + "  ".join(f"{x:>15}" for x in cells))
        for n, label in names.items():
            r = tr.loc[tr["rule"] == n, "ret"]
            if len(r):
                lines.append(f"  all {label}: {len(r)} trades ({len(r) / days:.1f} a day), {r.mean():+.2%} a trade before fees, "
                             f"right {(r > 0).mean():.0%}")
    return "\n".join(lines)
