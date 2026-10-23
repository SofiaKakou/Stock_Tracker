"""Bad times, in plain words: how a strategy did when things went wrong.

Averages hide the experience of holding something. These numbers describe the bad stretches
(ideas from passivbot, which judges strategies by their worst periods, not their average):

- longest time to recover: the longest stretch spent below a previous high, counting a slump that
  hasn't recovered yet (a strategy still under water at the end isn't let off the hook)
- worst months: the average of the worst 5% of months
- without the best months: the yearly growth if the 5 best months had been flat, which shows
  whether the result came from a few lucky months or was spread out
"""

from __future__ import annotations

import numpy as np
import pandas as pd

WORST_SHARE = 0.05
BEST_MONTHS = 5


def underwater(r: pd.Series) -> dict:
    """Longest stretch (in months) below a previous high, and whether it's still going at the end."""
    value = (1 + r.dropna()).cumprod().to_numpy()
    longest = current = 0
    peak = 1.0
    for v in value:
        if v >= peak:
            peak, current = v, 0
        else:
            current += 1
            longest = max(longest, current)
    return {"months": int(longest), "still_under": bool(current > 0 and current == longest), "now": int(current)}


def worst_months(r: pd.Series, share: float = WORST_SHARE) -> float:
    """Average of the worst `share` of months (at least one)."""
    x = np.sort(r.dropna().to_numpy())
    k = max(1, int(round(len(x) * share)))
    return float(x[:k].mean()) if len(x) else float("nan")


def without_best(r: pd.Series, k: int = BEST_MONTHS) -> float:
    """Yearly growth with the `k` best months set to zero."""
    x = r.dropna().copy()
    if len(x) <= k:
        return float("nan")
    x[x.nlargest(k).index] = 0.0
    return float((1 + x).prod() ** (12 / len(x)) - 1)


def summary(r: pd.Series) -> dict:
    x = r.dropna()
    u = underwater(x)
    return {"recover_months": u["months"], "still_under": u["still_under"], "under_now": u["now"],
            "worst_months": worst_months(x), "yearly": float((1 + x).prod() ** (12 / len(x)) - 1) if len(x) else np.nan,
            "without_best": without_best(x)}


def span(months: int) -> str:
    y, m = divmod(int(months), 12)
    parts = ([f"{y} year{'s' if y != 1 else ''}"] if y else []) + ([f"{m} month{'s' if m != 1 else ''}"] if m else [])
    return " ".join(parts) or "under a month"


def words(s: dict, name: str = "It") -> list[str]:
    """Plain sentences for one summary()."""
    if not s or s.get("yearly") != s.get("yearly"):
        return []
    rec = (f"{name} once spent {span(s['recover_months'])} below a previous high before recovering."
           if not s["still_under"] else
           f"{name} has been below its previous high for {span(s['recover_months'])} and hasn't recovered yet.")
    now = ([f"Right now it is {span(s['under_now'])} below its last high."]
           if s.get("under_now") and not s["still_under"] else [])
    return [rec, *now,
            f"Its worst months (the worst 5%) averaged {s['worst_months']:+.1%}.",
            f"Without its {BEST_MONTHS} best months it would have grown {s['without_best']:+.1%} a year instead of "
            f"{s['yearly']:+.1%}."]
