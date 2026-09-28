"""Required-service tests for the production HALTED benchmark evidence assembler."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Connection

from contracts.corporate_actions import ALL_ACTION_TYPES, CorporateActionCoverage, actions_sha256
from contracts.data import SicObservation
from contracts.enums import (
    ActionCoverageFilter,
    KillTrigger,
    KnowledgeBasis,
    ReferenceSource,
    RefMode,
    RefStatus,
    RunMode,
    RunStatus,
    Tape,
)
from contracts.market_data import (
    ExecutionReference,
    HaltReference,
    HaltSymbolSet,
    TBillObservation,
    TBillVintageCoverage,
    symbols_sha256,
)
from contracts.models import KillSwitchEvent, RunRecord
from evaluation.benchmarks import BenchmarkEvidenceError
from evaluation.calendar_rules import TradingCalendar
from evaluation.scorable import Completeness, ScoringTicket
from orchestration.benchmark_scoring import _halt_adjustment_inputs
from store import as_of, write
from tests.market_data_support import coverage_for, sessions_2024
from tests.store import factories as f

START = date(2024, 3, 4)
END = date(2024, 3, 11)
AS_OF = datetime(2024, 3, 1, 21, 0, tzinfo=UTC)
CUTOFF = datetime(2024, 4, 1, 12, 0, tzinfo=UTC)
TAU = datetime(2024, 3, 6, 15, 0, tzinfo=UTC)
MARKED = TAU + timedelta(minutes=1)


def _seed_halt_evidence(
    db: Connection, *, missing_mark: str | None = None, missing_rates: bool = False
) -> tuple[ScoringTicket, TradingCalendar, dict[int, ExecutionReference], set[int]]:
    run_id = uuid4()
    run = RunRecord(
        run_id=run_id,
        mode=RunMode.LIVE,
        as_of=AS_OF,
        config_hash="c" * 64,
        status=RunStatus.PARTIAL,
        started_at=AS_OF,
        ended_at=MARKED,
        status_reason="kill_switch:daily_loss",
    )
    write.upsert_run(db, run)
    write.insert_kill_switch_events(
        db,
        [
            KillSwitchEvent(run_id=run_id, triggered_at=TAU, trigger=KillTrigger.DAILY_LOSS),
            KillSwitchEvent(
                run_id=run_id,
                triggered_at=MARKED + timedelta(hours=1),
                trigger=KillTrigger.MANUAL,
                flattened=True,
            ),
        ],
    )
    equity_id = f.security(db, "AAA", 1001)
    xle_id = write.ensure_reference_instrument(db, ticker="XLE", name="Energy ETF")
    spy_id = write.ensure_reference_instrument(db, ticker="SPY", name="S&P 500 ETF")
    ids = {equity_id, xle_id, spy_id}
    symbols = {equity_id: "AAA", xle_id: "XLE", spy_id: "SPY"}
    write.insert_sic_observations(
        db,
        [
            SicObservation(
                security_id=equity_id,
                sic=1311,
                event_time=AS_OF,
                available_at=AS_OF,
                source_version="sic-test-v1",
            )
        ],
    )
    sessions = sessions_2024(date(2024, 2, 1), date(2024, 3, 20))
    calendar_coverage = coverage_for(sessions, date(2024, 2, 1), date(2024, 3, 20))
    write.insert_calendar_range(db, sessions, calendar_coverage)
    calendar = as_of.trading_calendar(db, CUTOFF)
    ticket = ScoringTicket(
        run_id=run_id,
        mode=RunMode.LIVE,
        completeness=Completeness.HALTED,
        commitment_sha256="a" * 64,
        git_commit="abcdef1234567",
        bitcoin_height=1,
        bitcoin_block_time=AS_OF,
        run_as_of=AS_OF,
        requested_at=CUTOFF,
    )
    entries = {
        sid: ExecutionReference(
            run_id=run_id,
            symbol_ref=symbol,
            status=RefStatus.RESOLVED,
            price=100.0,
            source=ReferenceSource.SIP_LAST,
            trade_time=datetime(2024, 3, 4, 15, 0, tzinfo=UTC),
            trade_tape=Tape.A,
            trade_conditions=("@",),
            trade_id=f"entry-{sid}",
            ref_time=datetime(2024, 3, 4, 15, 0, tzinfo=UTC),
            mode=RefMode.BACKTEST,
            session_date=START,
            available_at=datetime(2024, 3, 4, 15, 0, tzinfo=UTC),
            source_version="entry-test-v1",
        )
        for sid, symbol in symbols.items()
    }
    halt_symbols = tuple(sorted(symbols.values()))
    symbol_set = HaltSymbolSet(
        run_id=run_id,
        trigger=KillTrigger.DAILY_LOSS,
        symbols=halt_symbols,
        symbol_count=len(halt_symbols),
        symbols_sha256=symbols_sha256(halt_symbols),
        source="fixture-canonical-run-symbols",
        source_version="halt-set-test-v1",
        resolved_at=CUTOFF,
    )
    write.insert_halt_symbol_set(db, symbol_set)
    marks = [
        HaltReference(
            run_id=run_id,
            trigger=KillTrigger.DAILY_LOSS,
            symbol_ref=symbol,
            status=RefStatus.RESOLVED,
            price=101.0,
            source=ReferenceSource.SIP_LAST,
            trade_time=MARKED,
            trade_tape=Tape.A,
            trade_conditions=("@",),
            trade_id=f"halt-{sid}",
            observed_at=MARKED,
            lag_seconds=60.0,
            symbol_set_source=symbol_set.source,
            symbol_set_version=symbol_set.source_version,
            available_at=MARKED,
            source_version="halt-mark-test-v1",
        )
        for sid, symbol in symbols.items()
        if symbol != missing_mark
    ]
    write.insert_halt_references(db, marks)
    action_coverages = [
        CorporateActionCoverage(
            security_id=sid,
            date_filter=ActionCoverageFilter.PROCESS_DATE,
            range_start=date(1900, 1, 1),
            range_end=CUTOFF.date(),
            established_at=CUTOFF,
            symbols=(symbol,),
            action_types=ALL_ACTION_TYPES,
            page_count=1,
            pagination_exhausted=True,
            full_history=True,
            provider_lower_bound=date(1900, 1, 1),
            action_count=0,
            actions_sha256=actions_sha256(()),
            knowledge_basis=KnowledgeBasis.PROSPECTIVE,
            source_version=f"coverage-{symbol}-v1",
        )
        for sid, symbol in symbols.items()
    ]
    write.record_action_query(db, coverages=action_coverages, observed=())
    vintage_coverage = TBillVintageCoverage(
        series="DGS3MO",
        earliest_vintage=date(2024, 1, 1),
        latest_vintage=date(2024, 3, 1),
        vintage_count=1,
        vintage_dates_sha256="b" * 64,
        established_at=CUTOFF,
        source_version="tbill-coverage-test-v1",
    )
    write.insert_tbill_vintage_coverage(db, vintage_coverage)
    if not missing_rates:
        write.insert_tbill_rates(
            db,
            [
                TBillObservation(
                    series="DGS3MO",
                    observation_date=date(2024, 2, 29),
                    yield_pct=5.0,
                    vintage_date=date(2024, 3, 1),
                    source_version="tbill-rate-test-v1",
                )
            ],
        )
    return ticket, calendar, entries, ids


def test_database_halt_assembler_uses_first_trigger_marks_and_tbill_continuation(
    db: Connection,
) -> None:
    ticket, calendar, entries, ids = _seed_halt_evidence(db)
    event = as_of.first_kill_switch_event(db, ticket.run_id)
    assert event is not None and event.trigger is KillTrigger.DAILY_LOSS
    rows = as_of.tbill_rates(db, "DGS3MO", END)
    coverage = as_of.tbill_vintage_coverage(db, "DGS3MO", ticket.requested_at)
    assembled = _halt_adjustment_inputs(
        db,
        ticket,
        calendar=calendar,
        week_start=START,
        endpoint_day=END,
        entry_refs=entries,
        return_ids=ids,
        rates=rows,
        tbill_coverage=coverage,
    )
    assert set(assembled["pre_halt_returns"]) == ids
    assert set(assembled["post_halt_tbill_returns"]) == ids
    assert assembled["halt_tau"] == TAU
    assert assembled["halt_trigger"] == "daily_loss"
    assert all(assembled["post_halt_tbill_steps"].values())


@pytest.mark.parametrize("missing", ["AAA", "no_tbill_rates"])
def test_database_halt_assembler_fails_only_its_adjusted_evidence_unit(
    db: Connection, missing: str
) -> None:
    ticket, calendar, entries, ids = _seed_halt_evidence(
        db,
        missing_mark=missing if missing == "AAA" else None,
        missing_rates=missing == "no_tbill_rates",
    )
    with pytest.raises(BenchmarkEvidenceError):
        _halt_adjustment_inputs(
            db,
            ticket,
            calendar=calendar,
            week_start=START,
            endpoint_day=END,
            entry_refs=entries,
            return_ids=ids,
            rates=as_of.tbill_rates(db, "DGS3MO", END),
            tbill_coverage=as_of.tbill_vintage_coverage(db, "DGS3MO", ticket.requested_at),
        )
