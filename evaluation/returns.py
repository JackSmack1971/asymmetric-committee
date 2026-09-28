"""Pure, fail-closed forward return construction from admitted point-in-time evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from contracts.corporate_actions import CorporateAction, CorporateActionCoverage, Delisting
from contracts.data import PriceBar
from contracts.enums import (
    ActionInterpretation,
    DelistingStatus,
    Horizon,
    KnowledgeBasis,
    RefStatus,
    TerminalReturnSource,
)
from contracts.market_data import BenchmarkPeriodReference, ExecutionReference
from evaluation.calendar_rules import CalendarCoverageError, TradingCalendar

_ET = ZoneInfo("America/New_York")


class OutcomeUnresolved(ValueError):
    """Required return evidence is absent, late, incomplete, or uninterpretable."""


@dataclass(frozen=True)
class ReturnResult:
    value: float
    exit_session: date
    resolved_at: datetime


def _daily_close(
    bars: Sequence[PriceBar], security_id: int, day: date, cutoff: datetime
) -> PriceBar:
    found = [
        b
        for b in bars
        if b.security_id == security_id
        and b.available_at <= cutoff
        and b.event_time.astimezone(_ET).date() == day
    ]
    if not found:
        raise OutcomeUnresolved(f"missing point-in-time bar for security {security_id} on {day}")
    return max(found, key=lambda b: (b.available_at, b.source_version))


def _action_adjustments(actions: Sequence[CorporateAction]) -> tuple[float, float, datetime | None]:
    """Return cumulative shares and cash per starting share after the supplied actions."""
    ordered = sorted(actions, key=lambda a: (a.ex_date or date.min, a.provider_action_id))
    factors_after: dict[str, float] = {}
    running = 1.0
    for action in reversed(ordered):
        factors_after[action.provider_action_id] = running
        if action.interpretation is ActionInterpretation.SPLIT_FACTOR:
            if action.old_rate is None or action.new_rate is None or action.old_rate <= 0:
                raise OutcomeUnresolved("split is missing a valid ratio")
            running *= action.new_rate / action.old_rate
        elif action.interpretation is ActionInterpretation.STOCK_DISTRIBUTION:
            if action.stock_rate is None:
                raise OutcomeUnresolved("stock distribution is missing its rate")
            running *= 1.0 + action.stock_rate
    cash = sum(
        (action.cash_rate or 0.0) * factors_after[action.provider_action_id]
        for action in ordered
        if action.interpretation is ActionInterpretation.CASH_DIVIDEND
    )
    available = max((action.available_at for action in ordered), default=None)
    return running, cash, available


def forward_return(
    *,
    security_id: int,
    horizon: Horizon,
    reference: ExecutionReference,
    bars: Sequence[PriceBar],
    calendar: TradingCalendar,
    actions: Sequence[CorporateAction],
    coverage: Sequence[CorporateActionCoverage],
    delisting: Delisting | None,
    cutoff: datetime,
    acquirer_bars: Sequence[PriceBar] = (),
    acquirer_actions: Sequence[CorporateAction] = (),
    acquirer_coverage: Sequence[CorporateActionCoverage] = (),
) -> ReturnResult:
    """Compute local split/dividend adjusted arithmetic return; missing scope fails closed."""
    if (
        reference.status is not RefStatus.RESOLVED
        or reference.ref_time is None
        or reference.price is None
    ):
        raise OutcomeUnresolved("execution reference is unresolved")
    if reference.available_at > cutoff:
        raise OutcomeUnresolved("execution reference was not knowable at cutoff")
    entry_day = reference.session_date
    if entry_day is None:
        raise OutcomeUnresolved("execution reference has no session date")
    try:
        exit_day = calendar.horizon_session(entry_day, int(horizon))
    except (CalendarCoverageError, TypeError) as exc:
        raise OutcomeUnresolved("confirmed calendar does not cover the horizon") from exc

    known_actions = [
        a
        for a in actions
        if a.security_id == security_id
        and a.knowledge_basis is KnowledgeBasis.PROSPECTIVE
        and a.available_at <= cutoff
    ]
    if any(
        a.ex_date is None
        and not (a.interpretation is ActionInterpretation.TERMINAL and a.effective_date)
        for a in known_actions
    ):
        raise OutcomeUnresolved("an action without an ex-date cannot be assigned to the window")
    applicable = [
        a
        for a in known_actions
        if entry_day < (a.ex_date or a.effective_date or date.min) <= exit_day
    ]
    relevant_coverage = [
        c
        for c in coverage
        if c.security_id == security_id
        and c.knowledge_basis is KnowledgeBasis.PROSPECTIVE
        and c.established_at <= cutoff
        and c.full_history
        and c.pagination_exhausted
        and c.provider_lower_bound is not None
        and c.provider_lower_bound <= entry_day
        and c.range_start <= c.provider_lower_bound
        # Alpaca scopes these rows by process_date, not ex_date. Prove the complete
        # process-date snapshot through the latest date that could be known at cutoff.
        and c.covered_through >= cutoff.astimezone(_ET).date() - timedelta(days=1)
    ]
    if not relevant_coverage:
        raise OutcomeUnresolved("no provider-proven full-history all-type action coverage")
    calendar_times = [
        calendar.coverage_time(entry_day + timedelta(days=offset))
        for offset in range((exit_day - entry_day).days + 1)
    ]
    calendar_times.extend(
        session.available_at
        for session_day, session in calendar.sessions.items()
        if entry_day <= session_day <= exit_day
    )

    terminal = delisting if delisting and delisting.available_at <= cutoff else None
    if terminal and terminal.status is DelistingStatus.DELISTED:
        terminal_actions = [
            a
            for a in applicable
            if a.interpretation is ActionInterpretation.TERMINAL
            and a.effective_date is not None
            and a.effective_date <= exit_day
        ]
        if len(terminal_actions) > 1:
            raise OutcomeUnresolved("ambiguous terminal corporate-action evidence")
        terminal_action = terminal_actions[0] if terminal_actions else None
        terminal_day = (
            terminal_action.effective_date
            if terminal_action is not None
            else terminal.last_trade_date
        )
        if terminal_day is not None and terminal_day <= exit_day:
            if terminal.terminal_return is not None:
                terminal_dependencies = [
                    reference.available_at,
                    terminal.available_at,
                    *calendar_times,
                    *(c.established_at for c in relevant_coverage),
                ]
                if terminal_action is not None:
                    terminal_dependencies.append(terminal_action.available_at)
                return ReturnResult(
                    terminal.terminal_return,
                    terminal_day,
                    max(terminal_dependencies),
                )
            if any(
                a.interpretation is ActionInterpretation.UNINTERPRETED
                and (a.ex_date or a.effective_date or date.max) <= terminal_day
                for a in applicable
            ):
                raise OutcomeUnresolved("window contains an uninterpreted corporate action")
            acquiree_actions = [
                a for a in applicable if a.ex_date is not None and a.ex_date <= terminal_day
            ]
            target_shares, target_cash, target_action_time = _action_adjustments(acquiree_actions)
            if terminal.terminal_return_source is not TerminalReturnSource.CONSIDERATION:
                raise OutcomeUnresolved("terminal delisting has no admissible terminal value")
            if terminal.cash_per_share is None:
                if terminal.acquirer_rate is None:
                    raise OutcomeUnresolved("consideration terms are incomplete")
            elif terminal.acquirer_rate is None:
                value = (
                    terminal.cash_per_share * target_shares + target_cash
                ) / reference.price - 1.0
                used = [reference.available_at, terminal.available_at, *calendar_times]
                used.extend(c.established_at for c in relevant_coverage)
                used.extend(a.available_at for a in acquiree_actions)
                if target_action_time:
                    used.append(target_action_time)
                if terminal_action is not None:
                    used.append(terminal_action.available_at)
                return ReturnResult(value, terminal_day, max(used))
            if terminal.acquirer_security_id is None or terminal.acquirer_rate is None:
                raise OutcomeUnresolved("acquirer identity or conversion ratio is missing")
            acquirer_id = terminal.acquirer_security_id
            acquirer_exit = _daily_close(acquirer_bars, acquirer_id, exit_day, cutoff)
            acquirer_known = [
                a
                for a in acquirer_actions
                if a.security_id == acquirer_id
                and a.knowledge_basis is KnowledgeBasis.PROSPECTIVE
                and a.available_at <= cutoff
                and a.ex_date is not None
                and terminal_day < a.ex_date <= exit_day
            ]
            if any(a.interpretation is ActionInterpretation.UNINTERPRETED for a in acquirer_known):
                raise OutcomeUnresolved("acquirer window contains an uninterpreted action")
            acq_coverage = [
                c
                for c in acquirer_coverage
                if c.security_id == acquirer_id
                and c.knowledge_basis is KnowledgeBasis.PROSPECTIVE
                and c.established_at <= cutoff
                and c.full_history
                and c.pagination_exhausted
                and c.provider_lower_bound is not None
                and c.provider_lower_bound <= terminal_day
                and c.range_start <= c.provider_lower_bound
                and c.covered_through >= cutoff.astimezone(_ET).date() - timedelta(days=1)
            ]
            if not acq_coverage:
                raise OutcomeUnresolved("no provider-proven acquirer action coverage")
            shares, acquirer_cash, acq_action_time = _action_adjustments(acquirer_known)
            acquired_cash = terminal.cash_per_share or 0.0
            value = (
                acquirer_exit.close * terminal.acquirer_rate * target_shares * shares
                + acquired_cash * target_shares
                + (target_cash + acquirer_cash * terminal.acquirer_rate * target_shares)
            ) / reference.price - 1.0
            used = [
                reference.available_at,
                terminal.available_at,
                acquirer_exit.available_at,
                *calendar_times,
                *(c.established_at for c in acq_coverage),
            ]
            if terminal_action is not None:
                used.append(terminal_action.available_at)
            if acq_action_time:
                used.append(acq_action_time)
            used.extend(a.available_at for a in acquiree_actions)
            if target_action_time:
                used.append(target_action_time)
            return ReturnResult(value, exit_day, max(used))

    if any(a.interpretation is ActionInterpretation.UNINTERPRETED for a in applicable):
        raise OutcomeUnresolved("window contains an uninterpreted corporate action")

    exit_bar = _daily_close(bars, security_id, exit_day, cutoff)
    split, cash, _ = _action_adjustments(applicable)
    value = (exit_bar.close * split + cash) / reference.price - 1.0
    if value < -1.0:
        raise OutcomeUnresolved("computed return violates limited liability")
    used_times = [
        reference.available_at,
        exit_bar.available_at,
        *calendar_times,
        *(a.available_at for a in applicable),
    ]
    if relevant_coverage:
        used_times.extend(c.established_at for c in relevant_coverage)
    if terminal:
        used_times.append(terminal.available_at)
    return ReturnResult(value, exit_day, max(used_times))


def weekly_period_return(
    *,
    security_id: int,
    entry_reference: ExecutionReference,
    endpoint_reference: BenchmarkPeriodReference,
    calendar: TradingCalendar,
    actions: Sequence[CorporateAction],
    coverage: Sequence[CorporateActionCoverage],
    delisting: Delisting | None,
    cutoff: datetime,
) -> ReturnResult:
    """Compute a corporate-action adjusted book return between immutable run references.

    This is the weekly benchmark period contract (§12.3), distinct from the D5 close outcome.
    It deliberately refuses terminal delisting windows until terminal consideration can be
    evaluated at the weekly reference boundary with the same P6.5 evidence rules.
    """
    if (
        entry_reference.status is not RefStatus.RESOLVED
        or endpoint_reference.status is not RefStatus.RESOLVED
        or entry_reference.price is None
        or endpoint_reference.price is None
        or entry_reference.session_date is None
        or endpoint_reference.session_date is None
        or entry_reference.ref_time is None
        or endpoint_reference.ref_time is None
    ):
        raise OutcomeUnresolved("weekly execution reference is unresolved")
    if (
        entry_reference.available_at > cutoff
        or endpoint_reference.available_at > cutoff
        or entry_reference.ref_time > cutoff
        or endpoint_reference.ref_time > cutoff
    ):
        raise OutcomeUnresolved("weekly execution reference was not knowable at cutoff")
    if (
        entry_reference.run_id != endpoint_reference.run_id
        or entry_reference.symbol_ref != endpoint_reference.symbol_ref
        or entry_reference.mode is not endpoint_reference.mode
        or endpoint_reference.period_start != entry_reference.session_date
    ):
        raise OutcomeUnresolved("weekly period references are not bound to the same run and symbol")
    entry_day = entry_reference.session_date
    exit_day = endpoint_reference.session_date
    if exit_day <= entry_day:
        raise OutcomeUnresolved("weekly benchmark endpoint does not follow entry")
    try:
        calendar_times = [
            calendar.coverage_time(entry_day + timedelta(days=offset))
            for offset in range((exit_day - entry_day).days + 1)
        ]
        if (
            calendar.session(entry_day).session_date != entry_day
            or calendar.session(exit_day).session_date != exit_day
        ):
            raise OutcomeUnresolved("weekly references do not align to confirmed sessions")
    except CalendarCoverageError as exc:
        raise OutcomeUnresolved("confirmed calendar does not cover weekly period") from exc
    calendar_times.extend(
        session.available_at
        for session_day, session in calendar.sessions.items()
        if entry_day <= session_day <= exit_day
    )

    known_actions = [
        action
        for action in actions
        if action.security_id == security_id
        and action.knowledge_basis is KnowledgeBasis.PROSPECTIVE
        and action.available_at <= cutoff
    ]
    if any(
        action.ex_date is None
        and not (action.interpretation is ActionInterpretation.TERMINAL and action.effective_date)
        for action in known_actions
    ):
        raise OutcomeUnresolved(
            "an action without an ex-date cannot be assigned to the weekly window"
        )
    applicable = [
        action
        for action in known_actions
        if entry_day < (action.ex_date or action.effective_date or date.min) <= exit_day
    ]
    if any(action.interpretation is ActionInterpretation.TERMINAL for action in applicable):
        raise OutcomeUnresolved("weekly window contains a terminal corporate action")
    if any(action.interpretation is ActionInterpretation.UNINTERPRETED for action in applicable):
        raise OutcomeUnresolved("weekly window contains an uninterpreted corporate action")
    if (
        delisting is not None
        and delisting.knowledge_basis is KnowledgeBasis.PROSPECTIVE
        and delisting.available_at <= cutoff
        and delisting.status is DelistingStatus.DELISTED
        and delisting.last_trade_date is not None
        and delisting.last_trade_date <= exit_day
    ):
        raise OutcomeUnresolved("weekly window contains a terminal delisting")
    relevant_coverage = [
        row
        for row in coverage
        if row.security_id == security_id
        and row.knowledge_basis is KnowledgeBasis.PROSPECTIVE
        and row.established_at <= cutoff
        and row.full_history
        and row.pagination_exhausted
        and row.provider_lower_bound is not None
        and row.provider_lower_bound <= entry_day
        and row.range_start <= row.provider_lower_bound
        and row.covered_through >= cutoff.astimezone(_ET).date() - timedelta(days=1)
    ]
    if not relevant_coverage:
        raise OutcomeUnresolved("no provider-proven full-history all-type action coverage")

    split, cash, action_time = _action_adjustments(applicable)
    value = (endpoint_reference.price * split + cash) / entry_reference.price - 1.0
    if value < -1.0:
        raise OutcomeUnresolved("computed weekly return violates limited liability")
    used_times = [
        entry_reference.available_at,
        endpoint_reference.available_at,
        *calendar_times,
        *(action.available_at for action in applicable),
        *(row.established_at for row in relevant_coverage),
    ]
    if action_time is not None:
        used_times.append(action_time)
    if delisting is not None and delisting.available_at <= cutoff:
        used_times.append(delisting.available_at)
    return ReturnResult(value, exit_day, max(used_times))
