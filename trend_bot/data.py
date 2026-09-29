"""Price data loading.

Every loader returns a DataFrame indexed by date with columns
Open, High, Low, Close, Volume (prices adjusted for splits/dividends).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
CACHE_DIR = Path("data_cache")


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    # yfinance can return MultiIndex columns like ("Close", "AAPL").
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"price data is missing columns: {missing}")
    df = df[REQUIRED_COLUMNS].dropna(subset=["Close"])
    df.index = pd.to_datetime(df.index)
    df.index.name = "Date"
    return df.sort_index()


def load_csv(path: str | Path) -> pd.DataFrame:
    """Load prices from a CSV with a Date column plus OHLCV columns."""
    df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
    return _normalize(df)


def load_yahoo(ticker: str, period: str = "2y", use_cache: bool = True) -> pd.DataFrame:
    """Download daily prices from Yahoo Finance, caching to data_cache/.

    The cache is keyed by ticker, period and date, so it refreshes once a day.
    """
    cache_file = CACHE_DIR / f"{ticker.upper()}_{period}_{pd.Timestamp.today():%Y%m%d}.csv"
    if use_cache and cache_file.exists():
        return load_csv(cache_file)

    import yfinance as yf  # imported lazily so offline use doesn't need it

    raw = yf.download(ticker, period=period, interval="1d", auto_adjust=True, progress=False)
    if raw is None or raw.empty:
        raise RuntimeError(f"no data returned for {ticker!r} (bad ticker or no network?)")
    df = _normalize(raw)

    if use_cache:
        CACHE_DIR.mkdir(exist_ok=True)
        df.to_csv(cache_file)
    return df


def load_prices(source: str, period: str = "2y", use_cache: bool = True) -> pd.DataFrame:
    """Load from a CSV path if `source` ends in .csv, otherwise treat it as a ticker."""
    if source.lower().endswith(".csv"):
        return load_csv(source)
    return load_yahoo(source, period=period, use_cache=use_cache)


def read_watchlist(path: str | Path) -> list[str]:
    lines = Path(path).read_text().splitlines()
    return [ln.strip().upper() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
