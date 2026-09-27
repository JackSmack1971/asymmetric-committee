"""Scheduling over the broker's trading calendar: week-final closes and the execution window."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from contracts.models import MarketSession
from ingest.timeutil import ET
from orchestration.schedule import (
    NoSessionError,
    WindowState,
    execution_window,
    weekly_as_of,
    window_state,
)


class Calendar:
    """Weekdays are sessions except ``holidays``; ``early`` maps a date to its early close."""

    def __init__(
        self, holidays: tuple[date, ...] = (), early: dict[date, time] | None = None
    ) -> None:
        self.holidays, self.early = set(holidays), early or {}
        self.asked: list[tuple[date, date]] = []

    def sessions(self, start: date, end: date) -> list[MarketSession]:
        self.asked.append((start, end))
        out = []
        d = start
        while d <= end:
            if d.weekday() < 5 and d not in self.holidays:
                out.append(
                    MarketSession(
                        session_date=d,
                        opens_at=datetime.combine(d, time(9, 30), ET),
                        closes_at=datetime.combine(d, self.early.get(d, time(16, 0)), ET),
                    )
                )
            d += timedelta(days=1)
        return out


def et(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


FRI = date(2024, 3, 1)


def test_the_friday_close_is_the_weekly_as_of_once_the_session_has_closed() -> None:
    cal = Calendar()
    assert weekly_as_of(cal, et(2024, 3, 1, 15, 59)) is None  # Thursday's close ends no week
    assert weekly_as_of(cal, et(2024, 3, 1, 16, 0)) == et(2024, 3, 1, 16, 0)
    assert weekly_as_of(cal, et(2024, 3, 3, 12)) == et(2024, 3, 1, 16, 0)  # the weekend after
    assert weekly_as_of(cal, et(2024, 3, 4, 9)) == et(2024, 3, 1, 16, 0)  # Monday before the open
    assert weekly_as_of(cal, et(2024, 3, 5, 17)) is None  # Monday's close is mid-week


def test_a_holiday_friday_moves_the_weekly_close_to_thursday() -> None:
    cal = Calendar(holidays=(FRI,))
    assert weekly_as_of(cal, et(2024, 3, 1, 17)) == et(2024, 2, 29, 16, 0)
    assert weekly_as_of(cal, et(2024, 2, 29, 16, 30)) == et(2024, 2, 29, 16, 0)
    assert weekly_as_of(cal, et(2024, 2, 28, 17)) is None


def test_early_closes_are_taken_from_the_calendar() -> None:
    cal = Calendar(early={FRI: time(13, 0)})
    assert weekly_as_of(cal, et(2024, 3, 1, 13, 5)) == et(2024, 3, 1, 13, 0)
    assert weekly_as_of(cal, et(2024, 3, 1, 12, 55)) is None


def test_the_window_is_the_next_session_from_open_plus_30_minutes_to_its_close() -> None:
    start, end = execution_window(Calendar(), et(2024, 3, 1, 16, 0))
    assert (start, end) == (et(2024, 3, 4, 10, 0), et(2024, 3, 4, 16, 0))  # Monday 10:00-16:00


def test_a_holiday_monday_moves_the_window_to_tuesday_and_early_closes_end_it_early() -> None:
    cal = Calendar(holidays=(date(2024, 3, 4),), early={date(2024, 3, 5): time(13, 0)})
    assert execution_window(cal, et(2024, 3, 1, 16, 0)) == (
        et(2024, 3, 5, 10, 0),
        et(2024, 3, 5, 13, 0),
    )


def test_the_window_never_starts_on_the_as_of_day_itself() -> None:
    # A run committed Thursday evening must not trade Friday's session before Monday's plan exists.
    start, _ = execution_window(Calendar(), et(2024, 2, 29, 16, 0))
    assert start == et(2024, 3, 1, 10, 0)


def test_a_utc_as_of_is_read_in_market_time() -> None:
    utc = et(2024, 3, 1, 22, 0).astimezone(UTC)  # Friday 22:00 ET is already Saturday in UTC
    assert execution_window(Calendar(), utc)[0] == et(2024, 3, 4, 10, 0)


def test_no_session_means_no_guess() -> None:
    class Empty:
        def sessions(self, start: date, end: date) -> list[MarketSession]:
            return []

    with pytest.raises(NoSessionError):
        execution_window(Empty(), et(2024, 3, 1, 16, 0))
    with pytest.raises(NoSessionError):
        weekly_as_of(_OnlyPast(), et(2024, 3, 1, 17))


class _OnlyPast:
    def sessions(self, start: date, end: date) -> list[MarketSession]:
        return [s for s in Calendar().sessions(start, end) if s.session_date <= FRI]


def test_window_states() -> None:
    w = (et(2024, 3, 4, 10, 0), et(2024, 3, 4, 16, 0))
    assert window_state(et(2024, 3, 4, 9, 59), w) is WindowState.EARLY
    assert window_state(et(2024, 3, 4, 10, 0), w) is WindowState.OPEN
    assert window_state(et(2024, 3, 4, 15, 59), w) is WindowState.OPEN
    assert window_state(et(2024, 3, 4, 16, 0), w) is WindowState.EXPIRED
