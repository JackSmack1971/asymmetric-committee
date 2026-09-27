"""DGS3MO accrual rate selection (§12.3). Pure: contracts and the calendar rules only.

Eligibility is date-granular. ALFRED publishes the date a vintage became current (`vintage_date`),
not a time, and no release time is stored, so for accrual session date ``P`` only a vintage dated
strictly before ``P`` is usable: a same-day vintage is never treated as known at the start of the
day. The observation used is the latest one dated at or before the previous *session* of ``P``,
taken from the stored trading calendar. There is no business-day arithmetic, no
observation-date-plus-one rule and no synthetic availability: without a defensible vintage the
answer is unresolved.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from contracts.market_data import TBillObservation, TBillVintageCoverage
from evaluation.calendar_rules import CalendarCoverageError, TradingCalendar


class TBillUnresolved(StrEnum):
    NO_VINTAGE_COVERAGE = "no_vintage_coverage"
    BEFORE_EARLIEST_VINTAGE = "before_earliest_vintage"
    NO_USABLE_RATE = "no_usable_rate"
    CALENDAR_UNCOVERED = "calendar_uncovered"


@dataclass(frozen=True)
class TBillRate:
    observation_date: date
    vintage_date: date
    yield_pct: float


@dataclass(frozen=True)
class Unresolved:
    reason: TBillUnresolved


def select_rate(
    rows: Sequence[TBillObservation],
    coverage: TBillVintageCoverage | None,
    accrual_date: date,
    calendar: TradingCalendar,
) -> TBillRate | Unresolved:
    if coverage is None:
        return Unresolved(TBillUnresolved.NO_VINTAGE_COVERAGE)
    if accrual_date <= coverage.earliest_vintage:
        return Unresolved(TBillUnresolved.BEFORE_EARLIEST_VINTAGE)
    try:
        previous = calendar.previous_session(accrual_date)
    except CalendarCoverageError:
        return Unresolved(TBillUnresolved.CALENDAR_UNCOVERED)
    latest: dict[date, TBillObservation] = {}
    for r in rows:
        if r.vintage_date >= accrual_date or r.observation_date > previous:
            continue
        if (
            r.observation_date not in latest
            or r.vintage_date > latest[r.observation_date].vintage_date
        ):
            latest[r.observation_date] = r
    if not latest:
        return Unresolved(TBillUnresolved.NO_USABLE_RATE)
    chosen = latest[max(latest)]
    return TBillRate(chosen.observation_date, chosen.vintage_date, chosen.yield_pct)
