"""Trading-calendar reads that fail closed (§4.6). Pure: contracts only.

Session facts alone cannot tell a holiday from a missing fetch. A date is known only inside a range
the calendar was fetched for completely (`CalendarCoverage`): there a date without a session is a
closed day, anywhere else it is unknown. Nothing here looks at weekdays, and the newest stored
session date is never treated as coverage.

`build_calendar` takes everything visible at one ``as_of`` (every session version and every coverage
row) and refuses the view unless each coverage row's hash matches exactly the session versions that
were current when it was written, so a coverage row can never expose a newer revision to an older
read.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from contracts.market_data import CalendarCoverage, TradingSession, calendar_hash

# Upper bound on a walk over dates. Coverage ends the walk long before this in practice; the bound
# only guarantees termination for a pathological view.
_MAX_WALK_DAYS = 3660


class CalendarCoverageError(RuntimeError):
    """The calendar cannot answer: an uncovered date, an inconsistent view or a missing session."""


class SessionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"  # inside confirmed coverage, no session: a holiday or weekend
    UNCOVERED = "uncovered"  # in no confirmed range: unknown, never assumed closed


def _latest(sessions: Sequence[TradingSession]) -> TradingSession:
    return max(sessions, key=lambda s: (s.available_at, s.source_version))


@dataclass(frozen=True)
class TradingCalendar:
    sessions: dict[date, TradingSession]
    ranges: tuple[tuple[date, date], ...]

    def status(self, day: date) -> SessionStatus:
        if not any(lo <= day <= hi for lo, hi in self.ranges):
            return SessionStatus.UNCOVERED
        return SessionStatus.OPEN if day in self.sessions else SessionStatus.CLOSED

    def session(self, day: date) -> TradingSession:
        status = self.status(day)
        if status is SessionStatus.UNCOVERED:
            raise CalendarCoverageError(f"{day} is not covered by a confirmed calendar range")
        if status is SessionStatus.CLOSED:
            raise CalendarCoverageError(f"{day} is not a trading session")
        return self.sessions[day]

    def _step(self, day: date, direction: int) -> date:
        """The nearest session strictly after (+1) or before (-1) ``day`` over covered dates."""
        cursor = day
        for _ in range(_MAX_WALK_DAYS):
            cursor += timedelta(days=direction)
            status = self.status(cursor)
            if status is SessionStatus.UNCOVERED:
                side = "after" if direction > 0 else "before"
                raise CalendarCoverageError(f"calendar coverage ends before a session {side} {day}")
            if status is SessionStatus.OPEN:
                return cursor
        raise CalendarCoverageError(f"no session found from {day}")  # pragma: no cover

    def session_closing_after(self, instant: datetime, start_day: date) -> TradingSession:
        """The first session, from ``start_day`` on, that has not closed at ``instant``."""
        day = start_day
        for _ in range(_MAX_WALK_DAYS):
            status = self.status(day)
            if status is SessionStatus.UNCOVERED:
                raise CalendarCoverageError(
                    f"calendar coverage ends before a session after {instant}"
                )
            if status is SessionStatus.OPEN and self.sessions[day].close_at > instant:
                return self.sessions[day]
            day += timedelta(days=1)
        raise CalendarCoverageError(f"no session found after {instant}")  # pragma: no cover

    def d0(self, as_of_session: date) -> date:
        """The entry session: the next stored session after the week-final ``as_of`` session."""
        return self._step(self.session(as_of_session).session_date, +1)

    def next_session(self, day: date) -> date:
        return self._step(day, +1)

    def previous_session(self, day: date) -> date:
        """The latest session strictly before ``day`` (a session date or any covered date)."""
        return self._step(day, -1)

    def horizon_session(self, d0: date, horizon: int) -> date:
        """The session ``horizon`` trading days after ``d0`` (D0 + h), stored sessions only."""
        if horizon < 1:
            raise ValueError("horizon is at least one session")
        cursor = self.session(d0).session_date
        for _ in range(horizon):
            cursor = self._step(cursor, +1)
        return cursor


def build_calendar(
    sessions: Sequence[TradingSession], coverage: Sequence[CalendarCoverage]
) -> TradingCalendar:
    """The calendar as of one point in time from every visible session version and coverage row."""
    by_range: dict[tuple[str, date, date], CalendarCoverage] = {}
    for c in coverage:
        key = (c.source, c.range_start, c.range_end)
        if key not in by_range or (c.available_at, c.source_version) > (
            by_range[key].available_at,
            by_range[key].source_version,
        ):
            by_range[key] = c
    versions: dict[date, list[TradingSession]] = {}
    for s in sessions:
        versions.setdefault(s.session_date, []).append(s)

    # Each chosen coverage row must describe exactly the versions current when it was written.
    for c in by_range.values():
        current = [
            _latest([s for s in versions[d] if s.available_at <= c.available_at])
            for d in versions
            if c.range_start <= d <= c.range_end
            and any(s.available_at <= c.available_at for s in versions[d])
        ]
        if len(current) != c.session_count or calendar_hash(current) != c.sessions_sha256:
            raise CalendarCoverageError(
                f"coverage {c.range_start}..{c.range_end} does not match its visible sessions"
            )

    # For every covered date the newest coverage row must be at least as new as the newest session.
    effective: dict[date, TradingSession] = {}
    for d, vs in versions.items():
        newest = _latest(vs)
        covering = [c for c in by_range.values() if c.range_start <= d <= c.range_end]
        if not covering:
            continue  # a session outside every confirmed range is not evidence of anything
        if max(c.available_at for c in covering) < newest.available_at:
            raise CalendarCoverageError(f"session {d} was revised without a newer coverage record")
        effective[d] = newest
    ranges = tuple(sorted({(c.range_start, c.range_end) for c in by_range.values()}))
    return TradingCalendar(sessions=effective, ranges=ranges)
