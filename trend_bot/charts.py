"""The growth chart: what $10,000 would have become in the tested portfolio vs an S&P 500 fund.

Two forms of the same picture: an inline SVG for the report (self-contained, follows light/dark
mode) and a PNG for the email (Gmail doesn't show inline SVG).
"""

from __future__ import annotations

import io
from html import escape

import pandas as pd

START = 10_000
LABELS = {"portfolio": "Top-ideas portfolio (tested)", "spy": "S&P 500 index fund"}


def _money(v: float) -> str:
    return f"${v / 1e6:,.1f}M" if v >= 1e6 else f"${v / 1e3:,.0f}K"


def growth_svg(curve: pd.DataFrame, width: int = 640, height: int = 260) -> str:
    """Two lines (portfolio, S&P 500) on a log scale, so equal % moves look equal across the years."""
    import numpy as np

    left, right, top, bottom = 52, 12, 12, 26
    vals = curve[["portfolio", "spy"]] * START
    lo, hi = float(np.log(vals.min().min())), float(np.log(vals.max().max()))
    span = (hi - lo) or 1.0
    n = len(vals)
    x = lambda i: left + (width - left - right) * i / max(n - 1, 1)
    y = lambda v: top + (height - top - bottom) * (1 - (np.log(v) - lo) / span)
    lines = []
    for col, style in (("spy", 'stroke="var(--muted)" stroke-width="2"'),
                       ("portfolio", 'stroke="var(--accent)" stroke-width="2.5"')):
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals[col]))
        lines.append(f'<polyline points="{pts}" fill="none" {style} stroke-linejoin="round"/>')
    ticks = []
    for v in (START * k for k in (1, 2, 5, 10, 20, 50, 100)):
        if np.log(v) < lo - 0.01 or np.log(v) > hi + 0.01:
            continue
        ticks.append(f'<line x1="{left}" x2="{width - right}" y1="{y(v):.1f}" y2="{y(v):.1f}" '
                     f'stroke="var(--line)"/><text x="{left - 6}" y="{y(v) + 4:.1f}" text-anchor="end" '
                     f'font-size="11" fill="var(--ink-2)">{_money(v)}</text>')
    years = sorted({d.year for d in vals.index})
    step = max(1, len(years) // 6)
    for yr in years[::step]:
        i = next(k for k, d in enumerate(vals.index) if d.year == yr)
        ticks.append(f'<text x="{x(i):.1f}" y="{height - 6}" text-anchor="middle" font-size="11" '
                     f'fill="var(--ink-2)">{yr}</text>')
    end = vals.iloc[-1]
    legend = (f'<div class="note" style="margin-top:4px"><span style="color:var(--accent)">━</span> '
              f'{escape(LABELS["portfolio"])}: {_money(end["portfolio"])} &nbsp; '
              f'<span style="color:var(--muted)">━</span> {escape(LABELS["spy"])}: {_money(end["spy"])}</div>')
    return (f'<div class="scroll"><svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px" '
            f'role="img" aria-label="Growth of $10,000: portfolio {_money(end["portfolio"])}, '
            f'S&amp;P 500 {_money(end["spy"])}">{"".join(ticks)}{"".join(lines)}</svg></div>{legend}')


def growth_png(curve: pd.DataFrame) -> bytes:
    """The same chart as a PNG (for email)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, LogLocator

    vals = curve[["portfolio", "spy"]] * START
    fig, ax = plt.subplots(figsize=(6.4, 2.8), dpi=150)
    ax.plot(vals.index, vals["spy"], color="#8d8c86", lw=1.8, label=f"{LABELS['spy']}: {_money(vals['spy'].iloc[-1])}")
    ax.plot(vals.index, vals["portfolio"], color="#2a78d6", lw=2.2,
            label=f"{LABELS['portfolio']}: {_money(vals['portfolio'].iloc[-1])}")
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: _money(v)))
    ax.yaxis.set_minor_formatter(FuncFormatter(lambda v, _: ""))
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    ax.tick_params(labelsize=8)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
