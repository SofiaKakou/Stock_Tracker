"""Companies that disappeared (no prices today) are put back into the factor and model studies."""
import io
import json
import zipfile

import numpy as np
import pandas as pd
import pytest

from test_fundamentals import bulk_zip, company  # noqa: F401
from test_model import DATES, model_db  # noqa: F401
from trend_bot import factors, fates, fundamentals, ml

GONE_CIK, SMALL_CIK = 999, 998


def with_public_float(doc, value, years):
    doc["facts"]["dei"]["EntityPublicFloat"] = {"units": {"USD": [
        {"val": value, "end": f"{y}-06-30", "filed": f"{y + 1}-02-15", "form": "10-K", "fy": y, "fp": "FY"}
        for y in years]}}
    return doc


def zip_with_gone(years):
    buf = io.BytesIO(bulk_zip(years))
    with zipfile.ZipFile(buf, "a") as z:
        z.writestr(f"CIK{GONE_CIK:010d}.json", json.dumps(with_public_float(company(GONE_CIK, years, 0.02), 2e9, years)))
        z.writestr(f"CIK{SMALL_CIK:010d}.json", json.dumps(with_public_float(company(SMALL_CIK, years, 0.02), 1e8, years)))
    return buf.getvalue()


@pytest.fixture
def gone_db(model_db, tmp_path, monkeypatch):
    years = list(range(DATES[0].year - 1, DATES[-1].year))
    data = zip_with_gone(years)
    monkeypatch.setattr(fundamentals.insiders, "CACHE_DIR", tmp_path / "sec")
    fundamentals.update_fundamentals(model_db, download=lambda p: (p.parent.mkdir(parents=True, exist_ok=True),
                                                                   p.write_bytes(data), p)[-1],
                                     log=lambda *_: None)
    return model_db


def test_big_untracked_companies_are_kept_small_ones_dropped(gone_db):
    ciks = {c for (c,) in gone_db.execute("SELECT DISTINCT cik FROM facts")}
    assert GONE_CIK in ciks and SMALL_CIK not in ciks and 1 in ciks


def test_their_fate_gets_looked_up(gone_db):
    assert GONE_CIK in fates.companies_to_check(gone_db)
    gone_db.execute("INSERT INTO company_fates VALUES (?, 'Gone Co', 'bankrupt', '2025-06-10', '2026-01-01')",
                    (GONE_CIK,))
    assert GONE_CIK not in fates.companies_to_check(gone_db)   # a final outcome is never re-checked


def test_factor_and_model_studies_put_them_back(gone_db, tmp_path, monkeypatch, capsys):
    from trend_bot.cli import main

    when = DATES[600]
    gone_db.execute("INSERT INTO company_fates VALUES (?, 'Gone Co', 'bankrupt', ?, '2026-01-01')",
                    (GONE_CIK, str(when.date())))
    gone_db.commit()
    df = factors.monthly_factors(gone_db, since=str(DATES[260].date()), log=lambda *_: None, extras=True)
    row = df[df["ticker"] == f"CIK{GONE_CIK}"]
    assert len(row) == 1
    r = row.iloc[0]
    assert r["gone"] == "bankrupt" and r["ret"] == -1.0
    assert r["month"] < when <= r["month"] + pd.offsets.MonthEnd(1)       # the month before it disappeared
    assert not np.isnan(r["gross_profitability"]) and np.isnan(r["mom12"])  # no prices for it
    assert df.loc[df["ticker"] != f"CIK{GONE_CIK}", "gone"].isna().all()

    res = factors.study(df, split_year=int(df["month"].dt.year.max()))
    cmp = res[factors.GONE_TABLE]
    # Its terrible profitability now lines up with a -100% month: the signal looks better.
    assert cmp.loc["gross_profitability", "ic_with_gone"] >= cmp.loc["gross_profitability", "ic_listed_only"]

    trained = []
    real_train = ml.train
    def spy_train(X, y, trees=ml.TREES):
        trained.append(len(X))
        return real_train(X, y, trees)
    ml_df = df.assign(insider_buyers=0.0, insider_sellers=0.0)
    for c in ml.FEATURES:
        if c not in ml_df:
            ml_df[c] = np.nan
    monkeypatch.setattr(ml, "train", spy_train)
    preds = ml.walk_forward(ml_df, first_year=int(df["month"].dt.year.max()), trees=10, log=lambda *_: None)
    monkeypatch.setattr(ml, "train", real_train)
    year = int(df["month"].dt.year.max())
    listed_before = ((ml_df["month"].dt.year < year) & ml_df["gone"].isna()).sum()
    assert trained == [listed_before]                     # never trained on the company that disappeared
    if r["month"].year == year:
        assert f"CIK{GONE_CIK}" in set(preds["ticker"])   # but it is scored

    capsys.readouterr()
    assert main(["study", "factors", "--db", str(tmp_path / "market.db"), "--since", str(DATES[260].date())]) == 0
    out = capsys.readouterr().out
    assert "1 bankrupt (-100%)" in out and "listed today only vs with companies that disappeared" in out


def test_fates_catch_up_in_one_go(gone_db, tmp_path, monkeypatch, capsys):
    from trend_bot.cli import main

    doc = {"name": "Gone Co", "filings": {"recent": {"form": ["10-K", "8-K"], "filingDate": ["2024-02-15", "2025-06-10"],
                                                      "items": ["", "1.03"]}}}
    monkeypatch.setattr(fates.insiders, "_get", lambda url: json.dumps(doc).encode())
    assert main(["db", "update", "--fates-only", "--sec-lookups", "20000", "--db", str(tmp_path / "market.db")]) == 0
    out = capsys.readouterr().out
    assert "bankrupt 1" in out and "all caught up" in out
    assert gone_db.execute("SELECT status, fate_date FROM company_fates WHERE cik = ?",
                           (GONE_CIK,)).fetchone() == ("bankrupt", "2025-06-10")


def test_monthly_table_is_built_once_per_data_version(gone_db, monkeypatch):
    builds = []
    real = factors.monthly_factors
    monkeypatch.setattr(factors, "monthly_factors", lambda *a, **k: builds.append(1) or real(*a, **k))
    quiet = dict(log=lambda *_: None)
    assert factors.table(gone_db, build=False, **quiet) is None       # nothing built yet
    first = factors.table(gone_db, **quiet)
    again = factors.table(gone_db, since=str(DATES[400].date()), **quiet)
    assert len(builds) == 1                                            # second study reuses it
    assert again["month"].min() >= DATES[400] and len(again) < len(first)
    assert first["ret"].isna().any()                                   # includes the latest month
    gone_db.execute("INSERT INTO company_fates VALUES (?, 'Gone Co', 'bankrupt', ?, '2026-01-01')",
                    (GONE_CIK, str(DATES[600].date())))
    gone_db.commit()
    factors.table(gone_db, **quiet)
    assert len(builds) == 2                                            # new fate: rebuilt
    folder = fundamentals.insiders.CACHE_DIR.parent / "factors"
    assert len(list(folder.glob("table_*.pkl"))) == 1                  # old versions removed
