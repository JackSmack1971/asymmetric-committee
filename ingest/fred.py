"""FRED/ALFRED vintage retrieval for DGS3MO (§4.1, §12.3).

Revised macro series must come from ALFRED vintages, never from current FRED values. Every
(observation, vintage) pair is kept with its ``vintage_date`` (ALFRED's ``realtime_start``); a
missing value (``"."``) is not stored. Coverage is established from the fetched vintage-date list:
the earliest vintage is whatever ALFRED reports, never assumed.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, date, datetime
from typing import Any

import httpx

from contracts.market_data import TBillObservation, TBillVintageCoverage

FRED_URL = "https://api.stlouisfed.org"
SERIES = "DGS3MO"
_FIRST = "1776-07-04"  # FRED's documented earliest real-time date
_LAST = "9999-12-31"
_PAGE = 100_000  # FRED's maximum page size


class FredError(RuntimeError):
    """A FRED response could not be read as the vintage data it should contain."""


class AlfredClient:
    def __init__(
        self, transport: httpx.BaseTransport | None = None, *, api_key: str | None = None
    ) -> None:
        self._key = api_key or os.environ.get("FRED_API_KEY", "")
        self._http = httpx.Client(
            base_url=FRED_URL, transport=transport, timeout=60.0, follow_redirects=False
        )

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        r = self._http.get(path, params={**params, "api_key": self._key, "file_type": "json"})
        r.raise_for_status()
        payload: dict[str, Any] = r.json()
        return payload

    def vintage_dates(self, series: str = SERIES) -> list[date]:
        """Every date on which the series changed, oldest first."""
        out: list[date] = []
        offset = 0
        while True:
            page = self._get(
                "/fred/series/vintagedates",
                {
                    "series_id": series,
                    "realtime_start": _FIRST,
                    "realtime_end": _LAST,
                    "limit": "10000",
                    "offset": str(offset),
                },
            )
            rows = page.get("vintage_dates")
            if not isinstance(rows, list):
                raise FredError("vintagedates response has no vintage_dates list")
            out.extend(date.fromisoformat(d) for d in rows)
            if len(rows) < 10_000:
                return sorted(set(out))
            offset += len(rows)

    def observations(self, series: str = SERIES) -> list[TBillObservation]:
        """Every observation in every vintage (``realtime_start`` -> ``vintage_date``)."""
        out: list[TBillObservation] = []
        offset = 0
        while True:
            page = self._get(
                "/fred/series/observations",
                {
                    "series_id": series,
                    "realtime_start": _FIRST,
                    "realtime_end": _LAST,
                    "limit": str(_PAGE),
                    "offset": str(offset),
                },
            )
            rows = page.get("observations")
            if not isinstance(rows, list):
                raise FredError("observations response has no observations list")
            out.extend(parse_observations(series, rows))
            if len(rows) < _PAGE:
                return out
            offset += len(rows)

    def close(self) -> None:
        self._http.close()


def parse_observations(series: str, rows: list[dict[str, Any]]) -> list[TBillObservation]:
    out: list[TBillObservation] = []
    for r in rows:
        if r["value"] == ".":  # FRED's marker for "no value": absence, not zero
            continue
        vintage = date.fromisoformat(r["realtime_start"])
        out.append(
            TBillObservation(
                series=series,
                observation_date=date.fromisoformat(r["date"]),
                yield_pct=float(r["value"]),
                vintage_date=vintage,
                source_version=f"alfred:{vintage.isoformat()}",
            )
        )
    return out


def establish_vintage_coverage(
    series: str, vintage_dates: list[date], *, now: datetime
) -> TBillVintageCoverage:
    """Earliest and latest usable vintage, derived from the fetched list (never assumed)."""
    if not vintage_dates:
        raise FredError(f"{series}: ALFRED reported no vintages")
    ordered = sorted(set(vintage_dates))
    digest = hashlib.sha256("\n".join(d.isoformat() for d in ordered).encode()).hexdigest()
    return TBillVintageCoverage(
        series=series,
        earliest_vintage=ordered[0],
        latest_vintage=ordered[-1],
        vintage_count=len(ordered),
        vintage_dates_sha256=digest,
        established_at=now.astimezone(UTC),
        source_version=f"alfred_vintages:{digest[:16]}",
    )
