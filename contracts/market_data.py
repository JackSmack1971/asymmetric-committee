"""Market-data foundations (P6.3): calendar, benchmark instruments, DGS3MO vintages, references.

Nothing here is LLM-facing. Reference rows are run-scoped and insert-only; a reference that cannot
be resolved is stored as ``unresolved`` with a reason and is never replaced by a daily price.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Annotated, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from contracts.data import Fact, Ticker, Version
from contracts.enums import (
    HaltRequestStatus,
    KillTrigger,
    ReferenceSource,
    RefMode,
    RefReason,
    RefStatus,
    SecurityKind,
    Tape,
)
from contracts.models import Contract, Finite, Label, NonNegative, Positive, Sha256Hex

Series = Annotated[str, Field(pattern=r"^[A-Z0-9]{1,32}$")]


def canonical_utc(value: datetime) -> str:
    """One text form for an instant, independent of the zone it was given in."""
    if value.tzinfo is None:
        raise ValueError("naive datetime")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class TradingSession(Fact):
    """One exchange session as the broker's calendar stated it at ``available_at``.

    ``event_time`` is the session open. A revised session is a new row (new ``source_version``).
    """

    session_date: date
    open_at: AwareDatetime
    close_at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.close_at <= self.open_at:
            raise ValueError("a session closes after it opens")
        if self.event_time != self.open_at:
            raise ValueError("a session's event_time is its open")
        return self


def calendar_hash(sessions: Iterable[TradingSession]) -> str:
    """Canonical digest of a set of session versions.

    Sorted by date, one line per session (`date|open|close|source_version`, instants in UTC), so it
    is independent of row, dict or JSON order and of the zone an instant was written in.
    """
    lines = [
        "|".join(
            (
                s.session_date.isoformat(),
                canonical_utc(s.open_at),
                canonical_utc(s.close_at),
                s.source_version,
            )
        )
        for s in sorted(sessions, key=lambda s: (s.session_date, s.source_version))
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


class CalendarCoverage(Contract):
    """A date range whose sessions were fetched completely and successfully at ``available_at``.

    Inside a confirmed range a date with no session is a closed day; outside every confirmed
    range it is unknown. ``sessions_sha256`` is `calendar_hash` of exactly the sessions written
    with it.
    """

    source: Label
    range_start: date
    range_end: date
    available_at: AwareDatetime
    session_count: int = Field(ge=0)
    sessions_sha256: Sha256Hex
    source_version: Version

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.range_end < self.range_start:
            raise ValueError("coverage range ends before it starts")
        return self


class TBillObservation(Contract):
    """One DGS3MO observation in one ALFRED vintage.

    NAMED EXCEPTION to the fact-table ``available_at`` rule (§4.3): ``vintage_date`` is ALFRED's
    ``realtime_start``, a date. No release time is invented. For accrual session date P only
    ``vintage_date < P`` is usable (`evaluation.tbill_rules`); ``ingested_at`` (set by the
    database) is when we fetched it and is never evidence of when the value was known.
    """

    series: Series
    observation_date: date
    yield_pct: Finite
    vintage_date: date
    source_version: Version

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.vintage_date < self.observation_date:
            raise ValueError("a value cannot be published before its observation date")
        return self


class TBillVintageCoverage(Contract):
    """Earliest and latest usable vintage, established from the fetched ALFRED vintage-date list."""

    series: Series
    earliest_vintage: date
    latest_vintage: date
    vintage_count: int = Field(ge=1)
    vintage_dates_sha256: Sha256Hex
    established_at: AwareDatetime
    source_version: Version

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.latest_vintage < self.earliest_vintage:
            raise ValueError("latest vintage precedes the earliest")
        return self


class SipTrade(Contract):
    """One historical SIP trade as Alpaca returned it (``c`` conditions, ``z`` tape)."""

    symbol: Ticker
    time: AwareDatetime
    price: Positive
    size: NonNegative
    tape: Tape
    conditions: tuple[Annotated[str, Field(min_length=1, max_length=2)], ...]
    trade_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class _Observed(Contract):
    """The evidence common to every reference: a price with its source, or an unresolved reason."""

    status: RefStatus
    reason: RefReason | None = None
    price: Positive | None = None
    source: ReferenceSource | None = None
    trade_time: AwareDatetime | None = None
    trade_tape: Tape | None = None
    trade_conditions: tuple[str, ...] = ()
    trade_id: str | None = None

    @model_validator(mode="after")
    def _check_observed(self) -> Self:
        if self.status is RefStatus.RESOLVED:
            if self.price is None or self.source is None or self.reason is not None:
                raise ValueError("a resolved reference has a price and source and no reason")
        else:
            if self.reason is None:
                raise ValueError("an unresolved reference states why")
            if self.price is not None or self.source is not None:
                raise ValueError("an unresolved reference has no price and no source")
        return self


class ReferenceObservation(_Observed):
    """Pure resolver output for one symbol at one timestamp (no run, no persistence)."""

    symbol_ref: Ticker
    ref_time: AwareDatetime


class ExecutionReference(_Observed):
    """Durable, insert-only, run-scoped scoring reference (§4.6). Key ``(run_id, symbol_ref)``.

    ``ref_time`` and ``session_date`` are absent only for an unresolved row whose reference day
    could not be determined because the trading calendar was never covered (``calendar_uncovered``).
    """

    run_id: UUID
    symbol_ref: Ticker
    ref_time: AwareDatetime | None
    mode: RefMode
    session_date: date | None
    available_at: AwareDatetime
    source_version: Version

    @model_validator(mode="after")
    def _evidence(self) -> Self:
        undated = self.ref_time is None or self.session_date is None
        if self.status is RefStatus.RESOLVED and undated:
            raise ValueError("a resolved reference states its reference time and session")
        if undated and self.reason is not RefReason.CALENDAR_UNCOVERED:
            raise ValueError("only calendar_uncovered may lack the reference time and session")
        if (
            self.mode is RefMode.BACKTEST
            and self.status is RefStatus.RESOLVED
            and (
                self.source is not ReferenceSource.SIP_LAST
                or self.trade_time is None
                or self.trade_tape is None
            )
        ):
            raise ValueError("a resolved backtest reference is a SIP trade with its time and tape")
        return self


class BenchmarkPeriodReference(ExecutionReference):
    """Same-run reference at the endpoint of one weekly benchmark period (§12.3)."""

    period_start: date

    @model_validator(mode="after")
    def _ordered_period(self) -> Self:
        if self.ref_time is not None and self.available_at < self.ref_time:
            raise ValueError("benchmark endpoint cannot be available before its reference time")
        if self.session_date is not None and self.session_date <= self.period_start:
            raise ValueError("benchmark period endpoint must be after period_start")
        return self


class HaltReferenceRequest(Contract):
    """Written in the halt transaction. It never holds symbols: the sweeper reconstructs them."""

    run_id: UUID
    trigger: KillTrigger
    tau: AwareDatetime
    status: HaltRequestStatus = HaltRequestStatus.SYMBOLS_PENDING
    requested_at: AwareDatetime
    source_version: Version


def symbols_sha256(symbols: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest()


class HaltSymbolSet(Contract):
    """The canonical symbols a halt needs references for (sorted, unique, never empty)."""

    run_id: UUID
    trigger: KillTrigger
    symbols: tuple[Ticker, ...] = Field(min_length=1)
    symbol_count: int = Field(ge=1)
    symbols_sha256: Sha256Hex
    source: Label
    source_version: Version
    resolved_at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        if list(self.symbols) != sorted(set(self.symbols)):
            raise ValueError("halt symbols are sorted and unique")
        if self.symbol_count != len(self.symbols):
            raise ValueError("symbol_count must equal the number of symbols")
        if self.symbols_sha256 != symbols_sha256(self.symbols):
            raise ValueError("symbols_sha256 does not match the symbols")
        return self


class HaltReference(_Observed):
    """Durable halt mark for one symbol. ``observed_at`` is when it was actually observed."""

    run_id: UUID
    trigger: KillTrigger
    symbol_ref: Ticker
    observed_at: AwareDatetime
    lag_seconds: Finite
    symbol_set_source: Label
    symbol_set_version: Version
    available_at: AwareDatetime
    source_version: Version


class ReferenceInstrument(Contract):
    """SPY or a sector ETF (§4.3). It has no CIK: no synthetic identifier exists."""

    security_id: Annotated[int, Field(ge=1)]
    ticker: Ticker
    name: Annotated[str, Field(min_length=1, max_length=256)]
    kind: SecurityKind = SecurityKind.ETF

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.kind is SecurityKind.EQUITY:
            raise ValueError("a reference instrument is not an equity")
        return self
