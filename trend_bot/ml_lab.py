"""ML lab: six ways to make the model beat the simple mix, tested the same honest way.

The all-signal model (ml.py) has never beaten a plain equal-weight average of the same signals
on years it never saw. These variants borrow ideas from freqtrade's FreqAI (and two of our own)
and are judged exactly like the model: each year predicted by a model trained only on earlier
years, ranking quality (IC) compared with the simple mix in both halves of the history, with
the luck bar (t above 2). A variant that wins is only offered; nothing changes without a yes.

  base        the current model, for reference
  recent      recent years count more in training (FreqAI weight_factor): a year 4 years back counts
              half as much
  regularized a little random noise added to the training inputs, and overlapping signals merged
              into fewer independent ones (FreqAI noise_standard_deviation + PCA)
  abstain     where a stock looks unlike anything the model trained on, use the simple mix's
              score instead (FreqAI's Dissimilarity Index and do_predict flag)
  confident   use the model only where its prediction is far from the month's average; elsewhere
              the simple mix (FreqAI's mean +- 1.25 std thresholds)
  classify    predict "beats the month's middle stock: yes/no" instead of the exact rank
  blend       half model, half simple mix
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from trend_bot import factors, ml

VARIANTS = ["base", "recent", "regularized", "abstain", "confident", "classify", "blend"]
RECENT_HALF_LIFE = 4.0      # years
NOISE = 0.05                # on 0-1 ranked inputs
PCA_VARIANCE = 0.99         # keep components explaining this share of the variance
DI_THRESHOLD = 1.0          # FreqAI's example value
DI_SAMPLE = 4000            # training rows the distances are measured against
CONFIDENT_Z = 1.25


def _filled(X: pd.DataFrame) -> np.ndarray:
    return X.fillna(0.5).to_numpy(dtype=float)       # ranks: missing = middle


def pca_fit(X: np.ndarray, share: float = PCA_VARIANCE) -> tuple[np.ndarray, np.ndarray]:
    """(mean, components) keeping `share` of the variance."""
    mean = X.mean(axis=0)
    _, s, vt = np.linalg.svd(X - mean, full_matrices=False)
    var = s ** 2 / (s ** 2).sum()
    k = int(np.searchsorted(np.cumsum(var), share) + 1)
    return mean, vt[:k]


def dissimilarity(train: np.ndarray, test: np.ndarray, rng: np.random.Generator,
                  sample: int = DI_SAMPLE) -> np.ndarray:
    """FreqAI's Dissimilarity Index: distance to the nearest training point, divided by the
    average distance between training points. Above about 1: unlike anything seen in training."""
    ref = train[rng.choice(len(train), size=min(sample, len(train)), replace=False)]
    sq = (ref ** 2).sum(axis=1)
    sub = ref[rng.choice(len(ref), size=min(1000, len(ref)), replace=False)]
    pair = np.sqrt(np.maximum((sub ** 2).sum(1)[:, None] + (sub ** 2).sum(1)[None, :] - 2 * sub @ sub.T, 0))
    avg = pair[np.triu_indices(len(sub), k=1)].mean()
    out = np.empty(len(test))
    for i in range(0, len(test), 2000):
        t = test[i:i + 2000]
        d = (t ** 2).sum(1)[:, None] + sq[None, :] - 2 * t @ ref.T
        out[i:i + 2000] = np.sqrt(np.maximum(d.min(axis=1), 0)) / avg
    return out


def _train(X, y, trees: int, weight=None, objective: str = "regression"):
    import lightgbm as lgb

    params = dict(ml.PARAMS, objective=objective)
    return lgb.train(params, lgb.Dataset(X, y, weight=weight, free_raw_data=False), num_boost_round=trees)


def walk_forward(df: pd.DataFrame, first_year: int = 2014, trees: int = ml.TREES, log=print,
                 seed: int = 7) -> pd.DataFrame:
    """Out-of-sample scores of every variant (plus the simple mix), each year from earlier years only."""
    df = df.dropna(subset=["ret"]).reset_index(drop=True)
    listed = df["gone"].isna() if "gone" in df else pd.Series(True, index=df.index)
    X = ml.ranked(df)
    Xf = _filled(X)
    y = ml.target(df[listed])
    beats = (df["ret"] > df.groupby("month")["ret"].transform("median")).astype(float)
    mix = ml.simple_mix(df)
    rng = np.random.default_rng(seed)
    out = []
    for year in sorted(y for y in df["month"].dt.year.unique() if y >= first_year):
        tr = (df["month"].dt.year < year) & listed
        te = df["month"].dt.year == year
        if tr.sum() < 1000:
            continue
        ytr = y[tr[tr].index]
        part = df.loc[te, ["month", "ticker", "ret"]].copy()
        part["base"] = _train(X[tr], ytr, trees).predict(X[te])
        age = (year - df.loc[tr, "month"].dt.year - 0.5).to_numpy()
        part["recent"] = _train(X[tr], ytr, trees, weight=0.5 ** (age / RECENT_HALF_LIFE)).predict(X[te])
        noisy = Xf[tr.to_numpy()] + rng.normal(0, NOISE, size=Xf[tr.to_numpy()].shape)
        mean, comp = pca_fit(noisy)
        part["regularized"] = _train((noisy - mean) @ comp.T, ytr.to_numpy(), trees).predict(
            (Xf[te.to_numpy()] - mean) @ comp.T)
        part["classify"] = _train(X[tr], beats[tr], trees, objective="binary").predict(X[te])
        part["di"] = dissimilarity(Xf[tr.to_numpy()], Xf[te.to_numpy()], rng)
        out.append(part)
        log(f"[ml lab] {year}: trained on {int(tr.sum()):,} rows, predicted {int(te.sum()):,}")
    if not out:
        return pd.DataFrame()
    preds = pd.concat(out)
    preds["simple_mix"] = mix.loc[preds.index]
    return combine(preds)


def combine(preds: pd.DataFrame) -> pd.DataFrame:
    """The variants that mix the model with the simple mix, on a within-month rank scale."""
    rank = lambda c: preds.groupby("month")[c].rank(pct=True)
    model_r, mix_r = rank("base"), rank("simple_mix")
    preds["abstain"] = model_r.where(preds["di"] <= DI_THRESHOLD, mix_r)
    z = preds.groupby("month")["base"].transform(lambda s: (s - s.mean()) / (s.std() or 1))
    preds["confident"] = model_r.where(z.abs() >= CONFIDENT_Z, mix_r)
    preds["blend"] = 0.5 * model_r + 0.5 * mix_r
    return preds


def scorecard(preds: pd.DataFrame, split_year: int = 2020) -> pd.DataFrame:
    """Per variant: IC and t overall, IC in each half, the simple mix's IC in each half, and a verdict."""
    halves = {"before": preds[preds["month"].dt.year < split_year],
              "after": preds[preds["month"].dt.year >= split_year]}
    mix = {h: factors.evaluate(p, "simple_mix")["mean_ic"] for h, p in halves.items()}
    rows = {}
    for v in VARIANTS + ["simple_mix"]:
        if v not in preds:
            continue
        whole = factors.evaluate(preds, v)
        r = {"ic": whole["mean_ic"], "ic_t": whole["ic_t"]}
        for h, p in halves.items():
            r[f"ic_{h}"] = factors.evaluate(p, v)["mean_ic"]
        p = ml.top_portfolio(preds, v)
        if len(p):
            r["top10_per_year"] = (1 + p["net"]).prod() ** (12 / len(p)) - 1
        if v != "simple_mix":
            wins = all(r[f"ic_{h}"] > mix[h] for h in halves)
            r["verdict"] = ("BEATS the simple mix" if wins and r["ic_t"] > 2 else
                            "beats it, but could be luck" if wins else "doesn't beat the simple mix")
        else:
            r["verdict"] = "(the bar to beat)"
        rows[v] = r
    return pd.DataFrame(rows).T


def text(card: pd.DataFrame, split_year: int) -> str:
    f = lambda v, s: "–" if v is None or (isinstance(v, float) and np.isnan(v)) else s.format(v)
    lines = [f"  {'variant':<12}{'IC':>8}{'t':>6}{'before ' + str(split_year):>13}{str(split_year) + ' on':>9}"
             f"{'top 10%/yr':>12}  verdict"]
    for v, r in card.iterrows():
        lines.append(f"  {v:<12}{f(r['ic'], '{:+.3f}'):>8}{f(r['ic_t'], '{:+.1f}'):>6}"
                     f"{f(r['ic_before'], '{:+.3f}'):>13}{f(r['ic_after'], '{:+.3f}'):>9}"
                     f"{f(r.get('top10_per_year'), '{:+.1%}'):>12}  {r['verdict']}")
    winners = [v for v, r in card.iterrows() if r["verdict"] == "BEATS the simple mix"]
    lines.append("\n" + (f"Winner(s): {', '.join(winners)}. Beat the simple mix on years they never saw, in both "
                         "halves, and unlikely to be luck. Offered to you; nothing changes without your yes."
                         if winners else
                         "No variant beat the simple mix in both halves with enough certainty. The simple mix "
                         "stays: averaging the signals is as good as the fancier models, which is useful to know."))
    return "\n".join(lines)
