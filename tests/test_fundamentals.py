import io
import json
import zipfile

import numpy as np
import pandas as pd
import pytest

from test_model import DATES, N_STOCKS, model_db  # noqa: F401  (80 simulated stocks; S79 drifts up most)
from trend_bot import factors, fundamentals
from trend_bot.cli import main


def entry(val, end, filed, start=None, form="10-K"):
    e = {"val": val, "end": end, "filed": filed, "form": form, "fy": int(end[:4]), "fp": "FY"}
    if start:
        e["start"] = start
    return e


def company(cik, years, quality):
    """Annual financials for fiscal years ending Dec 31, filed mid-February."""
    usgaap = {k: {"units": {"USD": []}} for k in ("Revenues", "GrossProfit", "NetIncomeLoss",
                                                  "NetCashProvidedByUsedInOperatingActivities", "Assets",
                                                  "Liabilities", "StockholdersEquity")}
    for i, y in enumerate(years):
        start, end, filed = f"{y}-01-01", f"{y}-12-31", f"{y + 1}-02-15"
        assets = 1000.0 * (1.05 ** i)
        add = lambda tag, v, s=start: usgaap[tag]["units"]["USD"].append(entry(v, end, filed, s))
        add("Revenues", 800.0 * (1.03 ** i))
        add("GrossProfit", assets * quality)
        add("NetIncomeLoss", assets * quality / 3)
        add("NetCashProvidedByUsedInOperatingActivities", assets * quality / 2)
        add("Assets", assets, None)
        add("Liabilities", assets * 0.5, None)
        add("StockholdersEquity", assets * 0.5, None)
    usgaap["Revenues"]["units"]["USD"].append(entry(99.0, f"{years[-1]}-03-31", f"{years[-1]}-05-01",
                                                    f"{years[-1]}-01-01", "10-Q"))  # a quarter: ignored
    usgaap["Revenues"]["units"]["USD"].append(entry(5.0, f"{years[-1]}-12-31", f"{years[-1]}-12-31",
                                                    f"{years[-1]}-01-01", "8-K"))  # not a 10-K/10-Q: ignored
    dei = {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        entry(1_000_000.0, f"{y + 1}-01-31", f"{y + 1}-02-15") for y in years]}}}
    return {"cik": cik, "entityName": f"Co {cik}", "facts": {"us-gaap": usgaap, "dei": dei}}


def bulk_zip(years):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(N_STOCKS):
            # S79 (cik 80) is the most profitable, S00 the least, matching their price drift.
            z.writestr(f"CIK{i + 1:010d}.json", json.dumps(company(i + 1, years, 0.05 + 0.5 * i / N_STOCKS)))
    return buf.getvalue()


def test_parse_keeps_full_years_and_10k_10q_only():
    rows = fundamentals.parse_company(company(7, [2020, 2021], 0.3))
    items = pd.DataFrame(rows, columns=["cik", "item", "priority", "start", "end", "filed", "val"])
    rev = items[items["item"] == "revenue"]
    assert len(rev) == 3 and (rev["val"] == 99.0).any() and not (rev["val"] == 5.0).any()  # 2 years + 1 quarter; 8-K ignored
    assert items[items["item"] == "assets"]["start"].isna().all()
    assert set(items["item"]) >= {"revenue", "gross_profit", "net_income", "cfo", "assets", "shares"}


def test_as_of_uses_only_filed_numbers_and_finds_prior_year():
    rows = fundamentals.parse_company(company(1, [2020, 2021, 2022], 0.3))
    # A restatement of 2022 net income, filed later.
    rows.append((1, "net_income", 0, "2022-01-01", "2022-12-31", "2023-08-01", 1.0))
    facts = pd.DataFrame(rows, columns=["cik", "item", "priority", "start", "end", "filed", "val"])
    for c in ("start", "end", "filed"):
        facts[c] = pd.to_datetime(facts[c])
    jan = fundamentals.as_of(facts, pd.Timestamp("2023-01-31"))   # 2022 10-K not filed yet
    assert jan.loc[1, "revenue"] == pytest.approx(800 * 1.03)
    mar = fundamentals.as_of(facts, pd.Timestamp("2023-03-31"))
    assert mar.loc[1, "revenue"] == pytest.approx(800 * 1.03 ** 2)
    assert mar.loc[1, "revenue_prior"] == pytest.approx(800 * 1.03)
    assert mar.loc[1, "net_income"] != 1.0                         # restatement not public yet
    sep = fundamentals.as_of(facts, pd.Timestamp("2023-09-30"))
    assert sep.loc[1, "net_income"] == 1.0
    assert fundamentals.as_of(facts, pd.Timestamp("2026-09-30")).empty  # stale numbers are dropped


def test_market_cap_undoes_later_splits():
    close = pd.Series({"ABC": 50.0})            # Yahoo's split-adjusted price
    shares = pd.Series({9: 1_000.0})              # reported before a 2-for-1 split
    when = pd.Series({9: pd.Timestamp("2020-01-31")})
    splits = pd.DataFrame({"ticker": ["ABC"], "date": ["2021-06-01"], "ratio": [2.0]})
    caps = factors.market_caps(close, shares, when, splits, {9: "ABC"})
    assert caps[9] == pytest.approx(50 * 1_000 * 2)


def test_update_and_study(model_db, tmp_path, monkeypatch, capsys):
    years = list(range(DATES[0].year - 1, DATES[-1].year))
    data = bulk_zip(years)
    monkeypatch.setattr(fundamentals.insiders, "CACHE_DIR", tmp_path / "sec")

    def fake_download(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    n = fundamentals.update_fundamentals(model_db, download=fake_download, log=lambda *_: None)
    assert n > 0 and not (tmp_path / "sec" / "companyfacts.zip").exists()  # big file removed afterwards
    assert fundamentals.update_fundamentals(model_db, download=fake_download, log=lambda *_: None) == 0  # weekly

    df = factors.monthly_factors(model_db, since=str(DATES[260].date()), log=lambda *_: None)
    assert len(df) and {"gross_profitability", "book_to_market", "asset_growth", "ret"} <= set(df.columns)
    res = factors.study(df, split_year=int(df["month"].dt.year.max()))
    # Profitability was built to line up with price drift, so its IC must be clearly positive.
    assert res["all"].loc["gross_profitability", "mean_ic"] > 0.1

    assert main(["study", "factors", "--db", str(tmp_path / "market.db"), "--since", str(DATES[260].date())]) == 0
    out = capsys.readouterr().out
    assert "gross_profitability" in out and "quality_value_combo" in out and "mean_ic" in out


def test_study_factors_without_data(model_db, tmp_path, capsys):
    assert main(["study", "factors", "--db", str(tmp_path / "market.db")]) == 0
    assert "No company financials yet" in capsys.readouterr().out


def test_net_issuance_takes_out_splits_and_drops_bad_data():
    f = pd.DataFrame({
        "shares":            [1_000.0, 4_000.0, 900.0, 50_000.0],
        "shares_prior":      [900.0, 1_000.0, 1_000.0, 1_000.0],
        "shares_date":       pd.to_datetime(["2024-12-31"] * 4),
        "shares_prior_date": pd.to_datetime(["2023-12-31"] * 4),
    }, index=[1, 2, 3, 4])
    splits = pd.DataFrame({"ticker": ["BBB", "BBB"], "date": ["2024-06-01", "2025-03-01"], "ratio": [4.0, 2.0]})
    out = factors.net_issuance(f, splits, {1: "AAA", 2: "BBB", 3: "CCC", 4: "DDD"})
    assert out[1] == pytest.approx(np.log(1000 / 900))     # issued 11% more shares
    assert out[2] == pytest.approx(0.0)                    # a 4-for-1 split isn't issuance; the 2025 one is later
    assert out[3] < 0                                      # bought back 10%
    assert np.isnan(out[4])                                # 50x in a year: a data error


def test_as_of_remembers_when_the_prior_share_count_was_reported():
    rows = fundamentals.parse_company(company(1, [2020, 2021, 2022], 0.3))
    facts = pd.DataFrame(rows, columns=["cik", "item", "priority", "start", "end", "filed", "val"])
    for c in ("start", "end", "filed"):
        facts[c] = pd.to_datetime(facts[c])
    mar = fundamentals.as_of(facts, pd.Timestamp("2023-03-31"))
    gap = (mar.loc[1, "shares_date"] - mar.loc[1, "shares_prior_date"]).days
    assert 330 <= gap <= 400
