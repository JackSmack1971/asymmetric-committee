"""Scheduling rules over the broker's official trading calendar (§9, §11).

The calendar is Alpaca's (``GET /v2/calendar``), so holidays and early closes come from the
exchange calendar the broker itself trades on, not from a table kept here. Two rules:

- The weekly decision run is taken at the close of the last session of each ISO week
  (``weekly_as_of``): "Friday's close", or Thursday's when Friday is a holiday.
- Its orders may go out only in the *first session after* that close, from its open plus 30
  minutes (§9) until its close (``execution_window``). A run that is still unexecuted after that
  window is stale and is never traded; a new run must be made.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from contracts.models import MarketSession
from ingest.timeutil import ET

EXECUTION_DELAY = timedelta(minutes=30)
_LOOKAROUND_DAYS = 14


class MarketCalendar(Protocol):
    def sessions(self, start: date, end: date) -> list[MarketSession]: ...


class WindowState(StrEnum):
    EARLY = "early"
    OPEN = "open"
    EXPIRED = "expired"


class NoSessionError(RuntimeError):
    """The calendar returned no session where one must exist: do not guess a schedule."""


def execution_window(
    cal: MarketCalendar, as_of: datetime, *, delay: timedelta = EXECUTION_DELAY
) -> tuple[datetime, datetime]:
    """``(start, end)`` in which a run committed at ``as_of`` may trade."""
    first_day = as_of.astimezone(ET).date() + timedelta(days=1)
    upcoming = [
        s
        for s in cal.sessions(first_day, first_day + timedelta(days=_LOOKAROUND_DAYS))
        if s.session_date >= first_day
    ]
    if not upcoming:
        raise NoSessionError(f"no trading session after {as_of.isoformat()}")
    nxt = upcoming[0]
    return nxt.opens_at + delay, nxt.closes_at


def window_state(now: datetime, window: tuple[datetime, datetime]) -> WindowState:
    start, end = window
    if now < start:
        return WindowState.EARLY
    return WindowState.OPEN if now < end else WindowState.EXPIRED


def weekly_as_of(cal: MarketCalendar, now: datetime) -> datetime | None:
    """The close of the last session of a week, if ``now`` is at or after it and it is the latest
    such close; ``None`` when the most recent session close is not a week-final one."""
    today = now.astimezone(ET).date()
    sessions = cal.sessions(
        today - timedelta(days=_LOOKAROUND_DAYS), today + timedelta(days=_LOOKAROUND_DAYS)
    )
    closed = [s for s in sessions if s.closes_at <= now]
    if not closed:
        return None
    last = closed[-1]
    later = [s for s in sessions if s.session_date > last.session_date]
    if not later:
        raise NoSessionError(f"calendar has no session after {last.session_date}")
    same_week = later[0].session_date.isocalendar()[:2] == last.session_date.isocalendar()[:2]
    return None if same_week else last.closes_at
