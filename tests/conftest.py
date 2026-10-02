import numpy as np
import pandas as pd
import pytest


def make_prices(closes) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range("2020-01-01", periods=len(closes), name="Date")
    return pd.DataFrame(
        {"Open": closes, "High": closes * 1.001, "Low": closes * 0.999, "Close": closes, "Volume": 1_000},
        index=idx,
    )


@pytest.fixture
def up_then_down():
    """300 bars rising from 100 to 200, then 300 bars falling back to 100."""
    return make_prices(np.concatenate([np.linspace(100, 200, 300), np.linspace(200, 100, 300)]))


def submissions_zip(docs: dict[int, dict]) -> bytes:
    """A fake SEC submissions.zip: {cik: submissions JSON}."""
    import io
    import json
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for cik, doc in docs.items():
            z.writestr(f"CIK{cik:010d}.json", json.dumps(doc))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def no_real_sec_bulk_file(monkeypatch):
    """Tests never download the SEC's 1.5 GB bulk file: an empty one stands in (tests that need
    content patch sec_submissions.download themselves)."""
    from trend_bot import sec_submissions

    def fake(path, log=print):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(submissions_zip({}))
        return path

    monkeypatch.setattr(sec_submissions, "download", fake)
