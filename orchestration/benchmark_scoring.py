"""Ticket-gated point-in-time assembly for P6.6 benchmark weeks."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection

from agents.quant_baseline import decisions as quant_decisions
from config.loader import RiskConfig, SectorsConfig
from contracts.benchmarks import BenchmarkTrialIdentity
from contracts.commitment import commitment_hash
from contracts.corporate_actions import SymbolResolver
from contracts.data import FeatureRow, PriceBar
from contracts.enums import OutcomeCompleteness, RefStatus
from contracts.market_data import BenchmarkPeriodReference, ExecutionReference
from contracts.models import BenchmarkReplayContext, CommitteeDecisionRecord, RunRecord
from evaluation.benchmarks import (
    BenchmarkEvidenceError,
    BenchmarkWeekInputs,
    benchmark_inputs_from_result,
    estimate_effective_spread,
    evaluation_parameters_sha256,
    reconstruct_predecessor_books,
)
from evaluation.returns import OutcomeUnresolved, weekly_period_return
from evaluation.scorable import Completeness, ScoringTicket
from evaluation.tbill_rules import Unresolved, accrue_period
from orchestration.benchmarks import build_trial_identity
from risk.sizing import size_book
from store import as_of

_ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)


def _security_reference(
    security_id: int, references: list[ExecutionReference], resolver: SymbolResolver
) -> ExecutionReference | None:
    matches = [
        reference
        for reference in references
        if (
            resolver.ever_held(reference.symbol_ref)
            if reference.session_date is None
            else {resolver.resolve(reference.symbol_ref, reference.session_date)}
        )
        == {security_id}
    ]
    return matches[0] if len(matches) == 1 else None


def _trial_identity(
    conn: Connection, run: RunRecord
) -> tuple[
    BenchmarkTrialIdentity, BenchmarkReplayContext, tuple[CommitteeDecisionRecord, ...], str
]:
    loaded, commitment, verdicts, decisions, portfolio, anchor, context, gates = (
        as_of.benchmark_trial_material(conn, run.run_id)
    )
    if (
        loaded is None
        or loaded != run
        or commitment is None
        or portfolio is None
        or anchor is None
        or context is None
    ):
        raise BenchmarkEvidenceError("durable trial identity evidence is incomplete")
    if commitment.sha256 != commitment_hash(run, decisions, portfolio):
        raise BenchmarkEvidenceError("candidate run commitment does not match stored decisions")
    if context.evaluation_parameters_sha256 != evaluation_parameters_sha256():
        raise BenchmarkEvidenceError("replay context uses different P6.6 evaluation parameters")
    identity = build_trial_identity(
        run=run,
        replay_context=context,
        verdict_records=verdicts,
        portfolio=portfolio,
        anchor=anchor,
        gate_decisions=gates,
    )
    return identity, context, decisions, commitment.sha256


def _prior_books(
    conn: Connection,
    *,
    run: RunRecord,
    identity: BenchmarkTrialIdentity,
    ticket: ScoringTicket,
    entry_session: date,
) -> dict[str, Any]:
    candidates = as_of.prior_committed_trial_runs(conn, run, cutoff=ticket.requested_at)
    matches: list[RunRecord] = []
    matched_commitments: dict[object, str] = {}
    for candidate in candidates:
        candidate_identity, _, _, candidate_commitment = _trial_identity(conn, candidate)
        if candidate_identity.identity_sha256 == identity.identity_sha256:
            matches.append(candidate)
            matched_commitments[candidate.run_id] = candidate_commitment
    if not matches:
        return {"first_trial_week": True}

    previous_run = matches[0]  # store query orders candidates newest first
    results = as_of.benchmark_results(conn, previous_run.run_id)
    if not results:
        raise BenchmarkEvidenceError("adjacent committed predecessor has no benchmark results")
    if any(result.outcome_cutoff > ticket.requested_at for result in results):
        raise BenchmarkEvidenceError("predecessor result was not knowable at the current cutoff")
    previous_inputs = benchmark_inputs_from_result(results[0])
    if previous_inputs.outcome_cutoff > ticket.requested_at:
        raise BenchmarkEvidenceError("predecessor replay inputs follow the current cutoff")
    if (
        any(
            result.commitment_sha256 != matched_commitments[previous_run.run_id]
            for result in results
        )
        or previous_inputs.commitment_sha256 != matched_commitments[previous_run.run_id]
    ):
        raise BenchmarkEvidenceError("predecessor results do not match its verified commitment")
    endpoint_sessions = {ref.session_date for ref in previous_inputs.period_references.values()}
    if len(endpoint_sessions) != 1 or None in endpoint_sessions:
        raise BenchmarkEvidenceError("predecessor endpoint evidence is incomplete")
    endpoint = next(iter(endpoint_sessions))
    assert endpoint is not None
    predecessor = reconstruct_predecessor_books(
        previous_inputs=previous_inputs,
        previous_results=results,
        current_trial_identity_sha256=identity.identity_sha256,
        expected_previous_week_start=previous_inputs.week_start,
        previous_period_end_session=endpoint,
        current_entry_session=entry_session,
    )
    return {
        "first_trial_week": False,
        "previous_weights": predecessor.previous_weights,
        "committee_previous_weights": predecessor.committee_previous_weights,
        "random_previous_books": predecessor.random_previous_books,
        "previous_run_id": predecessor.run_id,
        "previous_trial_identity_sha256": predecessor.trial_identity_sha256,
        "previous_week_start": predecessor.week_start,
        "expected_previous_week_start": previous_inputs.week_start,
        "previous_period_end_session": predecessor.period_end_session,
        "entry_session": entry_session,
        "previous_completeness": predecessor.completeness,
    }


def _halt_adjustment_inputs(
    conn: Connection,
    ticket: ScoringTicket,
    *,
    calendar: Any,
    week_start: date,
    endpoint_day: date,
    entry_refs: dict[int, ExecutionReference],
    return_ids: set[int],
    rates: Any,
    tbill_coverage: Any,
) -> dict[str, Any]:
    """Assemble halt-only returns and T-bills without making unadjusted results depend on them."""
    event = as_of.first_kill_switch_event(conn, ticket.run_id)
    if event is None:
        raise BenchmarkEvidenceError("durable first halt trigger is missing")
    trigger = event.trigger
    symbol_set = as_of.halt_symbol_set(conn, ticket.run_id, trigger.value)
    marks = as_of.halt_references(conn, ticket.run_id, trigger.value)
    required_symbols = {entry_refs[sid].symbol_ref: sid for sid in return_ids}
    if (
        symbol_set is None
        or symbol_set.trigger is not trigger
        or not set(required_symbols) <= set(symbol_set.symbols)
    ):
        raise BenchmarkEvidenceError("canonical halt symbol set is incomplete")
    by_symbol = {mark.symbol_ref: mark for mark in marks}
    if not set(required_symbols) <= set(by_symbol):
        raise BenchmarkEvidenceError("required halt marks are incomplete")
    pre_halt: dict[int, float] = {}
    continuation: dict[int, float] = {}
    continuation_steps: dict[int, tuple[Any, ...]] = {}
    mark_provenance: dict[int, dict[str, object]] = {}
    for symbol, security_id in required_symbols.items():
        mark = by_symbol[symbol]
        entry = entry_refs[security_id]
        mark_day = mark.observed_at.astimezone(_ET).date()
        if (
            mark.trigger is not trigger
            or mark.status is not RefStatus.RESOLVED
            or mark.price is None
            or mark.available_at > ticket.requested_at
            or mark.observed_at < event.triggered_at
            or mark_day not in calendar.sessions
            or mark.symbol_set_source != symbol_set.source
            or mark.symbol_set_version != symbol_set.source_version
        ):
            raise BenchmarkEvidenceError(f"halt mark is ineligible for {symbol}")
        mark_reference = BenchmarkPeriodReference(
            run_id=ticket.run_id,
            symbol_ref=symbol,
            status=mark.status,
            price=mark.price,
            source=mark.source,
            trade_time=mark.trade_time,
            trade_tape=mark.trade_tape,
            trade_conditions=mark.trade_conditions,
            trade_id=mark.trade_id,
            ref_time=mark.observed_at,
            mode=entry.mode,
            session_date=mark_day,
            available_at=mark.available_at,
            source_version=mark.source_version,
            period_start=week_start,
        )
        actions = as_of.corporate_actions(
            conn,
            [security_id],
            ticket.requested_at,
            process_from=week_start + timedelta(days=1),
            process_to=mark_day,
        )
        coverage = as_of.action_coverage(conn, security_id, ticket.requested_at)
        delisting = as_of.delisting(conn, security_id, ticket.requested_at)
        try:
            pre = weekly_period_return(
                security_id=security_id,
                entry_reference=entry,
                endpoint_reference=mark_reference,
                calendar=calendar,
                actions=actions,
                coverage=coverage,
                delisting=delisting,
                cutoff=ticket.requested_at,
            )
        except OutcomeUnresolved as exc:
            raise BenchmarkEvidenceError(f"pre-halt return unresolved for {symbol}: {exc}") from exc
        pre_halt[security_id] = pre.value
        if mark_day == endpoint_day:
            continuation[security_id] = 0.0
            continuation_steps[security_id] = ()
        else:
            post = accrue_period(
                rates,
                tbill_coverage,
                mark_day,
                endpoint_day,
                calendar,
                cutoff=ticket.requested_at,
            )
            if isinstance(post, Unresolved):
                raise BenchmarkEvidenceError(
                    f"halt T-bill continuation unresolved for {symbol}: {post.reason.value}"
                )
            continuation[security_id] = post.value
            continuation_steps[security_id] = post.steps
        mark_provenance[security_id] = mark.model_dump(mode="json")
    return {
        "pre_halt_returns": pre_halt,
        "post_halt_tbill_returns": continuation,
        "post_halt_tbill_steps": continuation_steps,
        "halt_tau": event.triggered_at,
        "halt_trigger": trigger.value,
        "halt_event_provenance": event.model_dump(mode="json"),
        "halt_symbol_set_provenance": symbol_set.model_dump(mode="json"),
        "halt_reference_provenance": mark_provenance,
    }


def build_benchmark_week_inputs(
    conn: Connection,
    ticket: ScoringTicket,
    sectors: SectorsConfig,
) -> BenchmarkWeekInputs:
    """Build all inputs for one admitted week from canonical point-in-time readers."""
    if not sectors.confirmed:
        raise BenchmarkEvidenceError("sector crosswalk is not owner-confirmed")
    material = as_of.benchmark_trial_material(conn, ticket.run_id)
    run, commitment, _verdicts, decisions, portfolio, anchor, context, gates = material
    if run is None or commitment is None or portfolio is None or anchor is None or context is None:
        raise BenchmarkEvidenceError("committed benchmark inputs are incomplete")
    if run.as_of != ticket.run_as_of or commitment.sha256 != ticket.commitment_sha256:
        raise BenchmarkEvidenceError("benchmark material does not match the scoring ticket")
    halted = ticket.completeness is Completeness.HALTED
    identity, context, decisions, _ = _trial_identity(conn, run)

    scope = as_of.committed_security_scope(conn, ticket)
    if not scope or set(scope) != {bundle.security_id for bundle in context.random_bundles}:
        raise BenchmarkEvidenceError("replay bundles do not match committed decision scope")
    if {record.decision.security_id for record in decisions} != set(scope):
        raise BenchmarkEvidenceError("stored strategy decisions do not match committed scope")
    universe = tuple(member.security_id for member in as_of.universe(conn, run.as_of))
    if not universe or set(scope) - set(universe):
        raise BenchmarkEvidenceError("committed scope is outside the point-in-time universe")

    calendar = as_of.trading_calendar(conn, ticket.requested_at)
    raw_entry = as_of.execution_references(conn, ticket.run_id, as_of=ticket.requested_at)
    raw_period = as_of.benchmark_period_references(conn, ticket.run_id, as_of=ticket.requested_at)
    period_by_symbol = {ref.symbol_ref: ref for ref in raw_period}
    refs_by_symbol = {ref.symbol_ref: ref for ref in raw_entry}
    if len(refs_by_symbol) != len(raw_entry) or len(period_by_symbol) != len(raw_period):
        raise BenchmarkEvidenceError("duplicate stored weekly reference symbols")

    instruments = {
        instrument.ticker: instrument.security_id
        for instrument in as_of.reference_instruments(conn)
    }
    spy_id = instruments.get("SPY")
    if spy_id is None:
        raise BenchmarkEvidenceError("SPY reference instrument is missing")
    securities = {security.security_id: security for security in as_of.securities(conn, scope)}
    if set(securities) != set(scope):
        raise BenchmarkEvidenceError("committed equity scope has missing security records")
    resolver = SymbolResolver(as_of.symbol_history(conn, ticket.requested_at))
    sector_by_id: dict[int, str] = {}
    sector_etf: dict[str, int] = {}
    for security_id in scope:
        sic = as_of.sic_history(conn, security_id, run.as_of, valid_at=run.as_of)
        sector_entry = sectors.sector_for_sic(sic.sic) if sic is not None else None
        if sector_entry is None or sector_entry.etf not in instruments:
            raise BenchmarkEvidenceError(f"point-in-time sector mapping missing for {security_id}")
        sector_by_id[security_id] = sector_entry.sector
        sector_etf[sector_entry.sector] = instruments[sector_entry.etf]

    return_ids = set(scope) | {spy_id} | set(sector_etf.values())
    entry_refs: dict[int, ExecutionReference] = {}
    endpoint_refs: dict[int, BenchmarkPeriodReference] = {}
    for security_id in sorted(return_ids):
        if security_id in securities:
            entry_reference = _security_reference(security_id, raw_entry, resolver)
            if entry_reference is None:
                raise BenchmarkEvidenceError(f"entry reference missing for security {security_id}")
        else:
            instrument = next((x for x in instruments.items() if x[1] == security_id), None)
            if instrument is None:
                raise BenchmarkEvidenceError(f"reference instrument {security_id} is unknown")
            entry_reference = refs_by_symbol.get(instrument[0])
            if entry_reference is None:
                raise BenchmarkEvidenceError(f"entry reference missing for {instrument[0]}")
        endpoint = period_by_symbol.get(entry_reference.symbol_ref)
        if (
            endpoint is None
            or entry_reference.status is not RefStatus.RESOLVED
            or endpoint.status is not RefStatus.RESOLVED
        ):
            raise BenchmarkEvidenceError(
                f"weekly references unresolved for {entry_reference.symbol_ref}"
            )
        entry_refs[security_id] = entry_reference
        endpoint_refs[security_id] = endpoint

    week_starts = {ref.session_date for ref in entry_refs.values()}
    endpoint_days = {ref.session_date for ref in endpoint_refs.values()}
    if (
        len(week_starts) != 1
        or None in week_starts
        or len(endpoint_days) != 1
        or None in endpoint_days
    ):
        raise BenchmarkEvidenceError("weekly references disagree on their session dates")
    week_start = next(iter(week_starts))
    endpoint_day = next(iter(endpoint_days))
    assert week_start is not None and endpoint_day is not None
    scheduled_entry = calendar.next_session(run.as_of.astimezone(_ET).date())
    scheduled_endpoint = calendar.d0(run.as_of.astimezone(_ET).date() + timedelta(days=7))
    if week_start != scheduled_entry or endpoint_day != scheduled_endpoint:
        raise BenchmarkEvidenceError("weekly references do not match the scheduled sessions")

    returns: dict[int, float] = {}
    for security_id in sorted(return_ids):
        actions = as_of.corporate_actions(
            conn,
            [security_id],
            ticket.requested_at,
            process_from=week_start + timedelta(days=1),
            process_to=endpoint_day,
        )
        action_coverages = as_of.action_coverage(conn, security_id, ticket.requested_at)
        delisting = as_of.delisting(conn, security_id, ticket.requested_at)
        try:
            result = weekly_period_return(
                security_id=security_id,
                entry_reference=entry_refs[security_id],
                endpoint_reference=endpoint_refs[security_id],
                calendar=calendar,
                actions=actions,
                coverage=action_coverages,
                delisting=delisting,
                cutoff=ticket.requested_at,
            )
        except OutcomeUnresolved as exc:
            raise BenchmarkEvidenceError(
                f"weekly return unresolved for {security_id}: {exc}"
            ) from exc
        returns[security_id] = result.value

    feature_version_by_id = {gate.security_id: gate.feature_set_version for gate in gates}
    if set(feature_version_by_id) != set(scope):
        raise BenchmarkEvidenceError("stored gate evidence does not cover committed scope")
    features: dict[int, FeatureRow] = {}
    for version in sorted(set(feature_version_by_id.values())):
        rows = as_of.feature_rows(conn, scope, run.as_of, version)
        for row in rows:
            if feature_version_by_id.get(row.security_id) == version:
                features[row.security_id] = row
    if set(features) != set(scope):
        raise BenchmarkEvidenceError("point-in-time features do not cover committed scope")
    volatilities: dict[int, float] = {}
    for security_id, row in features.items():
        value = row.values.get("realized_vol_20d")
        if not isinstance(value, (int, float)) or not isfinite(float(value)) or float(value) <= 0:
            raise BenchmarkEvidenceError(f"realized volatility missing for {security_id}")
        volatilities[security_id] = float(value)
    tokens = {record.decision.security_id: record.decision.entity_token for record in decisions}
    quant_decision_rows = quant_decisions(
        rows=tuple(features[sid] for sid in sorted(scope)),
        run_id=ticket.run_id,
        as_of=run.as_of,
        entity_tokens=tokens,
    )
    risk_config = RiskConfig.model_validate(context.risk_config_snapshot)
    quant_book = size_book(
        decisions=quant_decision_rows,
        sectors=sector_by_id,
        volatilities=volatilities,
        config=risk_config,
    )
    quant_weights = {
        position.security_id: position.target_weight for position in quant_book.positions
    }
    strategy_weights = {
        position.security_id: position.target_weight for position in portfolio.book.positions
    }
    committee_records = [
        record for record in decisions if record.decision.horizon_days is context.sizing_horizon
    ]
    recorded_weights = {
        record.decision.security_id: record.decision.target_weight for record in committee_records
    }
    if recorded_weights != {sid: strategy_weights.get(sid, 0.0) for sid in scope}:
        raise BenchmarkEvidenceError("committee weights disagree with committed portfolio")

    input_kwargs = _prior_books(
        conn,
        run=run,
        identity=identity,
        ticket=ticket,
        entry_session=week_start,
    )
    cost_ids = set(return_ids)
    for books in input_kwargs.get("previous_weights", {}).values():
        for book in books.values():
            cost_ids.update(book)
    for book in input_kwargs.get("committee_previous_weights", {}).values():
        cost_ids.update(book)
    for books in input_kwargs.get("random_previous_books", {}).values():
        for book in books:
            cost_ids.update(book)

    cost_sessions = tuple(day for day in sorted(calendar.sessions) if day < week_start)[-22:]
    if len(cost_sessions) != 22:
        raise BenchmarkEvidenceError("calendar lacks 22 eligible SIP spread sessions")
    spread_bars = as_of.prices_between(
        conn, cost_ids, cost_sessions[0], cost_sessions[-1], run.as_of
    )
    bars_by_id: dict[int, list[PriceBar]] = {sid: [] for sid in cost_ids}
    for bar in spread_bars:
        bars_by_id.setdefault(bar.security_id, []).append(bar)
    spreads = {
        security_id: estimate_effective_spread(
            bars=bars_by_id[security_id],
            sessions=cost_sessions,
            security_id=security_id,
            cutoff=run.as_of,
        )
        for security_id in sorted(cost_ids)
    }

    tbill_coverage = as_of.tbill_vintage_coverage(conn, "DGS3MO", ticket.requested_at)
    rates = as_of.tbill_rates(conn, "DGS3MO", endpoint_day)
    accrued = accrue_period(
        rates, tbill_coverage, week_start, endpoint_day, calendar, cutoff=ticket.requested_at
    )
    if isinstance(accrued, Unresolved):
        raise BenchmarkEvidenceError(f"T-bill period is unresolved: {accrued.reason.value}")
    halt_adjustment: dict[str, Any] = {}
    if halted:
        try:
            halt_adjustment = _halt_adjustment_inputs(
                conn,
                ticket,
                calendar=calendar,
                week_start=week_start,
                endpoint_day=endpoint_day,
                entry_refs=entry_refs,
                return_ids=return_ids,
                rates=rates,
                tbill_coverage=tbill_coverage,
            )
        except (BenchmarkEvidenceError, ValueError, KeyError) as exc:
            log.info("HALTED adjusted benchmark variant unresolved for %s: %s", ticket.run_id, exc)
    fit = next(
        (fit for fit in context.calibration_fits if fit.horizon is context.sizing_horizon), None
    )
    if fit is None:
        raise BenchmarkEvidenceError("random-control calibration fit is missing")
    return BenchmarkWeekInputs(
        run_id=ticket.run_id,
        run_as_of=ticket.run_as_of,
        week_start=week_start,
        entry_references=entry_refs,
        period_references=endpoint_refs,
        cost_cutoff=ticket.run_as_of,
        outcome_cutoff=ticket.requested_at,
        commitment_sha256=ticket.commitment_sha256,
        trial_identity=identity,
        completeness=(OutcomeCompleteness.HALTED if halted else OutcomeCompleteness.COMPLETE),
        spy_security_id=spy_id,
        universe_security_ids=tuple(sorted(scope)),
        committee_weights=strategy_weights,
        committed_scope_ids=tuple(sorted(scope)),
        sector_by_security_id=sector_by_id,
        sector_etf_by_sector=sector_etf,
        quant_weights=quant_weights,
        random_bundles={bundle.security_id: bundle for bundle in context.random_bundles},
        random_entity_tokens=tokens,
        random_calibration_fit=fit,
        random_risk_config=risk_config,
        volatilities=volatilities,
        period_returns=returns,
        spreads=spreads,
        tbill_return=accrued.value,
        tbill_accrual_steps=accrued.steps,
        halted=halted,
        **halt_adjustment,
        **input_kwargs,
    )
