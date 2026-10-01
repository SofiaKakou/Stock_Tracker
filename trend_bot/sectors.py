"""Industry groups from SEC industry codes (SIC), for comparing companies with their peers.

A bank and a software company look very different on debt, profit margins and
price-to-book, so "cheap" or "heavily indebted" means more within an industry
than across the whole market. About 15 broad groups, adapted from the
Fama-French industry definitions.
"""

from __future__ import annotations

import sqlite3

import pandas as pd

# (first SIC, last SIC, group); the first match wins, so narrow ranges come first.
RANGES = [
    (2830, 2836, "Health care"), (3840, 3851, "Health care"), (8000, 8099, "Health care"),
    (3570, 3579, "Technology"), (3660, 3679, "Technology"), (3810, 3829, "Technology"),
    (7370, 7379, "Technology"),
    (4800, 4899, "Telecom & media"), (2710, 2799, "Telecom & media"), (7810, 7819, "Telecom & media"),
    (6798, 6798, "Real estate"), (6500, 6599, "Real estate"),
    (1300, 1399, "Energy"), (2900, 2999, "Energy"), (1200, 1299, "Energy"),
    (4900, 4999, "Utilities"),
    (6000, 6799, "Financials"),
    (2000, 2199, "Consumer staples"), (2840, 2844, "Consumer staples"), (100, 999, "Consumer staples"),
    (5200, 5999, "Retail"), (7000, 7099, "Consumer services"), (7800, 7999, "Consumer services"),
    (5800, 5899, "Consumer services"),
    (2200, 2399, "Consumer goods"), (2500, 2599, "Consumer goods"), (3000, 3099, "Consumer goods"),
    (3630, 3659, "Consumer goods"), (3710, 3711, "Consumer goods"), (3714, 3716, "Consumer goods"),
    (3900, 3999, "Consumer goods"),
    (1000, 1199, "Materials"), (1400, 1499, "Materials"), (2400, 2499, "Materials"), (2600, 2699, "Materials"),
    (2800, 2899, "Materials"), (3200, 3399, "Materials"),
    (4000, 4799, "Transport"),
    (3400, 3999, "Industrials"), (1500, 1799, "Industrials"), (5000, 5199, "Industrials"),
    (7300, 7399, "Business services"), (8100, 8999, "Business services"),
]


def sector(sic: int | float | None) -> str | None:
    if sic is None or pd.isna(sic):
        return None
    sic = int(sic)
    for lo, hi, name in RANGES:
        if lo <= sic <= hi:
            return name
    return "Other"


def ticker_sectors(con: sqlite3.Connection) -> pd.Series:
    """Industry group per ticker (missing when the SEC code hasn't been looked up yet)."""
    rows = con.execute("SELECT ticker, sic FROM tickers WHERE sic IS NOT NULL").fetchall()
    return pd.Series({t: sector(s) for t, s in rows}, dtype=object)
