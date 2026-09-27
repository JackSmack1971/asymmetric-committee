"""Shared fixtures for the P6.3 market-data tests: a 2024 exchange calendar, trades, fakes.

The calendar generator uses weekdays *to build a fixture*; production code never does (it reads
stored sessions inside confirmed coverage).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

from contracts.enums import Tape
from contracts.market_data import CalendarCoverage, SipTrade, TradingSession, calendar_hash
from execution.trade_conditions import (
    CTS_LAST,
    UTP_LAST,
    ProviderCodeMap,
    ProviderEntry,
    load_provider_map,
)
from ingest.timeutil import ET

HOLIDAYS_2024 = frozenset(
    date(2024, m, d)
    for m, d in [
        (1, 1),
        (1, 15),
        (2, 19),
        (3, 29),
        (5, 27),
        (6, 19),
        (7, 4),
        (9, 2),
        (11, 28),
        (12, 25),
    ]
)
EARLY_CLOSES_2024 = frozenset({date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24)})
FETCHED = datetime(2024, 1, 2, 12, 0, tzinfo=UTC)


def et(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=ET)


def session_for(
    day: date, *, available_at: datetime = FETCHED, version: str = "v1"
) -> TradingSession:
    close = time(13, 0) if day in EARLY_CLOSES_2024 else time(16, 0)
    open_at = datetime.combine(day, time(9, 30), ET)
    return TradingSession(
        event_time=open_at,
        available_at=available_at,
        source_version=version,
        session_date=day,
        open_at=open_at,
        close_at=datetime.combine(day, close, ET),
    )


def sessions_2024(
    start: date = date(2024, 1, 2), end: date = date(2024, 12, 31), **kw: object
) -> list[TradingSession]:
    out: list[TradingSession] = []
    day = start
    while day <= end:
        if day.weekday() < 5 and day not in HOLIDAYS_2024:
            out.append(session_for(day, **kw))  # type: ignore[arg-type]
        day += timedelta(days=1)
    return out


def coverage_for(
    sessions: Sequence[TradingSession], start: date, end: date, *, available_at: datetime = FETCHED
) -> CalendarCoverage:
    digest = calendar_hash(sessions)
    return CalendarCoverage(
        source="alpaca_calendar",
        range_start=start,
        range_end=end,
        available_at=available_at,
        session_count=len(sessions),
        sessions_sha256=digest,
        source_version=f"calendar_coverage:{digest[:16]}",
    )


def trade(
    at: datetime,
    price: float,
    conditions: Iterable[str] = ("@",),
    *,
    tape: Tape = Tape.C,
    symbol: str = "SPY",
    size: float = 100,
    trade_id: str | None = None,
) -> SipTrade:
    return SipTrade(
        symbol=symbol,
        time=at,
        price=price,
        size=size,
        tape=tape,
        conditions=tuple(conditions),
        trade_id=trade_id,
    )


def validated_map_for_tests() -> ProviderCodeMap:
    """The shipped map with every entry marked validated, for offline tests only.

    Production code has no way to build this: it can only load the checked-in file, where every
    entry is ``validated: false``.
    """
    return ProviderCodeMap(replace(e, validated=True) for e in load_provider_map().entries)


def spec_codes(tape: Tape) -> list[str]:
    return list((UTP_LAST if tape is Tape.C else CTS_LAST).keys())


def entries(*items: ProviderEntry) -> ProviderCodeMap:
    return ProviderCodeMap(items)
