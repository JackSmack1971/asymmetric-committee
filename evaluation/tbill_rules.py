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

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from contracts.market_data import TBillObservation, TBillVintageCoverage
from evaluation.calendar_rules import CalendarCoverageError, TradingCalendar


class TBillUnresolved(StrEnum):
    NO_VINTAGE_COVERAGE = "no_vintage_coverage"
    VINTAGE_COVERAGE_NOT_KNOWN = "vintage_coverage_not_known"
    BEFORE_EARLIEST_VINTAGE = "before_earliest_vintage"
    NO_USABLE_RATE = "no_usable_rate"
    CALENDAR_UNCOVERED = "calendar_uncovered"


@dataclass(frozen=True)
class TBillRate:
    observation_date: date
    vintage_date: date
    yield_pct: float
    source_version: str


@dataclass(frozen=True)
class Unresolved:
    reason: TBillUnresolved


@dataclass(frozen=True)
class TBillAccrualStep:
    accrual_session: date
    accrual_days: int
    rate: TBillRate


@dataclass(frozen=True)
class TBillPeriodReturn:
    value: float
    steps: tuple[TBillAccrualStep, ...]


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
    return TBillRate(
        chosen.observation_date,
        chosen.vintage_date,
        chosen.yield_pct,
        chosen.source_version,
    )


def accrue_period(
    rows: Sequence[TBillObservation],
    coverage: TBillVintageCoverage | None,
    start: date,
    end: date,
    calendar: TradingCalendar,
    *,
    cutoff: datetime,
) -> TBillPeriodReturn | Unresolved:
    """Accrue DGS3MO across a stored-calendar interval with per-session ALFRED vintages.

    ``cutoff`` is the admitted P6.5 scoring time; it bounds the durable vintage coverage record.
    Each returned step retains the selected observation/vintage/version for exact replay.
    """
    if end <= start:
        raise ValueError("T-bill accrual end must follow start")
    if coverage is None:
        return Unresolved(TBillUnresolved.NO_VINTAGE_COVERAGE)
    if coverage.established_at > cutoff:
        return Unresolved(TBillUnresolved.VINTAGE_COVERAGE_NOT_KNOWN)
    try:
        calendar.session(start)
        calendar.session(end)
        for offset in range((end - start).days + 1):
            calendar.coverage_time(start + timedelta(days=offset))
    except CalendarCoverageError:
        return Unresolved(TBillUnresolved.CALENDAR_UNCOVERED)
    sessions = tuple(day for day in sorted(calendar.sessions) if start < day <= end)
    if not sessions or sessions[-1] != end:
        return Unresolved(TBillUnresolved.CALENDAR_UNCOVERED)
    growth = 1.0
    previous = start
    steps: list[TBillAccrualStep] = []
    for accrual_session in sessions:
        selected = select_rate(rows, coverage, accrual_session, calendar)
        if isinstance(selected, Unresolved):
            return selected
        factor = 1.0 + selected.yield_pct / 100.0
        days = (accrual_session - previous).days
        if factor <= 0 or days <= 0:
            return Unresolved(TBillUnresolved.NO_USABLE_RATE)
        growth *= factor ** (days / 365.0)
        if not math.isfinite(growth):
            return Unresolved(TBillUnresolved.NO_USABLE_RATE)
        steps.append(TBillAccrualStep(accrual_session, days, selected))
        previous = accrual_session
    return TBillPeriodReturn(growth - 1.0, tuple(steps))
