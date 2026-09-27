"""Trading-calendar rules (§4.6): D0, horizons, coverage vs holidays, revisions, canonical hash."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, timedelta

import pytest

from contracts.market_data import CalendarCoverage, TradingSession, calendar_hash
from evaluation.calendar_rules import (
    CalendarCoverageError,
    SessionStatus,
    TradingCalendar,
    build_calendar,
)
from tests.market_data_support import (
    FETCHED,
    coverage_for,
    et,
    session_for,
    sessions_2024,
)

FULL = date(2024, 1, 2), date(2024, 12, 31)


def calendar(start: date = FULL[0], end: date = FULL[1]) -> TradingCalendar:
    sessions = sessions_2024(start, end)
    return build_calendar(sessions, [coverage_for(sessions, start, end)])


# --- D0 and horizons across holidays and early closes -------------------------------------------


def test_d0_is_the_next_stored_session_and_can_be_a_tuesday() -> None:
    cal = calendar()
    assert cal.d0(date(2024, 3, 1)) == date(2024, 3, 4)  # normal Friday -> Monday
    assert cal.d0(date(2024, 1, 12)) == date(2024, 1, 16)  # Monday 15 Jan is MLK day -> Tuesday


def test_d0_after_a_thursday_when_friday_is_a_holiday() -> None:
    assert calendar().d0(date(2024, 3, 28)) == date(2024, 4, 1)  # Good Friday 29 Mar closed


def test_horizons_count_stored_sessions_across_thanksgiving_and_the_early_close() -> None:
    cal = calendar()
    d0 = date(2024, 11, 25)
    # 26, 27, (28 closed), 29 early close, Dec 2, Dec 3
    assert cal.horizon_session(d0, 5) == date(2024, 12, 3)
    assert cal.session(date(2024, 11, 29)).close_at == et(2024, 11, 29, 13, 0)
    assert cal.horizon_session(d0, 1) == date(2024, 11, 26)


def test_21_and_63_session_horizons_skip_every_holiday() -> None:
    cal = calendar()
    d0 = date(2024, 6, 3)
    sessions = [s for s in sessions_2024() if s.session_date > d0]
    assert cal.horizon_session(d0, 21) == sessions[20].session_date
    assert cal.horizon_session(d0, 63) == sessions[62].session_date
    # The 63rd session is later than 63 weekdays would be, because holidays were skipped.
    assert (cal.horizon_session(d0, 63) - d0).days > 63 * 7 // 5


# --- coverage: a holiday is not a missing fetch --------------------------------------------------


def test_inside_confirmed_coverage_absence_means_closed() -> None:
    cal = calendar()
    assert cal.status(date(2024, 1, 15)) is SessionStatus.CLOSED  # holiday
    assert cal.status(date(2024, 1, 13)) is SessionStatus.CLOSED  # weekend
    assert cal.status(date(2024, 1, 16)) is SessionStatus.OPEN


def test_outside_every_confirmed_range_a_date_is_unknown_not_closed() -> None:
    cal = calendar(date(2024, 1, 2), date(2024, 6, 28))
    assert cal.status(date(2024, 7, 1)) is SessionStatus.UNCOVERED  # would be a Monday session
    assert cal.status(date(2023, 12, 29)) is SessionStatus.UNCOVERED
    with pytest.raises(CalendarCoverageError):
        cal.session(date(2024, 7, 1))


def test_sessions_alone_do_not_imply_coverage_and_max_date_is_not_coverage() -> None:
    sessions = sessions_2024(date(2024, 1, 2), date(2024, 1, 31))
    cal = build_calendar(sessions, [])  # sessions stored, but no coverage record
    assert cal.status(date(2024, 1, 5)) is SessionStatus.UNCOVERED
    assert cal.status(date(2024, 1, 15)) is SessionStatus.UNCOVERED  # not "closed" either
    with pytest.raises(CalendarCoverageError):
        cal.d0(date(2024, 1, 12))


def test_a_horizon_running_past_coverage_fails_closed() -> None:
    cal = calendar(date(2024, 1, 2), date(2024, 3, 28))
    with pytest.raises(CalendarCoverageError):
        cal.horizon_session(date(2024, 3, 4), 63)  # coverage ends long before 63 sessions


def test_a_gap_in_the_middle_is_not_walked_over() -> None:
    a = sessions_2024(date(2024, 1, 2), date(2024, 1, 31))
    b = sessions_2024(date(2024, 3, 1), date(2024, 3, 29))
    cov_a = coverage_for(a, date(2024, 1, 2), date(2024, 1, 31))
    cov_b = coverage_for(b, date(2024, 3, 1), date(2024, 3, 29))
    cal = build_calendar([*a, *b], [cov_a, cov_b])
    with pytest.raises(CalendarCoverageError):
        cal.horizon_session(date(2024, 1, 30), 10)  # would cross February, which was never fetched


def test_no_weekday_fallback_a_weekday_without_a_stored_session_is_never_counted() -> None:
    may = sessions_2024(date(2024, 5, 1), date(2024, 5, 31))
    sessions = [s for s in may if s.session_date != date(2024, 5, 8)]
    cal = build_calendar(sessions, [coverage_for(sessions, date(2024, 5, 1), date(2024, 5, 31))])
    # a Wednesday, absent inside coverage
    assert cal.status(date(2024, 5, 8)) is SessionStatus.CLOSED
    assert cal.horizon_session(date(2024, 5, 7), 1) == date(2024, 5, 9)


def test_previous_session_uses_the_stored_calendar_only() -> None:
    cal = calendar()
    assert cal.previous_session(date(2024, 1, 16)) == date(2024, 1, 12)  # over the MLK weekend
    assert cal.previous_session(date(2024, 11, 29)) == date(2024, 11, 27)  # over Thanksgiving
    with pytest.raises(CalendarCoverageError):
        cal.previous_session(date(2024, 1, 2))  # nothing covered before the first day


# --- revisions and one point-in-time view --------------------------------------------------------


def test_a_revision_needs_new_coverage_and_never_leaks_into_an_older_view() -> None:
    t0, t1 = FETCHED, FETCHED + timedelta(days=30)
    day = date(2024, 3, 4)
    v1 = sessions_2024(date(2024, 3, 1), date(2024, 3, 8), available_at=t0, version="v1")
    revised = session_for(day, available_at=t1, version="v2").model_copy(
        update={"close_at": et(2024, 3, 4, 13, 0)}
    )
    v2 = [revised if s.session_date == day else s for s in v1]
    cov1 = coverage_for(v1, date(2024, 3, 1), date(2024, 3, 8), available_at=t0)
    # The revision alone, without its coverage row: refused (a session newer than its coverage).
    with pytest.raises(CalendarCoverageError):
        build_calendar([*v1, revised], [cov1])
    # With the new coverage record the newest view sees the revision, the old view does not.
    v2_only_new = [s for s in v2 if s.session_date == day]
    cov2 = CalendarCoverage(
        **{
            **cov1.model_dump(),
            "available_at": t1,
            "sessions_sha256": calendar_hash(v2),
            "source_version": "cov2",
        }
    )
    both = [*v1, *v2_only_new]
    assert build_calendar(both, [cov1, cov2]).session(day).close_at == et(2024, 3, 4, 13, 0)
    known = [s for s in both if s.available_at <= t0]
    older = build_calendar(known, [c for c in [cov1, cov2] if c.available_at <= t0])
    assert older.session(day).close_at == et(2024, 3, 4, 16, 0)


def test_a_coverage_record_that_does_not_match_its_sessions_is_refused() -> None:
    sessions = sessions_2024(date(2024, 3, 1), date(2024, 3, 8))
    cov = coverage_for(sessions[:-1], date(2024, 3, 1), date(2024, 3, 8))  # hash of a different set
    with pytest.raises(CalendarCoverageError):
        build_calendar(sessions, [cov])


# --- the canonical calendar hash (golden) -------------------------------------------------------

GOLDEN = [
    TradingSession(
        event_time=et(2024, 3, 4, 9, 30),
        available_at=FETCHED,
        source_version="alpaca_calendar:aaaa1111",
        session_date=date(2024, 3, 4),
        open_at=et(2024, 3, 4, 9, 30),
        close_at=et(2024, 3, 4, 16, 0),
    ),
    TradingSession(
        event_time=et(2024, 3, 5, 9, 30),
        available_at=FETCHED,
        source_version="alpaca_calendar:bbbb2222",
        session_date=date(2024, 3, 5),
        open_at=et(2024, 3, 5, 9, 30),
        close_at=et(2024, 3, 5, 13, 0),
    ),
]
GOLDEN_HASH = "cdf5fb029d0129d99087240a8fb1b8540d46f301c061146c1cba26a97432cf16"
GOLDEN_TEXT = (
    "2024-03-04|2024-03-04T14:30:00.000000+00:00|2024-03-04T21:00:00.000000+00:00"
    "|alpaca_calendar:aaaa1111\n"
    "2024-03-05|2024-03-05T14:30:00.000000+00:00|2024-03-05T18:00:00.000000+00:00"
    "|alpaca_calendar:bbbb2222"
)


def test_the_calendar_hash_is_a_pinned_canonical_digest() -> None:
    assert hashlib.sha256(GOLDEN_TEXT.encode()).hexdigest() == GOLDEN_HASH  # the documented format
    assert calendar_hash(GOLDEN) == GOLDEN_HASH


def test_hash_is_input_order_invariant() -> None:
    assert calendar_hash(list(reversed(GOLDEN))) == calendar_hash(GOLDEN)


def test_hash_is_timezone_normalised() -> None:
    utc = [
        s.model_copy(
            update={
                "open_at": s.open_at.astimezone(UTC),
                "close_at": s.close_at.astimezone(UTC),
                "event_time": s.event_time.astimezone(UTC),
            }
        )
        for s in GOLDEN
    ]
    assert calendar_hash(utc) == calendar_hash(GOLDEN)


@pytest.mark.parametrize(
    "change",
    [
        {"session_date": date(2024, 3, 6)},
        {"open_at": et(2024, 3, 4, 9, 31), "event_time": et(2024, 3, 4, 9, 31)},
        {"close_at": et(2024, 3, 4, 15, 59)},
        {"source_version": "alpaca_calendar:cccc3333"},
    ],
)
def test_hash_is_sensitive_to_every_field_that_matters(change: dict[str, object]) -> None:
    changed = [GOLDEN[0].model_copy(update=change), GOLDEN[1]]
    assert calendar_hash(changed) != GOLDEN_HASH
