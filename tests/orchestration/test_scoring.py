from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, text

from config.loader import SectorEntry, SectorsConfig
from contracts.corporate_actions import ALL_ACTION_TYPES, CorporateActionCoverage, actions_sha256
from contracts.data import PriceBar, SicObservation
from contracts.enums import (
    ActionCoverageFilter,
    AgentName,
    Horizon,
    KnowledgeBasis,
    PriceFeed,
    ReferenceSource,
    RefMode,
    RefStatus,
    RunStatus,
    Tape,
)
from contracts.market_data import ExecutionReference
from contracts.models import CommitmentAnchor, ResolvedForecastOutcome
from evaluation.calendar_rules import TradingCalendar
from evaluation.returns import OutcomeUnresolved, ReturnResult
from orchestration import scoring
from orchestration.loader import stacker_history
from orchestration.sink import DecisionSink
from store import as_of, write
from tests.evaluation.test_scorable import Git
from tests.market_data_support import FETCHED, coverage_for, sessions_2024
from tests.orchestration.anchoring_support import GIT_COMMIT, BlockHeaders, confirmed_proof
from tests.orchestration.test_sink import AS_OF
from tests.orchestration.test_store_anchoring import committed, halt
from tests.store import factories as f

EASTERN = ZoneInfo("America/New_York")
ENTRY = date(2024, 3, 4)
TICKET_CUTOFF = datetime(2026, 12, 31, 23, 0, tzinfo=UTC)
SECURITY_COVERAGE_AT = datetime(2026, 12, 31, 18, 0, tzinfo=UTC)
ETF_COVERAGE_AT = datetime(2026, 12, 31, 20, 0, tzinfo=UTC)
BLOCK_TIME = int((AS_OF + timedelta(hours=2)).timestamp())


@pytest.fixture
def scoring_engine(pg_engine: Engine) -> Iterator[Engine]:
    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE runs, securities RESTART IDENTITY CASCADE"))
    yield pg_engine
    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE runs, securities RESTART IDENTITY CASCADE"))


def _confirmed_sector_config() -> SectorsConfig:
    return SectorsConfig(
        confirmed=True,
        sectors=(
            SectorEntry(sector="energy", etf="XLE", sic_ranges=((1200, 1399),)),
            SectorEntry(sector="technology", etf="XLK", sic_ranges=((3500, 3599),)),
        ),
    )


def _anchor(sink: DecisionSink, run_id: Any) -> tuple[bytes, BlockHeaders]:
    material = sink.load_commitment_material(run_id)
    assert material is not None
    proof, root = confirmed_proof(material.stored_sha256)
    assert sink.mark_anchored(
        CommitmentAnchor(
            run_id=run_id,
            sha256=material.stored_sha256,
            ots_proof=proof,
            git_commit=GIT_COMMIT,
            anchored_at=AS_OF + timedelta(hours=1),
        )
    )
    sink.record_anchor(
        CommitmentAnchor(
            run_id=run_id,
            sha256=material.stored_sha256,
            ots_proof=proof,
            git_commit=GIT_COMMIT,
            anchored_at=AS_OF + timedelta(hours=1),
            verified_at=AS_OF + timedelta(hours=3),
        )
    )
    return root, BlockHeaders(root, BLOCK_TIME)


def _seed_market(
    engine: Engine,
    run_id: Any,
    security_id: int,
    *,
    include_etf_reference: bool = True,
    etf_reference_available_at: datetime = AS_OF,
    sic_available_at: datetime | None = None,
) -> tuple[int, tuple[date, date, date]]:
    with engine.begin() as conn:
        etf_id = write.ensure_reference_instrument(conn, ticker="XLE", name="Energy ETF")
        write.ensure_reference_instrument(conn, ticker="XLK", name="Technology ETF")
        write.insert_sic_observations(
            conn,
            [
                SicObservation(
                    security_id=security_id,
                    sic=1311,
                    event_time=sic_available_at or AS_OF - timedelta(hours=2),
                    available_at=sic_available_at or AS_OF - timedelta(hours=2),
                    source_version="sic-1311",
                )
            ],
        )
        sessions = sessions_2024(ENTRY, date(2024, 6, 30), available_at=FETCHED)
        write.insert_calendar_range(
            conn, sessions, coverage_for(sessions, ENTRY, date(2024, 6, 30))
        )
        calendar = as_of.trading_calendar(conn, TICKET_CUTOFF)
        exits = cast(
            tuple[date, date, date],
            tuple(calendar.horizon_session(ENTRY, int(h)) for h in Horizon),
        )
        ref_session = calendar.session(ENTRY)
        ref_time = ref_session.open_at + timedelta(minutes=30)
        refs = [
            ExecutionReference(
                run_id=run_id,
                symbol_ref="AAA",
                status=RefStatus.RESOLVED,
                price=100.0,
                source=ReferenceSource.SIP_LAST,
                trade_time=ref_time,
                trade_tape=Tape.A,
                ref_time=ref_time,
                mode=RefMode.BACKTEST,
                session_date=ENTRY,
                available_at=AS_OF,
                source_version="scoring-test-v1",
            )
        ]
        if include_etf_reference:
            refs.append(
                ExecutionReference(
                    run_id=run_id,
                    symbol_ref="XLE",
                    status=RefStatus.RESOLVED,
                    price=50.0,
                    source=ReferenceSource.SIP_LAST,
                    trade_time=ref_time,
                    trade_tape=Tape.A,
                    ref_time=ref_time,
                    mode=RefMode.BACKTEST,
                    session_date=ENTRY,
                    available_at=etf_reference_available_at,
                    source_version="scoring-test-v1",
                )
            )
        write.insert_execution_references(conn, refs)
        bars: list[PriceBar] = []
        for instrument_id, scale in ((security_id, 1.0), (etf_id, 0.5)):
            for n, exit_day in enumerate(exits, start=1):
                session = calendar.session(exit_day)
                close = 100.0 * scale + n
                bars.append(
                    PriceBar(
                        security_id=instrument_id,
                        event_time=session.close_at,
                        available_at=session.close_at + timedelta(minutes=15),
                        source_version=PriceFeed.SIP.value,
                        open=close,
                        high=close,
                        low=close,
                        close=close,
                        volume=1000,
                        feed=PriceFeed.SIP,
                    )
                )
        write.insert_price_bars(conn, bars)
        coverages = []
        for instrument_id, symbol, established_at in (
            (security_id, "AAA", ETF_COVERAGE_AT),
            (etf_id, "XLE", ETF_COVERAGE_AT),
        ):
            coverages.append(
                CorporateActionCoverage(
                    security_id=instrument_id,
                    date_filter=ActionCoverageFilter.PROCESS_DATE,
                    range_start=ENTRY,
                    range_end=TICKET_CUTOFF.astimezone(EASTERN).date(),
                    established_at=established_at,
                    symbols=(symbol,),
                    action_types=ALL_ACTION_TYPES,
                    page_count=1,
                    pagination_exhausted=True,
                    full_history=False,
                    action_count=0,
                    actions_sha256=actions_sha256(()),
                    knowledge_basis=KnowledgeBasis.PROSPECTIVE,
                    source_version=f"coverage-{symbol}",
                )
            )
        write.record_action_query(conn, coverages=coverages, observed=())
    return etf_id, exits


def test_stacker_history_joins_only_matching_resolved_outcomes() -> None:
    run_id = uuid4()
    observation = ResolvedForecastOutcome(
        run_id=run_id,
        security_id=1,
        agent=AgentName.VALUE,
        horizon=Horizon.D5,
        forecast=0.7,
        outperformed=True,
        committed_at=datetime(2024, 1, 2, tzinfo=UTC),
        resolved_at=datetime(2024, 1, 9, tzinfo=UTC),
    )
    history = stacker_history(Horizon.D5, [observation])
    assert history[AgentName.VALUE].forecasts == (0.7,)
    assert history[AgentName.VALUE].outcomes == (1,)
    assert history[AgentName.VALUE].independent_periods == 1
    assert stacker_history(Horizon.D21, [observation]) == {}


def _committed_run(
    engine: Engine, *, anchor: bool = True
) -> tuple[DecisionSink, Any, int, BlockHeaders | None]:
    sink = DecisionSink(engine)
    with engine.begin() as conn:
        security_id = f.security(conn, "AAA", 1001)
    run_id = committed(sink, security_id)
    headers = None
    if anchor:
        _, headers = _anchor(sink, run_id)
    return sink, run_id, security_id, headers


def _score(
    sink: DecisionSink,
    engine: Engine,
    run_id: Any,
    headers: BlockHeaders | None,
    *,
    sectors: SectorsConfig | None = None,
) -> tuple[Any, ...]:
    from evaluation.scorable import ScoringRequest

    return scoring.score_run(
        ScoringRequest(run_id),
        engine=engine,
        headers=cast(Any, headers),
        git=Git(),
        clock=lambda: TICKET_CUTOFF,
        sectors=sectors or _confirmed_sector_config(),
    )


def _run_status(sink: DecisionSink, run_id: Any) -> RunStatus:
    run = sink.load_run(run_id)
    assert run is not None
    return run.status


def test_score_run_builds_paired_complete_outcomes_only_after_admission(
    scoring_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink, run_id, security_id, headers = _committed_run(scoring_engine)
    assert headers is not None
    etf_id, exits = _seed_market(scoring_engine, run_id, security_id)
    # This unrelated security is deliberately outside the committed decision scope.
    with scoring_engine.begin() as conn:
        f.security(conn, "OUTSIDE", 1002)

    events: list[str] = []
    original_load = scoring.ScoringReader.load

    def admitted(reader: scoring.ScoringReader, requested_run_id: Any) -> Any:
        events.append("admission")
        return original_load(reader, requested_run_id)

    monkeypatch.setattr(scoring.ScoringReader, "load", admitted)
    for name in (
        "committed_security_scope",
        "execution_references",
        "reference_instruments",
        "symbol_history",
        "trading_calendar",
        "sic_history",
        "delisting",
        "prices_between",
        "corporate_actions",
        "action_coverage",
    ):
        original = getattr(scoring.as_of, name)

        def tracked(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any) -> Any:
            events.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(scoring.as_of, name, tracked)

    calls: list[dict[str, object]] = []

    def fake_return(**kwargs: Any) -> ReturnResult:
        calls.append(kwargs)
        ref = kwargs["reference"]
        horizon = kwargs["horizon"]
        calendar = kwargs["calendar"]
        coverage = kwargs["coverage"]
        assert coverage and coverage[0].full_history is False
        value = 0.1 if kwargs["security_id"] == security_id else 0.02
        return ReturnResult(
            value=value,
            exit_session=calendar.horizon_session(ref.session_date, int(horizon)),
            resolved_at=coverage[0].established_at,
        )

    monkeypatch.setattr(scoring, "forward_return", fake_return)
    outcomes = _score(sink, scoring_engine, run_id, headers)

    assert events[0] == "admission"
    assert set(events[1:]) >= {
        "committed_security_scope",
        "execution_references",
        "prices_between",
        "corporate_actions",
        "action_coverage",
    }
    assert len(outcomes) == 3
    assert [row.horizon for row in outcomes] == [Horizon.D5, Horizon.D21, Horizon.D63]
    assert [row.sector_fwd_return for row in outcomes] == [0.02, 0.02, 0.02]
    assert {row.resolved_at for row in outcomes} == {ETF_COVERAGE_AT}
    assert len(calls) == 6
    for horizon in Horizon:
        pair = [call for call in calls if call["horizon"] is horizon]
        assert {call["security_id"] for call in pair} == {security_id, etf_id}
        assert all(call["cutoff"] == TICKET_CUTOFF for call in pair)
        assert pair[0]["calendar"] is pair[1]["calendar"]
        assert {cast(ExecutionReference, call["reference"]).session_date for call in pair} == {
            ENTRY
        }
    assert set(exits) == {
        cast(TradingCalendar, call["calendar"]).horizon_session(
            ENTRY, int(cast(Horizon, call["horizon"]))
        )
        for call in calls
    }
    scored = sink.load_run(run_id)
    assert scored is not None and scored.status is RunStatus.SCORED
    with scoring_engine.connect() as conn:
        assert as_of.benchmark_results(conn, run_id) == []


def test_score_run_refusal_causes_zero_outcome_reads(
    scoring_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, run_id, _, _ = _committed_run(scoring_engine, anchor=False)
    outcome_reads: list[str] = []
    for name in (
        "committed_security_scope",
        "execution_references",
        "reference_instruments",
        "symbol_history",
        "trading_calendar",
        "sic_history",
        "delisting",
        "prices_between",
        "corporate_actions",
        "action_coverage",
    ):
        monkeypatch.setattr(
            scoring.as_of,
            name,
            lambda *args, _name=name, **kwargs: outcome_reads.append(_name),
        )
    from evaluation.scorable import ScoringRefusedError

    with pytest.raises(ScoringRefusedError):
        _score(DecisionSink(scoring_engine), scoring_engine, run_id, None)
    assert outcome_reads == []


def test_complete_scoring_writes_nothing_when_one_sector_horizon_is_unresolved(
    scoring_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink, run_id, security_id, headers = _committed_run(scoring_engine)
    assert headers is not None
    etf_id, _ = _seed_market(scoring_engine, run_id, security_id)

    def one_missing_pair_leg(**kwargs: Any) -> ReturnResult:
        if kwargs["security_id"] == etf_id and kwargs["horizon"] is Horizon.D21:
            raise OutcomeUnresolved("sector ETF D21 close is missing")
        ref = kwargs["reference"]
        horizon = kwargs["horizon"]
        coverage = kwargs["coverage"]
        return ReturnResult(
            value=0.02,
            exit_session=kwargs["calendar"].horizon_session(ref.session_date, int(horizon)),
            resolved_at=coverage[0].established_at,
        )

    monkeypatch.setattr(scoring, "forward_return", one_missing_pair_leg)
    assert _score(sink, scoring_engine, run_id, headers) == ()
    with scoring_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM outcomes WHERE run_id=:run"), {"run": run_id}
            ).scalar_one()
            == 0
        )
    assert _run_status(sink, run_id) is RunStatus.ANCHORED


def test_real_return_evaluator_keeps_unproven_no_action_coverage_unresolved(
    scoring_engine: Engine,
) -> None:
    sink, run_id, security_id, headers = _committed_run(scoring_engine)
    assert headers is not None
    _seed_market(scoring_engine, run_id, security_id)

    # These complete-query rows are deliberately not full-history evidence. The production return
    # evaluator therefore leaves outcomes absent, preserving the separate provider-scope blocker.
    outcomes = _score(sink, scoring_engine, run_id, headers)
    assert outcomes == ()
    assert _run_status(sink, run_id) is RunStatus.ANCHORED


@pytest.mark.parametrize(
    "missing", ["sic_mapping", "late_sic", "etf_reference", "late_etf_reference"]
)
def test_score_run_fails_closed_on_missing_or_late_sector_mapping_evidence(
    scoring_engine: Engine,
    missing: str,
) -> None:
    sink, run_id, security_id, headers = _committed_run(scoring_engine)
    assert headers is not None
    etf_id, _ = _seed_market(
        scoring_engine,
        run_id,
        security_id,
        include_etf_reference=missing != "etf_reference",
        etf_reference_available_at=(
            TICKET_CUTOFF + timedelta(seconds=1) if missing == "late_etf_reference" else AS_OF
        ),
        sic_available_at=AS_OF + timedelta(hours=1) if missing == "late_sic" else None,
    )
    if missing == "sic_mapping":
        config = SectorsConfig(
            confirmed=True,
            sectors=(SectorEntry(sector="technology", etf="XLK", sic_ranges=((3500, 3599),)),),
        )
    else:
        config = _confirmed_sector_config()
    outcomes = _score(sink, scoring_engine, run_id, headers, sectors=config)
    assert outcomes == ()
    assert _run_status(sink, run_id) is RunStatus.ANCHORED
    assert etf_id > 0


def test_halted_scoring_keeps_independently_resolved_pairs_and_stays_partial(
    scoring_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink, run_id, security_id, headers = _committed_run(scoring_engine)
    assert headers is not None
    etf_id, _ = _seed_market(scoring_engine, run_id, security_id)
    assert sink.record_halt(halt(run_id))

    def partial_return(**kwargs: Any) -> ReturnResult:
        if kwargs["security_id"] == etf_id and kwargs["horizon"] is Horizon.D21:
            raise OutcomeUnresolved("sector ETF horizon bar is missing")
        ref = kwargs["reference"]
        horizon = kwargs["horizon"]
        coverage = kwargs["coverage"]
        return ReturnResult(
            value=0.03 if kwargs["security_id"] == etf_id else 0.08,
            exit_session=kwargs["calendar"].horizon_session(ref.session_date, int(horizon)),
            resolved_at=coverage[0].established_at,
        )

    monkeypatch.setattr(scoring, "forward_return", partial_return)
    outcomes = _score(sink, scoring_engine, run_id, headers)
    assert {row.horizon for row in outcomes} == {Horizon.D5, Horizon.D63}
    with scoring_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT horizon, completeness FROM outcomes WHERE run_id=:run ORDER BY horizon"),
            {"run": run_id},
        ).all()
    assert stored == [(5, "HALTED"), (63, "HALTED")]
    assert _run_status(sink, run_id) is RunStatus.PARTIAL
