import json

import pandas as pd

from trend_bot import day_review as dr


def row(date, pattern, sym, side, entry, exit_, signal=None, time="10:00"):
    ret = side * (exit_ / entry - 1)
    return {"date": date, "time": time, "pattern": pattern, "symbol": sym, "side": side, "qty": 10,
            "signal": signal, "entry": entry, "exit": exit_, "ret": ret, "pnl": 10 * side * (exit_ - entry)}


class Data:
    def bars(self, symbols, start, end, timeframe):
        rows = []
        for s in symbols:   # AAA touches 102 at 10:30 then closes at 99; BBB never moves
            for m, (h, lo) in enumerate([(100.5, 99.5), (102.2, 100.0), (99.5, 98.8)]):
                t = pd.Timestamp(start) + pd.Timedelta(minutes=30 * (m + 1))
                rows.append((s, t.isoformat(), 100, h if s == "AAA" else 100, lo if s == "AAA" else 100, 100, 1))
        return pd.DataFrame(rows, columns=["symbol", "t", "open", "high", "low", "close", "volume"])


def test_the_review_reads_the_log_ranks_breakouts_and_checks_targets(tmp_path):
    log = [row("2026-11-02", "breakout", "AAA", 1, 100, 99, signal=0.9, time="09:45"),
           row("2026-11-02", "breakout", "BBB", 1, 100, 101, signal=0.1),
           row("2026-11-03", "gap", "AAA", 1, 100, 98, time="09:31"),
           {"date": "2026-11-03", "pattern": "gap", "symbol": "OPEN", "side": 1, "entry": 100, "exit": None,
            "ret": None, "pnl": None}]
    p = tmp_path / "daytrade.json"
    p.write_text(json.dumps({"log": log}))
    out = dr.run(Data(), str(p), log=lambda *_: None)
    assert "3 finished trades" in out and "Breakout from the first half hour: 2 trades" in out
    assert "top 5 a day: 2 trades" in out
    assert "sell at +2%" in out and "fade big opening gaps" in out
    assert dr.with_target(100, 1, 99, [100.5, 102.2], [99, 99], 0.02) == 102.0     # touched: sold at the target
    assert dr.with_target(100, -1, 101, [101, 101], [99.5, 99.5], 0.01) == 101     # short never reached -1%
    assert dr.run(None, str(tmp_path / "missing.json")).startswith("No day-trading results")
