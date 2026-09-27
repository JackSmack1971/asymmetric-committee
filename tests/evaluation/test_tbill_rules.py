"""DGS3MO accrual selection (§12.3): vintage-aware, date-granular, calendar-based."""

from __future__ import annotations

from datetime import UTC, date, datetime

from contracts.market_data import TBillObservation, TBillVintageCoverage
from evaluation.calendar_rules import build_calendar
from evaluation.tbill_rules import TBillRate, TBillUnresolved, Unresolved, select_rate
from tests.market_data_support import coverage_for, sessions_2024

SESSIONS = sessions_2024()
CAL = build_calendar(SESSIONS, [coverage_for(SESSIONS, date(2024, 1, 2), date(2024, 12, 31))])


def obs(observation: date, vintage: date, pct: float) -> TBillObservation:
    return TBillObservation(
        series="DGS3MO",
        observation_date=observation,
        yield_pct=pct,
        vintage_date=vintage,
        source_version=f"alfred:{vintage.isoformat()}",
    )


def coverage(earliest: date = date(2024, 1, 2)) -> TBillVintageCoverage:
    return TBillVintageCoverage(
        series="DGS3MO",
        earliest_vintage=earliest,
        latest_vintage=date(2024, 12, 31),
        vintage_count=250,
        vintage_dates_sha256="0" * 64,
        established_at=datetime(2024, 12, 31, tzinfo=UTC),
        source_version="alfred_vintages:x",
    )


def test_the_rate_is_the_latest_observation_at_or_before_the_previous_session() -> None:
    rows = [
        obs(date(2024, 3, 6), date(2024, 3, 7), 5.20),
        obs(date(2024, 3, 7), date(2024, 3, 8), 5.21),
        obs(date(2024, 3, 8), date(2024, 3, 11), 5.22),  # vintage on the accrual day: unusable
    ]
    got = select_rate(rows, coverage(), date(2024, 3, 11), CAL)
    # previous session of Mon 11 Mar is Fri 8 Mar, but its vintage is dated 11 Mar (same day)
    assert got == TBillRate(date(2024, 3, 7), date(2024, 3, 8), 5.21)


def test_a_same_day_vintage_is_never_usable_without_release_time_evidence() -> None:
    rows = [obs(date(2024, 3, 7), date(2024, 3, 8), 5.21)]
    assert isinstance(select_rate(rows, coverage(), date(2024, 3, 8), CAL), Unresolved)
    assert isinstance(select_rate(rows, coverage(), date(2024, 3, 11), CAL), TBillRate)


def test_a_later_vintage_cannot_change_what_an_earlier_accrual_date_sees() -> None:
    first = obs(date(2024, 3, 6), date(2024, 3, 7), 5.20)
    revised = obs(date(2024, 3, 6), date(2024, 4, 15), 5.30)
    before = select_rate([first, revised], coverage(), date(2024, 3, 11), CAL)
    assert isinstance(before, TBillRate) and before.yield_pct == 5.20
    after = select_rate([first, revised], coverage(), date(2024, 5, 1), CAL)
    assert isinstance(after, TBillRate) and (after.yield_pct, after.vintage_date) == (
        5.30,
        date(2024, 4, 15),
    )


def test_the_previous_session_skips_holidays_using_the_stored_calendar() -> None:
    rows = [obs(date(2024, 1, 12), date(2024, 1, 13), 5.30)]
    got = select_rate(
        rows, coverage(), date(2024, 1, 16), CAL
    )  # Tue after MLK: previous is Fri 12 Jan
    assert isinstance(got, TBillRate) and got.observation_date == date(2024, 1, 12)


def test_an_observation_after_the_previous_session_is_not_used() -> None:
    rows = [
        obs(date(2024, 3, 8), date(2024, 3, 9), 5.22),
        obs(date(2024, 3, 11), date(2024, 3, 12), 5.23),
    ]
    got = select_rate(rows, coverage(), date(2024, 3, 13), CAL)  # previous session is Mon 11 Mar
    assert isinstance(got, TBillRate) and got.observation_date == date(2024, 3, 11)


def test_missing_vintage_evidence_is_unresolved() -> None:
    assert select_rate([], coverage(), date(2024, 3, 11), CAL) == Unresolved(
        TBillUnresolved.NO_USABLE_RATE
    )
    assert select_rate(
        [obs(date(2024, 3, 7), date(2024, 3, 8), 5.2)], None, date(2024, 3, 11), CAL
    ) == Unresolved(TBillUnresolved.NO_VINTAGE_COVERAGE)


def test_a_period_before_the_earliest_usable_vintage_is_unresolved() -> None:
    rows = [obs(date(2024, 3, 7), date(2024, 3, 8), 5.2)]
    got = select_rate(rows, coverage(earliest=date(2024, 3, 8)), date(2024, 3, 8), CAL)
    assert got == Unresolved(TBillUnresolved.BEFORE_EARLIEST_VINTAGE)


def test_no_observation_date_plus_one_fallback_exists() -> None:
    """No vintage before the accrual day means unresolved, not a +1 day rescue."""
    rows = [obs(date(2024, 3, 8), date(2024, 3, 11), 5.22)]  # first known on the accrual day itself
    assert select_rate(rows, coverage(), date(2024, 3, 11), CAL) == Unresolved(
        TBillUnresolved.NO_USABLE_RATE
    )


def test_an_uncovered_calendar_is_unresolved_never_guessed() -> None:
    empty = build_calendar([], [])
    rows = [obs(date(2024, 3, 7), date(2024, 3, 8), 5.2)]
    assert select_rate(rows, coverage(), date(2024, 3, 11), empty) == Unresolved(
        TBillUnresolved.CALENDAR_UNCOVERED
    )
