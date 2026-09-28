from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest

from contracts.corporate_actions import (
    ALL_ACTION_TYPES,
    CorporateAction,
    CorporateActionCoverage,
    Delisting,
    actions_sha256,
    delisting_version,
    payload_version,
)
from contracts.data import PriceBar
from contracts.enums import (
    ActionInterpretation,
    CorporateActionType,
    DelistingReason,
    DelistingStatus,
    Horizon,
    KnowledgeBasis,
    PriceFeed,
    ReferenceSource,
    RefMode,
    RefStatus,
    Tape,
    TerminalReturnSource,
)
from contracts.market_data import (
    BenchmarkPeriodReference,
    CalendarCoverage,
    ExecutionReference,
    TradingSession,
    calendar_hash,
)
from evaluation.calendar_rules import TradingCalendar, build_calendar
from evaluation.returns import OutcomeUnresolved, forward_return, weekly_period_return


def inputs() -> tuple[date, TradingCalendar, ExecutionReference, list[PriceBar], datetime]:
    day = date(2024, 1, 1)
    sessions = [
        TradingSession(
            event_time=datetime.combine(day + timedelta(days=i), datetime.min.time(), UTC),
            available_at=datetime(2023, 12, 1, tzinfo=UTC),
            source_version="calendar-v1",
            session_date=day + timedelta(days=i),
            open_at=datetime.combine(day + timedelta(days=i), datetime.min.time(), UTC),
            close_at=datetime.combine(day + timedelta(days=i), datetime.min.time(), UTC)
            + timedelta(hours=6),
        )
        for i in range(7)
    ]
    calendar = build_calendar(
        sessions,
        [
            CalendarCoverage(
                source="test",
                range_start=day,
                range_end=day + timedelta(days=6),
                available_at=datetime(2024, 1, 9, tzinfo=UTC),
                session_count=len(sessions),
                sessions_sha256=calendar_hash(sessions),
                source_version="calendar-v1",
            )
        ],
    )
    ref = ExecutionReference(
        run_id=UUID("00000000-0000-0000-0000-000000000001"),
        symbol_ref="AAA",
        status=RefStatus.RESOLVED,
        price=100,
        source=ReferenceSource.SIP_LAST,
        ref_time=datetime(2023, 12, 29, tzinfo=UTC),
        session_date=day,
        mode=RefMode.BACKTEST,
        available_at=datetime(2023, 12, 29, tzinfo=UTC),
        source_version="reference-v1",
        trade_time=datetime(2023, 12, 29, tzinfo=UTC),
        trade_tape=Tape.A,
    )
    bars = [
        PriceBar(
            security_id=1,
            event_time=datetime.combine(day + timedelta(days=5), datetime.min.time(), UTC)
            + timedelta(hours=17),
            available_at=datetime(2024, 1, 8, tzinfo=UTC),
            source_version=PriceFeed.SIP.value,
            open=110,
            high=110,
            low=110,
            close=110,
            volume=1,
            feed=PriceFeed.SIP,
        )
    ]
    cutoff = datetime(2024, 2, 1, tzinfo=UTC)
    return day, calendar, ref, bars, cutoff


def coverage(start: date) -> CorporateActionCoverage:
    digest = actions_sha256(())
    return CorporateActionCoverage(
        security_id=1,
        range_start=start,
        range_end=start + timedelta(days=40),
        established_at=datetime(2024, 1, 31, 8, tzinfo=UTC),
        symbols=("AAA",),
        action_types=ALL_ACTION_TYPES,
        page_count=1,
        pagination_exhausted=True,
        full_history=True,
        provider_lower_bound=start,
        action_count=0,
        actions_sha256=digest,
        knowledge_basis=KnowledgeBasis.PROSPECTIVE,
        source_version="coverage-v1",
    )


def test_return_needs_provider_proven_full_history_action_coverage() -> None:
    _, calendar, ref, bars, cutoff = inputs()
    with pytest.raises(OutcomeUnresolved, match="full-history"):
        forward_return(
            security_id=1,
            horizon=Horizon.D5,
            reference=ref,
            bars=bars,
            calendar=calendar,
            actions=(),
            coverage=(),
            delisting=None,
            cutoff=cutoff,
        )


def test_return_uses_raw_exit_bar_when_action_scope_is_proven() -> None:
    day, calendar, ref, bars, cutoff = inputs()
    result = forward_return(
        security_id=1,
        horizon=Horizon.D5,
        reference=ref,
        bars=bars,
        calendar=calendar,
        actions=(),
        coverage=(coverage(day),),
        delisting=None,
        cutoff=cutoff,
    )
    assert result.value == pytest.approx(0.10)
    assert result.resolved_at == datetime(2024, 1, 31, 8, tzinfo=UTC)


def test_weekly_return_uses_same_run_period_endpoint_reference_and_cutoff() -> None:
    day, calendar, entry, _, cutoff = inputs()
    endpoint = BenchmarkPeriodReference(
        run_id=entry.run_id,
        symbol_ref=entry.symbol_ref,
        period_start=day,
        ref_time=datetime(2024, 1, 7, 10, tzinfo=UTC),
        mode=RefMode.BACKTEST,
        status=RefStatus.RESOLVED,
        price=111,
        source=ReferenceSource.SIP_LAST,
        trade_time=datetime(2024, 1, 7, 9, 50, tzinfo=UTC),
        trade_tape=Tape.A,
        session_date=day + timedelta(days=6),
        available_at=datetime(2024, 1, 8, tzinfo=UTC),
        source_version="benchmark-reference-v1",
    )
    result = weekly_period_return(
        security_id=1,
        entry_reference=entry,
        endpoint_reference=endpoint,
        calendar=calendar,
        actions=(),
        coverage=(coverage(day),),
        delisting=None,
        cutoff=cutoff,
    )
    assert result.value == pytest.approx(0.11)
    assert result.exit_session == endpoint.session_date

    with pytest.raises(OutcomeUnresolved, match="not knowable at cutoff"):
        weekly_period_return(
            security_id=1,
            entry_reference=entry,
            endpoint_reference=endpoint.model_copy(
                update={"available_at": cutoff + timedelta(seconds=1)}
            ),
            calendar=calendar,
            actions=(),
            coverage=(coverage(day),),
            delisting=None,
            cutoff=cutoff,
        )


def test_weekly_return_credits_cash_dividend_inside_the_period() -> None:
    day, calendar, entry, _, cutoff = inputs()
    endpoint = BenchmarkPeriodReference(
        run_id=entry.run_id,
        symbol_ref=entry.symbol_ref,
        period_start=day,
        ref_time=datetime(2024, 1, 7, 10, tzinfo=UTC),
        mode=RefMode.BACKTEST,
        status=RefStatus.RESOLVED,
        price=111,
        source=ReferenceSource.SIP_LAST,
        trade_time=datetime(2024, 1, 7, 9, 50, tzinfo=UTC),
        trade_tape=Tape.A,
        session_date=day + timedelta(days=6),
        available_at=datetime(2024, 1, 8, tzinfo=UTC),
        source_version="benchmark-reference-v1",
    )
    raw = {"cash_rate": 1.0}
    action = CorporateAction(
        provider_action_id="weekly-dividend",
        available_at=datetime(2024, 1, 5, tzinfo=UTC),
        source_version=payload_version(raw),
        security_id=1,
        subject_symbol="AAA",
        action_type=CorporateActionType.CASH_DIVIDEND,
        interpretation=ActionInterpretation.CASH_DIVIDEND,
        knowledge_basis=KnowledgeBasis.PROSPECTIVE,
        process_date=day + timedelta(days=2),
        ex_date=day + timedelta(days=3),
        cash_rate=1.0,
        raw_payload=raw,
    )
    complete_coverage = coverage(day).model_copy(
        update={
            "actions_sha256": actions_sha256(((action.provider_action_id, action.source_version),)),
            "action_count": 1,
        }
    )
    result = weekly_period_return(
        security_id=1,
        entry_reference=entry,
        endpoint_reference=endpoint,
        calendar=calendar,
        actions=(action,),
        coverage=(complete_coverage,),
        delisting=None,
        cutoff=cutoff,
    )
    assert result.value == pytest.approx(0.12)
    with pytest.raises(OutcomeUnresolved, match="full-history"):
        weekly_period_return(
            security_id=1,
            entry_reference=entry,
            endpoint_reference=endpoint,
            calendar=calendar,
            actions=(),
            coverage=(),
            delisting=None,
            cutoff=cutoff,
        )


def test_action_coverage_must_cover_process_dates_through_ticket_cutoff() -> None:
    day, calendar, ref, bars, cutoff = inputs()
    partial = coverage(day).model_copy(update={"range_end": date(2024, 1, 20)})
    with pytest.raises(OutcomeUnresolved, match="full-history"):
        forward_return(
            security_id=1,
            horizon=Horizon.D5,
            reference=ref,
            bars=bars,
            calendar=calendar,
            actions=(),
            coverage=(partial,),
            delisting=None,
            cutoff=cutoff,
        )


def test_action_coverage_query_start_cannot_exclude_its_claimed_lower_bound() -> None:
    day, calendar, ref, bars, cutoff = inputs()
    partial = coverage(day).model_copy(update={"range_start": day + timedelta(days=1)})
    with pytest.raises(OutcomeUnresolved, match="full-history"):
        forward_return(
            security_id=1,
            horizon=Horizon.D5,
            reference=ref,
            bars=bars,
            calendar=calendar,
            actions=(),
            coverage=(partial,),
            delisting=None,
            cutoff=cutoff,
        )


def delist(
    *, cash: float | None, acquirer_id: int | None = None, rate: float | None = None
) -> Delisting:
    fields = dict(
        security_id=1,
        status=DelistingStatus.DELISTED,
        reason=DelistingReason.STOCK_AND_CASH_MERGER
        if rate and cash
        else (DelistingReason.STOCK_MERGER if rate else DelistingReason.CASH_MERGER),
        last_trade_date=date(2024, 1, 3),
        terminal_return=None,
        terminal_return_source=TerminalReturnSource.CONSIDERATION,
        cash_per_share=cash,
        acquirer_security_id=acquirer_id,
        acquirer_symbol="BBB" if rate else None,
        acquirer_rate=rate,
        evidence=("stored terminal action",),
        knowledge_basis=KnowledgeBasis.PROSPECTIVE,
        derivation_version="test-v1",
        available_at=datetime(2024, 1, 4, tzinfo=UTC),
    )
    provisional = Delisting.model_construct(**cast(Any, fields), source_version="")
    fields["source_version"] = delisting_version(provisional)
    return Delisting(**cast(Any, fields))


def test_cash_delisting_value_is_flat_through_horizon() -> None:
    day, calendar, ref, bars, cutoff = inputs()
    result = forward_return(
        security_id=1,
        horizon=Horizon.D5,
        reference=ref,
        bars=bars,
        calendar=calendar,
        actions=(),
        coverage=(coverage(day),),
        delisting=delist(cash=120),
        cutoff=cutoff,
    )
    assert result.value == pytest.approx(0.20)
    assert result.exit_session == date(2024, 1, 3)
    assert result.resolved_at == datetime(2024, 1, 31, 8, tzinfo=UTC)


def test_stock_and_mixed_delisting_carry_acquirer_through_original_horizon() -> None:
    day, calendar, ref, _, cutoff = inputs()
    acquirer_bar = PriceBar(
        security_id=2,
        event_time=datetime(2024, 1, 6, 17, tzinfo=UTC),
        available_at=datetime(2024, 1, 8, tzinfo=UTC),
        source_version="sip",
        open=60,
        high=60,
        low=60,
        close=60,
        volume=1,
        feed=PriceFeed.SIP,
    )
    acquirer_coverage = coverage(day).model_copy(update={"security_id": 2, "symbols": ("BBB",)})
    common: dict[str, Any] = dict(
        security_id=1,
        horizon=Horizon.D5,
        reference=ref,
        bars=(),
        calendar=calendar,
        actions=(),
        coverage=(coverage(day),),
        cutoff=cutoff,
        acquirer_bars=(acquirer_bar,),
        acquirer_actions=(),
        acquirer_coverage=(acquirer_coverage,),
    )
    stock = forward_return(**common, delisting=delist(cash=None, acquirer_id=2, rate=2))
    mixed = forward_return(**common, delisting=delist(cash=10, acquirer_id=2, rate=1.5))
    assert stock.value == pytest.approx(0.20)
    assert mixed.value == pytest.approx(0.00)
    assert stock.exit_session == date(2024, 1, 6)
    assert mixed.resolved_at == datetime(2024, 1, 31, 8, tzinfo=UTC)


def test_acquirer_split_and_dividend_apply_after_terminal_event() -> None:
    day, calendar, ref, _, cutoff = inputs()
    bar = PriceBar(
        security_id=2,
        event_time=datetime(2024, 1, 6, 17, tzinfo=UTC),
        available_at=datetime(2024, 1, 8, tzinfo=UTC),
        source_version="sip",
        open=30,
        high=30,
        low=30,
        close=30,
        volume=1,
        feed=PriceFeed.SIP,
    )
    payload = {"old_rate": 1, "new_rate": 2}
    split = CorporateAction(
        provider_action_id="split-1",
        available_at=datetime(2024, 1, 8, tzinfo=UTC),
        source_version=payload_version(payload),
        security_id=2,
        subject_symbol="BBB",
        action_type=CorporateActionType.FORWARD_SPLIT,
        interpretation=ActionInterpretation.SPLIT_FACTOR,
        knowledge_basis=KnowledgeBasis.PROSPECTIVE,
        process_date=date(2024, 1, 4),
        ex_date=date(2024, 1, 5),
        old_rate=1,
        new_rate=2,
        raw_payload=payload,
    )
    acquirer_coverage = coverage(day).model_copy(update={"security_id": 2, "symbols": ("BBB",)})
    result = forward_return(
        security_id=1,
        horizon=Horizon.D5,
        reference=ref,
        bars=(),
        calendar=calendar,
        actions=(),
        coverage=(coverage(day),),
        delisting=delist(cash=None, acquirer_id=2, rate=2),
        cutoff=cutoff,
        acquirer_bars=(bar,),
        acquirer_actions=(split,),
        acquirer_coverage=(acquirer_coverage,),
    )
    assert result.value == pytest.approx(0.20)
    assert result.resolved_at == datetime(2024, 1, 31, 8, tzinfo=UTC)


def test_known_consideration_without_acquirer_id_fails_closed() -> None:
    day, calendar, ref, bars, cutoff = inputs()
    with pytest.raises(OutcomeUnresolved, match="acquirer identity"):
        forward_return(
            security_id=1,
            horizon=Horizon.D5,
            reference=ref,
            bars=bars,
            calendar=calendar,
            actions=(),
            coverage=(coverage(day),),
            delisting=delist(cash=None, acquirer_id=None, rate=2),
            cutoff=cutoff,
        )


def test_stock_delisting_requires_acquirer_action_coverage_and_cutoff_bars() -> None:
    day, calendar, ref, _, cutoff = inputs()
    bar = PriceBar(
        security_id=2,
        event_time=datetime(2024, 1, 6, 17, tzinfo=UTC),
        available_at=cutoff + timedelta(seconds=1),
        source_version="sip",
        open=60,
        high=60,
        low=60,
        close=60,
        volume=1,
        feed=PriceFeed.SIP,
    )
    args: dict[str, Any] = dict(
        security_id=1,
        horizon=Horizon.D5,
        reference=ref,
        bars=(),
        calendar=calendar,
        actions=(),
        coverage=(coverage(day),),
        delisting=delist(cash=None, acquirer_id=2, rate=2),
        cutoff=cutoff,
        acquirer_bars=(bar,),
        acquirer_actions=(),
        acquirer_coverage=(),
    )
    with pytest.raises(OutcomeUnresolved, match="missing point-in-time bar"):
        forward_return(**args)
    args["acquirer_bars"] = (bar.model_copy(update={"available_at": cutoff}),)
    with pytest.raises(OutcomeUnresolved, match="acquirer action coverage"):
        forward_return(**args)
