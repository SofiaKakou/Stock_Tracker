import pandas as pd

from test_fundamentals import bulk_zip
from test_model import DATES, model_db  # noqa: F401
from trend_bot import factors, fundamentals


def test_parallel_table_matches_the_one_process_table(model_db, tmp_path, monkeypatch):  # noqa: F811
    years = list(range(DATES[0].year - 1, DATES[-1].year))
    data = bulk_zip(years)
    monkeypatch.setattr(fundamentals.insiders, "CACHE_DIR", tmp_path / "sec")
    fundamentals.update_fundamentals(model_db, download=lambda p: (p.parent.mkdir(parents=True, exist_ok=True),
                                                                   p.write_bytes(data), p)[-1],
                                     log=lambda *_: None)
    since = str(DATES[200].date())
    one = factors.monthly_factors(model_db, since=since, extras=True, workers=1, log=lambda *_: None)
    many = factors.monthly_factors(model_db, since=since, extras=True, workers=3, log=lambda *_: None)
    assert one["month"].nunique() >= 12 and len(one) > 0
    pd.testing.assert_frame_equal(one, many)
    assert factors._CTX == {}                                 # nothing big left behind


def test_workers_default_and_override(monkeypatch):
    monkeypatch.setenv("FACTOR_WORKERS", "2")
    assert factors._factor_workers() == 2
    monkeypatch.delenv("FACTOR_WORKERS")
    assert 1 <= factors._factor_workers() <= 4
