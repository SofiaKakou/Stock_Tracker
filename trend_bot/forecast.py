"""30-day outlook: the chance each stock goes up, and the chance it beats the market.

For every month-end in the history, each eligible stock's signals (the parts
of the Trend Score, plus the market weather) are paired with what happened
over the following month. A logistic regression learns how much each signal
shifted the odds. It's simple and transparent: every prediction can be
explained by the signals behind it.

Honesty check (walk-forward): each year is predicted by a model trained only
on the years before it, then compared with what actually happened.
"""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pandas as pd

from trend_bot import model
from trend_bot.db import get_meta, load_prices, set_meta

INPUTS = ["trend", "momentum", "high52", "steadiness", "insiders", "weather"]
TARGETS = {"up": "goes up", "beat": "beats the S&P 500"}
REASONS = {
    "trend": ("in an uptrend", "in a downtrend"),
    "momentum": ("strong 6-12 month gains", "weak 6-12 month returns"),
    "high52": ("near its 52-week high", "far below its 52-week high"),
    "steadiness": ("steady price moves", "big daily swings"),
    "insiders": ("insiders buying", "insiders selling"),
    "weather": ("market in an uptrend", "market in a downtrend"),
}


# --- Logistic regression (small, dependency-free) -----------------------------------------

class Logit:
    """L2-regularized logistic regression on standardized inputs, fitted by Newton's method."""

    def __init__(self, l2: float = 1.0):
        self.l2 = l2

    def fit(self, X: np.ndarray, y: np.ndarray, iters: int = 30) -> "Logit":
        self.mean = X.mean(axis=0)
        self.std = X.std(axis=0)
        self.std[self.std == 0] = 1.0
        Z = np.column_stack([np.ones(len(X)), (X - self.mean) / self.std])
        w = np.zeros(Z.shape[1])
        penalty = np.full(Z.shape[1], self.l2)
        penalty[0] = 0.0  # don't shrink the intercept
        for _ in range(iters):
            p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
            grad = Z.T @ (y - p) - penalty * w
            hess = (Z * (p * (1 - p))[:, None]).T @ Z + np.diag(penalty)
            step = np.linalg.solve(hess, grad)
            w += step
            if np.abs(step).max() < 1e-8:
                break
        self.w = w
        return self

    def contributions(self, X: np.ndarray) -> np.ndarray:
        """Each input's push on the log-odds, relative to an average stock."""
        return ((X - self.mean) / self.std) * self.w[1:]

    def predict(self, X: np.ndarray) -> np.ndarray:
        z = self.w[0] + self.contributions(X).sum(axis=1)
        return 1 / (1 + np.exp(-np.clip(z, -30, 30)))

    def to_json(self) -> dict:
        return {"l2": self.l2, "w": self.w.tolist(), "mean": self.mean.tolist(), "std": self.std.tolist()}

    @classmethod
    def from_json(cls, d: dict) -> "Logit":
        m = cls(d["l2"])
        m.w, m.mean, m.std = (np.array(d[k]) for k in ("w", "mean", "std"))
        return m


# --- Training data ----------------------------------------------------------------------------

def training_rows(con: sqlite3.Connection, universe: int = 1000, min_price: float = 5,
                  panel: dict[str, pd.DataFrame] | None = None, benchmark: str = "SPY") -> pd.DataFrame:
    """One row per (month-end, eligible stock): its signals and next month's outcome."""
    panel = panel or model.monthly_panel(con)
    months = panel["close"].index
    buyers, sellers = model.insider_counts(con, months)
    spy = load_prices(con, benchmark)["Close"]
    weather = model._monthly_last(model.spy_weather(spy))
    spy_m = model._monthly_last(spy)
    spy_next = spy_m.shift(-1) / spy_m - 1
    nxt = panel["close"].shift(-1) / panel["close"] - 1
    parts = []
    for month in months[:-1]:
        feat = pd.DataFrame({f: panel[f].loc[month] for f in model.FEATURES})
        feat = feat[model.eligible(feat, universe, min_price)]
        if len(feat) < 30 or pd.isna(spy_next.get(month)):
            continue
        s = model.score(feat, buyers.loc[month] if month in buyers.index else pd.Series(dtype=float),
                        sellers.loc[month] if month in sellers.index else pd.Series(dtype=float))
        s["weather"] = float(bool(weather.get(month, True)))
        s["ret"] = nxt.loc[month, s.index]
        s["spy_ret"] = spy_next[month]
        s["month"] = month
        parts.append(s.dropna(subset=["ret"]).rename_axis("ticker").reset_index())
    rows = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if len(rows):
        rows["up"] = (rows["ret"] > 0).astype(float)
        rows["beat"] = (rows["ret"] > rows["spy_ret"]).astype(float)
    return rows


def fit(rows: pd.DataFrame) -> dict[str, Logit]:
    X = rows[INPUTS].to_numpy(float)
    return {t: Logit().fit(X, rows[t].to_numpy(float)) for t in TARGETS}


# --- Honesty check ------------------------------------------------------------------------------

def walk_forward(rows: pd.DataFrame, first_year: int = 2011) -> pd.DataFrame:
    """Predict each year with models trained only on earlier years."""
    out = []
    years = sorted(rows["month"].dt.year.unique())
    for year in [y for y in years if y >= first_year]:
        train = rows[rows["month"].dt.year < year]
        test = rows[rows["month"].dt.year == year].copy()
        if len(train) < 1000 or test.empty:
            continue
        models = fit(train)
        X = test[INPUTS].to_numpy(float)
        for t, m in models.items():
            test[f"p_{t}"] = m.predict(X)
            test[f"base_{t}"] = train[t].mean()  # the "no skill" guess: the historical average
        out.append(test)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def scorecard(preds: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Accuracy and calibration of out-of-sample predictions, and how ranked ideas did."""
    summary = []
    for t, label in TARGETS.items():
        y, p, base = preds[t], preds[f"p_{t}"], preds[f"base_{t}"]
        always = (y == (base > 0.5)).mean()  # always guessing the more common outcome
        summary.append({
            "prediction": f"30 days: {label}",
            "happened": y.mean(),
            "accuracy": ((p > 0.5) == y.astype(bool)).mean(),
            "naive_accuracy": always,
            "brier": ((p - y) ** 2).mean(),
            "naive_brier": ((base - y) ** 2).mean(),
        })
    bins = [0, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 1]
    calib = {}
    for t in TARGETS:
        b = pd.cut(preds[f"p_{t}"], bins)
        calib[t] = preds.groupby(b, observed=True).agg(predictions=(t, "size"), predicted=(f"p_{t}", "mean"),
                                                       happened=(t, "mean"))
    ranks = []
    for month, g in preds.groupby("month"):
        k = max(1, len(g) // 10)
        g = g.sort_values("p_beat", ascending=False)
        ranks.append({"month": month, "top10": g["ret"].head(k).mean(), "bottom10": g["ret"].tail(k).mean(),
                      "all": g["ret"].mean(), "spy": g["spy_ret"].iloc[0]})
    ranks = pd.DataFrame(ranks).set_index("month")
    deciles = pd.DataFrame({
        "avg_month": ranks[["top10", "all", "bottom10", "spy"]].mean(),
        "beat_spy_months": (ranks[["top10", "all", "bottom10", "spy"]].sub(ranks["spy"], axis=0) > 0).mean(),
    }).rename(index={"top10": "Top 10% ideas", "all": "All eligible", "bottom10": "Bottom 10%", "spy": "S&P 500"})
    deciles.loc["S&P 500", "beat_spy_months"] = np.nan
    return {"summary": pd.DataFrame(summary).set_index("prediction"), "calibration_up": calib["up"],
            "calibration_beat": calib["beat"], "ideas": deciles, "top_beat_bottom": (ranks["top10"] > ranks["bottom10"]).mean()}


# --- Live predictions ------------------------------------------------------------------------------

def load_models(con: sqlite3.Connection, retrain: bool = False, universe: int = 1000,
                log=print) -> tuple[dict[str, Logit], str]:
    """Models fitted on all history; retrained on the first run of each month (cached in the database)."""
    latest = con.execute("SELECT MAX(last_date) FROM tickers").fetchone()[0]
    month = latest[:7]
    cached = get_meta(con, "forecast_models")
    if cached and not retrain:
        d = json.loads(cached)
        if d.get("month") == month and d.get("inputs") == INPUTS:
            return {t: Logit.from_json(v) for t, v in d["models"].items()}, d["trained_on"]
    log("[forecast] learning from the full history (once a month, takes a few minutes)...")
    rows = training_rows(con, universe=universe)
    models = fit(rows)
    trained_on = f"{rows['month'].min():%Y-%m} to {rows['month'].max():%Y-%m}, {len(rows):,} stock-months"
    set_meta(con, "forecast_models", json.dumps({"month": month, "inputs": INPUTS, "trained_on": trained_on,
                                                 "models": {t: m.to_json() for t, m in models.items()}}))
    con.commit()
    return models, trained_on


def _reasons(contrib: np.ndarray, z: np.ndarray, n: int = 2) -> str:
    """The signals that moved this stock's odds most, described by what they show.

    The market weather is left out: it's the same for every stock on a given day,
    so it never explains why one stock ranks above another.
    """
    order = [i for i in np.argsort(-np.abs(contrib)) if INPUTS[i] != "weather"][:n]
    out = []
    for i in order:
        high, low = REASONS[INPUTS[i]]
        out.append(high if z[i] > 0 else low)  # above or below the average stock
    return ", ".join(out)


def predict_today(con: sqlite3.Connection, models: dict[str, Logit], universe: int = 1000,
                  benchmark: str = "SPY") -> tuple[pd.DataFrame, str, bool]:
    """(predictions sorted by chance to beat the market, date, market weather is 'invest')."""
    scores, latest = model.today_scores(con, universe)
    invest = bool(model.spy_weather(load_prices(con, benchmark)["Close"]).iloc[-1])
    if scores.empty:
        return scores, latest, invest
    scores["weather"] = float(invest)
    X = scores[INPUTS].to_numpy(float)
    out = scores[["close", "score"]].copy()
    out["p_up"] = models["up"].predict(X)
    out["p_beat"] = models["beat"].predict(X)
    contrib = models["beat"].contributions(X)
    z = (X - models["beat"].mean) / models["beat"].std
    out["why"] = [_reasons(c, z[i]) for i, c in enumerate(contrib)]
    return out.sort_values("p_beat", ascending=False), latest, invest


# --- Remembering how reliable the outlook has been ------------------------------------------------

def save_scorecard(con: sqlite3.Connection, preds: pd.DataFrame, card: dict) -> dict:
    s, ideas = card["summary"], card["ideas"]
    beat = s.loc["30 days: beats the S&P 500"]
    up = s.loc["30 days: goes up"]
    sc = {
        "tested": f"{preds['month'].min():%Y}-{preds['month'].max():%Y}",
        "beat_accuracy": float(beat["accuracy"]), "beat_naive": float(beat["naive_accuracy"]),
        "up_accuracy": float(up["accuracy"]), "up_naive": float(up["naive_accuracy"]),
        "top_vs_all": float(ideas.loc["Top 10% ideas", "avg_month"] - ideas.loc["All eligible", "avg_month"]),
        "bottom_vs_all": float(ideas.loc["Bottom 10%", "avg_month"] - ideas.loc["All eligible", "avg_month"]),
        "top_beat_bottom": float(card["top_beat_bottom"]),
    }
    set_meta(con, "forecast_scorecard", json.dumps(sc))
    con.commit()
    return sc


def load_scorecard(con: sqlite3.Connection) -> dict | None:
    raw = get_meta(con, "forecast_scorecard")
    return json.loads(raw) if raw else None


def reliability_note(sc: dict | None) -> str:
    """One plain sentence on how the outlook did in its honesty check."""
    if not sc:
        return "Not tested yet: run 'python -m trend_bot study forecast' to see how reliable these lists have been."
    skill = sc["beat_accuracy"] > sc["beat_naive"]
    parts = [f"Tested {sc['tested']}: 'beats the S&P 500' was right {sc['beat_accuracy']:.0%} of the time "
             f"(always guessing the common outcome: {sc['beat_naive']:.0%})" + ("" if skill else ", so no real skill"),
             f"the 'likely to lag' group trailed the average stock by {-sc['bottom_vs_all']:.1%} a month"
             if sc["bottom_vs_all"] < 0 else "the 'likely to lag' group did NOT trail the average stock",
             f"the top ideas {'beat' if sc['top_vs_all'] > 0 else 'did not beat'} the average stock "
             f"({sc['top_vs_all']:+.1%} a month)"]
    return "; ".join(parts) + "."
