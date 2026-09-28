from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection

from contracts.enums import (
    AgentName,
    DataSufficiency,
    FeedName,
    Horizon,
    RunMode,
    RunStatus,
    Stance,
)
from contracts.errors import ImmutableConflictError
from contracts.models import (
    AgentVerdict,
    AgentWeight,
    BenchmarkForecastBundle,
    BenchmarkReplayContext,
    CalibrationFit,
    CommitteeDecision,
    CommitteeDecisionRecord,
    DecisionCommitment,
    EvidenceRef,
    GateDecision,
    HorizonPool,
    PooledForecast,
    RunRecord,
    VerdictRecord,
)
from orchestration.benchmarks import _stored_gate_versions
from store import as_of, write
from tests.store import factories as f


def _context(
    run_id: UUID,
    feature_version: str = "fs_v1",
    gate_version: str = "gate_v1",
) -> BenchmarkReplayContext:
    token = "ENTITY_01"
    verdict = AgentVerdict(
        stance=Stance.BUY,
        p_outperform_5=0.55,
        p_outperform_21=0.6,
        p_outperform_63=0.62,
        key_evidence=(EvidenceRef(source=FeedName.PRICE_BARS, row_id="r1", note="evidence"),),
        risks=(),
        data_sufficiency=DataSufficiency.FULL,
        run_id=run_id,
        agent=AgentName.VALUE,
        entity_token=token,
        as_of=datetime(2026, 1, 5, tzinfo=UTC),
        prompt_version="v1-test",
        model_served="test/model",
        valid=True,
    )
    forecast = PooledForecast(
        entity_token=token,
        pools=tuple(
            HorizonPool(
                horizon=horizon,
                logit=0.1,
                lambda_t=0.0,
                dispersion=0.1,
                weights=(AgentWeight(agent=AgentName.VALUE, weight=1.0),),
            )
            for horizon in Horizon
        ),
    )
    return BenchmarkReplayContext(
        run_id=run_id,
        random_bundles=(
            BenchmarkForecastBundle(
                security_id=1,
                entity_token=token,
                agent_verdicts=(verdict,),
                pooled_forecast=forecast,
                bear_severity=None,
            ),
        ),
        calibration_fits=(
            CalibrationFit(
                horizon=Horizon.D21,
                alpha=0.0,
                beta=1.0,
                active=False,
                independent_periods=0,
                observations=0,
                base_rate=None,
            ),
        ),
        sizing_horizon=Horizon.D21,
        risk_config_snapshot={"max_position": 0.08},
        feature_set_versions=(feature_version,),
        gate_model_versions=(gate_version,),
        evaluation_parameters_sha256="a" * 64,
    )


def test_benchmark_replay_context_is_replayable_and_insert_only(db: Connection) -> None:
    run_id = uuid4()
    now = datetime(2026, 1, 5, tzinfo=UTC)
    write.upsert_run(
        db,
        RunRecord(
            run_id=run_id,
            mode=RunMode.BACKTEST,
            as_of=now,
            config_hash="b" * 64,
            status=RunStatus.COMMITTED,
            started_at=now,
            ended_at=now,
        ),
    )
    write.insert_commitment(
        db,
        DecisionCommitment(run_id=run_id, sha256="c" * 64, committed_at=now),
    )
    context = _context(run_id)
    f.security(db, "AAA", 1001)
    verdict_record = VerdictRecord(
        security_id=1,
        verdict=context.random_bundles[0].agent_verdicts[0],
        tokens_in=10,
        tokens_out=5,
        cost_usd=0.001,
        latency_ms=25,
    )
    write.insert_agent_verdicts(db, [verdict_record])
    decision_record = CommitteeDecisionRecord(
        decision=CommitteeDecision(
            run_id=run_id,
            security_id=1,
            entity_token="ENTITY_01",
            as_of=now,
            horizon_days=Horizon.D21,
            pooled_p=0.6,
            dispersion=0.1,
            agent_weights=(AgentWeight(agent=AgentName.VALUE, weight=1.0),),
            bear_severity=None,
            target_weight=0.05,
        )
    )
    write.insert_committee_decisions(db, [decision_record])
    gate_record = GateDecision(
        run_id=run_id,
        security_id=1,
        as_of=now,
        feature_set_version="fs_v1",
        gate_model_version="gate_v1",
        score=0.8,
        passed=True,
    )
    write.insert_gate_decisions(db, [gate_record])

    assert write.insert_benchmark_replay_context(db, context) == 1
    assert write.insert_benchmark_replay_context(db, context) == 0
    assert as_of.benchmark_replay_context(db, run_id) == context
    run, commitment, verdicts, decisions, portfolio, anchor, loaded_context, gate_rows = (
        as_of.benchmark_trial_material(db, run_id)
    )
    assert run is not None and run.run_id == run_id
    assert commitment is not None and commitment.sha256 == "c" * 64
    assert verdicts == (verdict_record,)
    assert decisions == (decision_record,)
    assert portfolio is None and anchor is None
    assert loaded_context == context
    assert gate_rows == (gate_record,)
    assert _stored_gate_versions(run, loaded_context, gate_rows) == (("gate_v1",), ("fs_v1",))
    with pytest.raises(ValueError, match="actual stored gate-decision evidence"):
        _stored_gate_versions(run, loaded_context, ())
    with pytest.raises(ValueError, match="gate versions do not match"):
        _stored_gate_versions(run, _context(run_id, gate_version="gate_v2"), gate_rows)

    with pytest.raises(ImmutableConflictError, match="conflicting benchmark replay context"):
        write.insert_benchmark_replay_context(db, _context(run_id, "fs_v2"))


def test_prior_trial_run_reader_is_config_scoped_and_cutoff_bounded(db: Connection) -> None:
    now = datetime(2026, 1, 5, tzinfo=UTC)
    current = RunRecord(
        run_id=uuid4(),
        mode=RunMode.BACKTEST,
        as_of=now,
        config_hash="d" * 64,
        status=RunStatus.COMMITTED,
        started_at=now,
        ended_at=now,
    )
    earlier = current.model_copy(
        update={
            "run_id": uuid4(),
            "as_of": now.replace(day=4),
            "started_at": now.replace(day=4),
            "ended_at": now.replace(day=4),
        }
    )
    later_commit = current.model_copy(
        update={"run_id": uuid4(), "as_of": now.replace(day=3), "started_at": now.replace(day=3)}
    )
    wrong_config = current.model_copy(
        update={"run_id": uuid4(), "as_of": now.replace(day=2), "config_hash": "e" * 64}
    )
    for run in (current, earlier, later_commit, wrong_config):
        write.upsert_run(db, run)
    for run, committed_at in (
        (earlier, now.replace(day=4)),
        (later_commit, now.replace(day=6)),
        (wrong_config, now.replace(day=2)),
    ):
        write.insert_commitment(
            db,
            DecisionCommitment(run_id=run.run_id, sha256="f" * 64, committed_at=committed_at),
        )

    assert as_of.prior_committed_trial_runs(db, current, cutoff=now) == (earlier,)
