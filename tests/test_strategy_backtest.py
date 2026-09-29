import pytest

from trend_bot.backtest import run_backtest
from trend_bot.cli import main
from trend_bot.strategy import Breakout, MACrossover


def test_ma_cross_follows_trend(up_then_down):
    sig = MACrossover(fast=10, slow=50).generate(up_then_down)
    assert sig["position"].iloc[:49].eq(0).all()  # not enough history yet
    assert sig["position"].iloc[100] == 1          # uptrend
    assert sig["position"].iloc[-1] == 0           # downtrend


def test_ma_cross_rejects_bad_windows(up_then_down):
    with pytest.raises(ValueError):
        MACrossover(fast=50, slow=10).generate(up_then_down)


def test_rsi_filter_blocks_overbought_entry(up_then_down):
    # A smooth uptrend has RSI 100, so the filter blocks entry the whole way up...
    sig = MACrossover(fast=10, slow=50, rsi_max=70).generate(up_then_down)
    assert sig["position"].iloc[:300].eq(0).all()
    # ...but once RSI cools while the MAs are still bullish, it buys the dip.
    unfiltered = MACrossover(fast=10, slow=50).generate(up_then_down)
    assert 0 < sig["position"].sum() < unfiltered["position"].sum()


def test_breakout(up_then_down):
    sig = Breakout(entry=20, exit=10).generate(up_then_down)
    assert sig["position"].iloc[100] == 1
    assert sig["position"].iloc[-1] == 0


def test_backtest_beats_buy_hold_on_round_trip(up_then_down):
    sig = MACrossover(fast=10, slow=50).generate(up_then_down)
    res = run_backtest(sig, capital=10_000, cost_bps=0)
    s = res.stats
    assert s["buy_hold_return"] == pytest.approx(0, abs=1e-9)
    assert s["total_return"] > 0.3
    assert s["max_drawdown"] > s["buy_hold_max_drawdown"]
    assert len(res.trades) == 1 and not res.trades["open"].iloc[0]


def test_backtest_no_lookahead(up_then_down):
    # Always-long from day 0 should earn nothing on day 0 (can't trade before the first close).
    sig = up_then_down.assign(position=1)
    res = run_backtest(sig, cost_bps=0)
    assert res.equity.iloc[0] == 10_000
    assert res.stats["total_return"] == pytest.approx(res.stats["buy_hold_return"])


def test_costs_reduce_returns(up_then_down):
    sig = MACrossover(fast=10, slow=50).generate(up_then_down)
    free = run_backtest(sig, cost_bps=0).stats["total_return"]
    costly = run_backtest(sig, cost_bps=50).stats["total_return"]
    assert costly < free


def test_cli_backtest_csv(up_then_down, tmp_path, capsys):
    path = tmp_path / "prices.csv"
    up_then_down.to_csv(path)
    assert main(["backtest", str(path), "--fast", "10", "--slow", "50", "--show-trades"]) == 0
    out = capsys.readouterr().out
    assert "Total return" in out and "Buy & hold" in out
