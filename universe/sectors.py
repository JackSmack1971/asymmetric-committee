"""SEC SIC code → sector (GICS-style names). Securities carry this sector; universe.yaml's
``sectors`` must use these names. Specific ranges come before the broad ones."""

from __future__ import annotations

SECTORS = (
    "Energy",
    "Materials",
    "Industrials",
    "Consumer Discretionary",
    "Consumer Staples",
    "Health Care",
    "Financials",
    "Information Technology",
    "Communication Services",
    "Utilities",
    "Real Estate",
)

# (lo, hi, sector), inclusive; first match wins.
_RANGES: tuple[tuple[int, int, str], ...] = (
    (1300, 1399, "Energy"),
    (2830, 2836, "Health Care"),
    (2840, 2844, "Consumer Staples"),
    (2900, 2999, "Energy"),
    (3100, 3199, "Consumer Discretionary"),
    (3570, 3579, "Information Technology"),
    (3630, 3639, "Consumer Discretionary"),
    (3710, 3716, "Consumer Discretionary"),
    (3840, 3851, "Health Care"),
    (5120, 5122, "Health Care"),
    (5140, 5149, "Consumer Staples"),
    (5400, 5499, "Consumer Staples"),
    (5912, 5912, "Consumer Staples"),
    (6500, 6553, "Real Estate"),
    (6798, 6798, "Real Estate"),
    (7370, 7379, "Information Technology"),
    (100, 999, "Consumer Staples"),
    (1000, 1499, "Materials"),
    (1500, 1799, "Industrials"),
    (2000, 2199, "Consumer Staples"),
    (2200, 2399, "Consumer Discretionary"),
    (2400, 2699, "Materials"),
    (2700, 2799, "Communication Services"),
    (2800, 2899, "Materials"),
    (3000, 3499, "Materials"),
    (3500, 3599, "Industrials"),
    (3600, 3699, "Information Technology"),
    (3700, 3799, "Industrials"),
    (3800, 3899, "Information Technology"),
    (3900, 3999, "Consumer Discretionary"),
    (4000, 4799, "Industrials"),
    (4800, 4899, "Communication Services"),
    (4900, 4999, "Utilities"),
    (5000, 5199, "Industrials"),
    (5200, 5999, "Consumer Discretionary"),
    (6000, 6799, "Financials"),
    (7000, 7299, "Consumer Discretionary"),
    (7300, 7399, "Industrials"),
    (7800, 7999, "Communication Services"),
    (8000, 8099, "Health Care"),
    (8700, 8799, "Industrials"),
)


def sector_for_sic(sic: int | None) -> str | None:
    if sic is None:
        return None
    return next((s for lo, hi, s in _RANGES if lo <= sic <= hi), None)
