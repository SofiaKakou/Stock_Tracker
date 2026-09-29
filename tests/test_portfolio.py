import numpy as np
import pytest

from conftest import make_prices
from trend_bot.cli import main
from trend_bot.portfolio import run_portfolio_backtest
from trend_bot.strategy import MACrossover


@pytest.fixture
def two_stocks(up_then_down):
    rising = make_prices(np.linspace(50, 150, len(up_then_down)))
    strat = MACrossover(fast=10, slow=50)
    return {"UPDOWN": strat.generate(up_then_down), "RISING": strat.generate(rising)}


def test_equal_weight_caps_each_slot(two_stocks):
    res = run_portfolio_backtest(two_stocks, weighting="equal", cost_bps=0)
    assert res.weights.max().max() == pytest.approx(0.5)
    assert res.weights.sum(axis=1).max() <= 1 + 1e-9


def test_active_weight_goes_all_in_on_the_trending_stock(two_stocks):
    res = run_portfolio_backtest(two_stocks, weighting="active", cost_bps=0)
    last = res.weights.iloc[-1]
    assert last["RISING"] == pytest.approx(1.0) and last["UPDOWN"] == 0
    assert res.stats["total_return"] > run_portfolio_backtest(two_stocks, "equal", cost_bps=0).stats["total_return"]


def test_benchmark_is_equal_weight_buy_hold(two_stocks):
    res = run_portfolio_backtest(two_stocks, cost_bps=0)
    assert set(res.per_ticker.index) == {"UPDOWN", "RISING"}
    assert res.stats["benchmark_return"] > 0  # half flat round trip, half tripling


def test_only_overlapping_dates_are_used(two_stocks):
    short = {k: v.iloc[100:] if k == "RISING" else v for k, v in two_stocks.items()}
    res = run_portfolio_backtest(short)
    assert res.equity.index[0] == two_stocks["RISING"].index[100]


def test_bad_weighting(two_stocks):
    with pytest.raises(ValueError):
        run_portfolio_backtest(two_stocks, weighting="yolo")


def test_cli_portfolio_with_plot(up_then_down, tmp_path, capsys):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    up_then_down.to_csv(a)
    make_prices(np.linspace(50, 150, 600)).to_csv(b)
    args = ["portfolio", str(a), str(b), "--fast", "10", "--slow", "50",
            "--plot", "--chart-dir", str(tmp_path / "charts"), "--no-open"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "Each ticker on its own" in out
    assert (tmp_path / "charts" / "PORTFOLIO_ma_cross_equal.png").exists()
