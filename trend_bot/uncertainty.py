"""How sure are the headline numbers? Likely ranges from a block bootstrap.

A tested "+13.8% a year" is one draw of history. To see how much it could have differed, the
monthly returns are re-drawn thousands of times in one-year blocks (so good and bad stretches
stay together, as they do in real markets), and the yearly growth recomputed each time. The
middle 90% of those results is the "likely range". The same is done for the gap to the S&P 500:
if its range includes zero, the test can't tell which one was really better.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DRAWS = 2000
BLOCK = 12          # months
LEVEL = 0.90


def _yearly(r: np.ndarray) -> np.ndarray:
    """Yearly growth along the last axis."""
    return np.prod(1 + r, axis=-1) ** (12 / r.shape[-1]) - 1


def ranges(a: pd.Series, b: pd.Series | None = None, draws: int = DRAWS, block: int = BLOCK,
           level: float = LEVEL, seed: int = 0) -> dict:
    """Likely range of `a`'s yearly growth, and (with `b`, e.g. the S&P 500) of the gap a - b.
    Both are drawn with the same months, so their shared ups and downs cancel in the gap."""
    both = pd.concat([a, b], axis=1).dropna() if b is not None else a.dropna().to_frame()
    x = both.to_numpy(dtype=float)
    n = len(x)
    if n < 2 * block:
        return {}
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(draws, -(-n // block)))
    idx = ((starts[:, :, None] + np.arange(block)) % n).reshape(draws, -1)[:, :n]   # circular blocks
    sample = x[idx]                                                                    # draws x n x cols
    lo_q, hi_q = (1 - level) / 2, 1 - (1 - level) / 2
    ya = _yearly(sample[:, :, 0])
    out = {"value": float(_yearly(x[:, 0])), "lo": float(np.quantile(ya, lo_q)), "hi": float(np.quantile(ya, hi_q))}
    if b is not None:
        gap = ya - _yearly(sample[:, :, 1])
        out |= {"gap": float(_yearly(x[:, 0]) - _yearly(x[:, 1])), "gap_lo": float(np.quantile(gap, lo_q)),
                "gap_hi": float(np.quantile(gap, hi_q)), "gap_positive": float((gap > 0).mean())}
    return out


def gap_words(r: dict, name: str = "the S&P 500") -> str:
    """Plain words for the gap's range."""
    if not r or "gap" not in r:
        return ""
    span = f"{r['gap']:+.1%} a year vs {name} (likely between {r['gap_lo']:+.1%} and {r['gap_hi']:+.1%})"
    if r["gap_lo"] > 0:
        return f"{span}: ahead, and the range stays above zero, so probably really better."
    if r["gap_hi"] < 0:
        return f"{span}: behind, and the range stays below zero, so probably really worse."
    return f"{span}: the range includes zero, so the test can't tell which was really better."
