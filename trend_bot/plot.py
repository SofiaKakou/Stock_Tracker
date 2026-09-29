"""Backtest charts: price with the strategy's lines and trades, plus equity curves."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # render to file; no GUI window needed
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

from trend_bot.backtest import BacktestResult
from trend_bot.portfolio import PortfolioResult

INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SURFACE = "#fcfcfb"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
VIOLET = "#4a3aa7"
NEUTRAL = "#8d8c86"
BUY = "#0ca30c"
SELL = "#d03b3b"
IN_MARKET = "#2a78d6"
# Fixed categorical order for tickers; more than 8 fold into "Other".
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
OTHER = "#b5b4ae"

# Indicator columns each strategy adds, with their legend label and color.
OVERLAYS = {
    "fast_ma": ("Fast MA", BLUE),
    "slow_ma": ("Slow MA", ORANGE),
    "upper": ("Entry channel (high)", BLUE),
    "lower": ("Exit channel (low)", ORANGE),
}


def _style(ax: plt.Axes) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)


def _use_log(series: pd.Series) -> bool:
    s = series.dropna()
    return len(s) > 0 and s.min() > 0 and s.max() / s.min() > 8


def _set_log(ax: plt.Axes) -> None:
    # Label 1-2-5 steps (10, 20, 50, 100, ...) so a log axis has enough readable ticks.
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
    ax.yaxis.set_minor_formatter(NullFormatter())


def _legend(ax: plt.Axes, ncol: int) -> None:
    # Above the plot area so it never covers the data.
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=ncol, fontsize=9, frameon=False,
              labelcolor=INK, borderaxespad=0.3, handlelength=1.6, columnspacing=1.6)


def plot_backtest(signals: pd.DataFrame, result: BacktestResult, title: str, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fig, (ax_p, ax_e) = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [3, 2], "hspace": 0.22},
    )
    fig.patch.set_facecolor(SURFACE)
    for ax in (ax_p, ax_e):
        _style(ax)

    # --- Price panel -------------------------------------------------------
    close = signals["Close"]
    pos = signals["position"]
    ax_p.fill_between(signals.index, 0, 1, where=pos.astype(bool), transform=ax_p.get_xaxis_transform(),
                      color=IN_MARKET, alpha=0.07, linewidth=0, step="post", label="In the market")
    ax_p.plot(close.index, close, color=INK, linewidth=1.2, label="Close")
    for col, (label, color) in OVERLAYS.items():
        if col in signals:
            ax_p.plot(signals.index, signals[col], color=color, linewidth=1.6, label=label)

    change = pos.diff().fillna(pos.iloc[0])
    buys, sells = signals.index[change > 0], signals.index[change < 0]
    ax_p.scatter(buys, close[buys], marker="^", s=90, color=BUY, edgecolor=SURFACE, linewidth=1.5,
                 zorder=5, label=f"Buy ({len(buys)})")
    ax_p.scatter(sells, close[sells], marker="v", s=90, color=SELL, edgecolor=SURFACE, linewidth=1.5,
                 zorder=5, label=f"Sell ({len(sells)})")

    if _use_log(close):
        _set_log(ax_p)
    ax_p.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}" if v >= 10 else f"{v:,.2f}"))
    ax_p.set_ylabel("Price", color=INK_2, fontsize=10)
    _legend(ax_p, ncol=6)

    # --- Equity panel ------------------------------------------------------
    s = result.stats
    ax_e.plot(result.buy_hold.index, result.buy_hold, color=NEUTRAL, linewidth=1.6,
              label=f"Buy & hold  {s['buy_hold_return']:+.0%}  (max drawdown {s['buy_hold_max_drawdown']:.0%})")
    ax_e.plot(result.equity.index, result.equity, color=VIOLET, linewidth=2,
              label=f"Strategy  {s['total_return']:+.0%}  (max drawdown {s['max_drawdown']:.0%})")
    if _use_log(pd.concat([result.equity, result.buy_hold])):
        _set_log(ax_e)
    ax_e.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax_e.set_ylabel("Portfolio value", color=INK_2, fontsize=10)
    _legend(ax_e, ncol=2)

    fig.suptitle(title, x=0.07, y=0.95, ha="left", fontsize=13, color=INK, fontweight="bold")
    fig.savefig(path, dpi=130, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return path


def plot_portfolio(result: PortfolioResult, title: str, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fig, (ax_e, ax_w) = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [3, 2], "hspace": 0.22},
    )
    fig.patch.set_facecolor(SURFACE)
    for ax in (ax_e, ax_w):
        _style(ax)

    # --- Portfolio value ---------------------------------------------------
    s = result.stats
    ax_e.plot(result.benchmark.index, result.benchmark, color=NEUTRAL, linewidth=1.6,
              label=f"Equal-weight buy & hold  {s['benchmark_return']:+.0%}  "
                    f"(max drawdown {s['benchmark_max_drawdown']:.0%})")
    ax_e.plot(result.equity.index, result.equity, color=INK, linewidth=2,
              label=f"Strategy portfolio  {s['total_return']:+.0%}  (max drawdown {s['max_drawdown']:.0%})")
    if _use_log(pd.concat([result.equity, result.benchmark])):
        _set_log(ax_e)
    ax_e.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax_e.set_ylabel("Portfolio value", color=INK_2, fontsize=10)
    _legend(ax_e, ncol=2)

    # --- What it held: stacked share of the portfolio per ticker ------------
    w = result.weights
    if w.shape[1] > len(CATEGORICAL):
        keep = w.mean().sort_values(ascending=False).index[: len(CATEGORICAL) - 1]
        w = w[keep].assign(Other=w.drop(columns=keep).sum(axis=1))
    colors = [OTHER if c == "Other" else CATEGORICAL[i] for i, c in enumerate(w.columns)]
    ax_w.stackplot(w.index, w.T.values, labels=w.columns, colors=colors, step="post",
                   edgecolor=SURFACE, linewidth=0.3)
    ax_w.set_ylim(0, 1)
    ax_w.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax_w.set_ylabel("Invested in (rest is cash)", color=INK_2, fontsize=10)
    _legend(ax_w, ncol=min(len(w.columns), 8))

    fig.suptitle(title, x=0.07, y=0.95, ha="left", fontsize=13, color=INK, fontweight="bold")
    fig.savefig(path, dpi=130, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return path
