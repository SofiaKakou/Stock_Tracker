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
