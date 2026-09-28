"""Admission-first construction and persistence of paired security/sector outcomes."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime

from sqlalchemy import Connection, Engine

from config.loader import SectorsConfig
from contracts.corporate_actions import (
    CorporateAction,
    CorporateActionCoverage,
    Delisting,
    SymbolResolver,
)
from contracts.data import PriceBar
from contracts.enums import Horizon, OutcomeCompleteness, RefStatus
from contracts.market_data import ExecutionReference
from contracts.models import OutcomeRecord
from evaluation.anchoring import HeaderSource
from evaluation.benchmarks import BenchmarkEvidenceError
from evaluation.calendar_rules import CalendarCoverageError, TradingCalendar
from evaluation.returns import OutcomeUnresolved, forward_return
from evaluation.scorable import (
    Completeness,
    GitReachability,
    ScoringRequest,
    ScoringTicket,
    run_scoring,
)
from orchestration.benchmark_scoring import build_benchmark_week_inputs
from orchestration.benchmarks import evaluate_and_persist_admitted_week
from orchestration.sink import DecisionSink
from store import as_of
from store.scoring_read import ScoringReader

_HORIZONS = (Horizon.D5, Horizon.D21, Horizon.D63)
log = logging.getLogger(__name__)

__all__ = ["ScoringReader", "as_of", "score_run"]


def _security_reference(
    security_id: int,
    references: Sequence[ExecutionReference],
    resolver: SymbolResolver,
) -> ExecutionReference | None:
    matches: list[ExecutionReference] = []
    for reference in references:
        if reference.session_date is None:
            owners = resolver.ever_held(reference.symbol_ref)
            owner = next(iter(owners)) if len(owners) == 1 else None
        else:
            owner = resolver.resolve(reference.symbol_ref, reference.session_date)
        if owner == security_id:
            matches.append(reference)
    return matches[0] if len(matches) == 1 else None


def _bar_range_end(
    calendar: TradingCalendar, references: Sequence[ExecutionReference]
) -> date | None:
    ends: list[date] = []
    starts: list[date] = []
    for reference in references:
        if reference.session_date is None:
            continue
        starts.append(reference.session_date)
        for horizon in _HORIZONS:
            try:
                ends.append(calendar.horizon_session(reference.session_date, int(horizon)))
            except CalendarCoverageError:
                continue
    # No horizon fits the calendar yet: read bars through the latest entry session only. Horizons
    # past calendar coverage stay unresolved (no outcome row) rather than being guessed.
    return max(ends, default=max(starts, default=None))


def _build_outcomes(
    conn: Connection, ticket: ScoringTicket, sectors: SectorsConfig
) -> tuple[OutcomeRecord, ...]:
    """Build from one read-only snapshot after admission; unresolved pairs produce no row."""
    if not sectors.confirmed:
        return ()

    scope = as_of.committed_security_scope(conn, ticket)
    if not scope:
        return ()
    references = as_of.execution_references(conn, ticket.run_id, as_of=ticket.requested_at)
    instruments = as_of.reference_instruments(conn)
    instrument_by_ticker = {instrument.ticker: instrument for instrument in instruments}
    resolver = SymbolResolver(as_of.symbol_history(conn, ticket.requested_at))
    calendar = as_of.trading_calendar(conn, ticket.requested_at)

    mappings: dict[int, tuple[int, datetime]] = {}
    security_refs: dict[int, ExecutionReference] = {}
    sector_refs: dict[int, ExecutionReference] = {}
    unresolved = False
    for security_id in scope:
        sic = as_of.sic_history(conn, security_id, ticket.requested_at, valid_at=ticket.run_as_of)
        if sic is None:
            unresolved = True
            continue
        entry = sectors.sector_for_sic(sic.sic)
        instrument = instrument_by_ticker.get(entry.etf) if entry is not None else None
        if entry is None or instrument is None:
            unresolved = True
            continue
        security_ref = _security_reference(security_id, references, resolver)
        sector_ref = next(
            (reference for reference in references if reference.symbol_ref == entry.etf), None
        )
        if (
            security_ref is None
            or sector_ref is None
            or security_ref.status is not RefStatus.RESOLVED
            or sector_ref.status is not RefStatus.RESOLVED
            or security_ref.available_at > ticket.requested_at
            or sector_ref.available_at > ticket.requested_at
            or security_ref.session_date is None
            or security_ref.session_date != sector_ref.session_date
        ):
            unresolved = True
            continue
        mappings[security_id] = (instrument.security_id, sic.available_at)
        security_refs[security_id] = security_ref
        sector_refs[security_id] = sector_ref

    if not mappings:
        return ()

    required_ids = {security_id for security_id in mappings} | {
        sector_id for sector_id, _ in mappings.values()
    }
    entries = tuple(security_refs.values()) + tuple(sector_refs.values())
    earliest = min(ref.session_date for ref in entries if ref.session_date is not None)
    latest = _bar_range_end(calendar, entries)
    if latest is None or latest < earliest:
        latest = earliest

    delistings: dict[int, Delisting | None] = {
        security_id: as_of.delisting(conn, security_id, ticket.requested_at)
        for security_id in sorted(required_ids)
    }
    acquirer_ids = {
        d.acquirer_security_id
        for d in delistings.values()
        if d is not None and d.acquirer_security_id is not None
    }
    all_ids = required_ids | acquirer_ids
    bars_by_id: dict[int, list[PriceBar]] = {security_id: [] for security_id in all_ids}
    for bar in as_of.prices_between(conn, all_ids, earliest, latest, ticket.requested_at):
        bars_by_id.setdefault(bar.security_id, []).append(bar)
    actions_by_id: dict[int, list[CorporateAction]] = {
        security_id: as_of.corporate_actions(conn, [security_id], ticket.requested_at)
        for security_id in sorted(all_ids)
    }
    coverage_by_id: dict[int, list[CorporateActionCoverage]] = {
        security_id: as_of.action_coverage(conn, security_id, ticket.requested_at)
        for security_id in sorted(all_ids)
    }

    outcomes: list[OutcomeRecord] = []
    for security_id in scope:
        mapping = mappings.get(security_id)
        security_ref = security_refs.get(security_id)
        if mapping is None or security_ref is None:
            continue
        sector_id, sic_available_at = mapping
        sector_ref = sector_refs[security_id]
        for horizon in _HORIZONS:
            target_delisting = delistings.get(security_id)
            acquirer_id = (
                target_delisting.acquirer_security_id if target_delisting is not None else None
            )
            try:
                security_return = forward_return(
                    security_id=security_id,
                    horizon=horizon,
                    reference=security_ref,
                    bars=bars_by_id.get(security_id, ()),
                    calendar=calendar,
                    actions=actions_by_id.get(security_id, ()),
                    coverage=coverage_by_id.get(security_id, ()),
                    delisting=target_delisting,
                    cutoff=ticket.requested_at,
                    acquirer_bars=(
                        bars_by_id.get(acquirer_id, ()) if acquirer_id is not None else ()
                    ),
                    acquirer_actions=(
                        actions_by_id.get(acquirer_id, ()) if acquirer_id is not None else ()
                    ),
                    acquirer_coverage=(
                        coverage_by_id.get(acquirer_id, ()) if acquirer_id is not None else ()
                    ),
                )
                sector_return = forward_return(
                    security_id=sector_id,
                    horizon=horizon,
                    reference=sector_ref,
                    bars=bars_by_id.get(sector_id, ()),
                    calendar=calendar,
                    actions=actions_by_id.get(sector_id, ()),
                    coverage=coverage_by_id.get(sector_id, ()),
                    delisting=delistings.get(sector_id),
                    cutoff=ticket.requested_at,
                )
            except OutcomeUnresolved:
                unresolved = True
                continue
            outcomes.append(
                OutcomeRecord(
                    run_id=ticket.run_id,
                    security_id=security_id,
                    horizon=horizon,
                    fwd_return=security_return.value,
                    sector_fwd_return=sector_return.value,
                    scored_at=ticket.requested_at,
                    resolved_at=max(
                        security_return.resolved_at,
                        sector_return.resolved_at,
                        sic_available_at,
                    ),
                )
            )

    if ticket.completeness is Completeness.COMPLETE and unresolved:
        return ()
    return tuple(outcomes)


def score_run(
    request: ScoringRequest,
    *,
    engine: Engine,
    headers: HeaderSource,
    git: GitReachability,
    clock: Callable[[], datetime],
    sectors: SectorsConfig,
) -> tuple[OutcomeRecord, ...]:
    """Admit first, then build paired outcomes and use the existing atomic sink."""
    sink = DecisionSink(engine)

    def admitted_builder(ticket: ScoringTicket) -> tuple[OutcomeRecord, ...]:
        benchmark_inputs = None
        with engine.connect() as raw:
            conn = raw.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            with conn.begin():
                rows = _build_outcomes(conn, ticket, sectors)
                if ticket.completeness is Completeness.HALTED or (
                    rows and ticket.completeness is Completeness.COMPLETE
                ):
                    try:
                        benchmark_inputs = build_benchmark_week_inputs(conn, ticket, sectors)
                    except (BenchmarkEvidenceError, ValueError, CalendarCoverageError) as exc:
                        log.warning("P6.6 benchmark week unresolved for %s: %s", ticket.run_id, exc)
        if rows:
            completeness = (
                OutcomeCompleteness.COMPLETE
                if ticket.may_become_scored
                else OutcomeCompleteness.HALTED
            )
            sink.record_outcomes(rows, completeness=completeness, requested_at=ticket.requested_at)
        if benchmark_inputs is not None:
            try:
                evaluate_and_persist_admitted_week(ticket, benchmark_inputs, sink)
            except (BenchmarkEvidenceError, ValueError) as exc:
                log.warning("P6.6 benchmark week unresolved for %s: %s", ticket.run_id, exc)
        return rows

    return run_scoring(
        request,
        source=ScoringReader(engine),
        headers=headers,
        git=git,
        clock=clock,
        load_outcomes=admitted_builder,
    )
