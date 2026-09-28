from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from contracts.benchmarks import (
    BenchmarkResult,
    RandomCommitteeSummary,
    assert_benchmark_variant_completeness,
)
from contracts.enums import (
    Benchmark,
    BenchmarkVariant,
    KillTrigger,
    OutcomeCompleteness,
    RunMode,
    RunStatus,
)
from contracts.models import CommitmentAnchor, DecisionCommitment, KillSwitchEvent, RunRecord
from evaluation.benchmarks import evaluate_benchmark_week
from store import as_of, write
from store.write import insert_benchmark_results
from tests.evaluation.test_benchmarks import _week_inputs


def _result(
    benchmark: Benchmark, variant: BenchmarkVariant, *, run_id: UUID | None = None
) -> BenchmarkResult:
    import hashlib
    import json

    replay = {"input": "immutable", "trial_identity_sha256": "e" * 64}
    provenance = hashlib.sha256(
        json.dumps(replay, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    input_digest = hashlib.sha256(
        json.dumps(replay, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    random_control = benchmark is Benchmark.RANDOM_COMMITTEE
    return BenchmarkResult(
        run_id=run_id or uuid4(),
        week_start=date(2026, 1, 5),
        benchmark=benchmark,
        variant=variant,
        gross_return=0.02,
        cost_return=0.001,
        net_return=0.019,
        turnover=0.2,
        outcome_cutoff=datetime(2026, 1, 12, tzinfo=UTC),
        commitment_sha256="a" * 64,
        trial_identity_sha256="e" * 64,
        input_sha256=input_digest,
        provenance_sha256=provenance,
        replay_inputs=replay,
        k=1000 if random_control else None,
        seed_sha256=hashlib.sha256(bytes.fromhex("a" * 64) + b"|random-committee|1000").hexdigest()
        if random_control
        else None,
        completeness=OutcomeCompleteness.COMPLETE,
        random_summary=(
            RandomCommitteeSummary(
                mean=0.019,
                p05=0.0,
                p25=0.01,
                p50=0.019,
                p75=0.025,
                p95=0.03,
                committee_percentile=0.5,
                seed_sha256=hashlib.sha256(
                    bytes.fromhex("a" * 64) + b"|random-committee|1000"
                ).hexdigest(),
            )
            if random_control
            else None
        ),
    )


def test_result_writer_refuses_incomplete_enum_grid_before_db_write() -> None:
    conn = MagicMock()
    with pytest.raises(ValueError, match="incomplete benchmark set"):
        insert_benchmark_results(
            conn,
            [_result(Benchmark.SPY, BenchmarkVariant.ADJUSTED)],
            expected_weeks=(date(2026, 1, 5),),
        )
    conn.execute.assert_not_called()


def test_halted_writer_accepts_only_complete_individual_variants() -> None:
    run_id = uuid4()
    rows = [
        _result(benchmark, BenchmarkVariant.UNADJUSTED, run_id=run_id).model_copy(
            update={"completeness": OutcomeCompleteness.HALTED}
        )
        for benchmark in Benchmark
    ]
    assert_benchmark_variant_completeness(
        ((row.week_start, row.benchmark, row.variant) for row in rows),
        (date(2026, 1, 5),),
    )
    with pytest.raises(ValueError, match="incomplete benchmark variant"):
        insert_benchmark_results(MagicMock(), rows[:-1], expected_weeks=(date(2026, 1, 5),))


def test_halted_writer_persists_variants_independently_with_exact_replay(db: Connection) -> None:
    run_id = uuid4()
    start = datetime(2026, 1, 5, tzinfo=UTC)
    run = RunRecord(
        run_id=run_id,
        mode=RunMode.LIVE,
        as_of=start,
        config_hash="d" * 64,
        status=RunStatus.COMMITTED,
        started_at=start,
        ended_at=start,
    )
    write.upsert_run(db, run)
    commitment = DecisionCommitment(run_id=run_id, sha256="a" * 64, committed_at=start)
    write.insert_commitment(db, commitment)
    write.upsert_anchor(
        db,
        CommitmentAnchor(
            run_id=run_id,
            sha256=commitment.sha256,
            ots_proof=b"proof",
            git_commit="abcdef1234567",
            anchored_at=start,
            verified_at=start,
        ),
    )
    event = KillSwitchEvent(
        run_id=run_id,
        triggered_at=datetime(2026, 1, 7, tzinfo=UTC),
        trigger=KillTrigger.DAILY_LOSS,
    )
    assert write.record_halt(db, event)
    assert not write.record_halt(
        db,
        KillSwitchEvent(
            run_id=run_id,
            triggered_at=datetime(2026, 1, 8, tzinfo=UTC),
            trigger=KillTrigger.MANUAL,
            flattened=True,
        ),
    )
    cutoff = datetime(2026, 2, 1, tzinfo=UTC)
    source_inputs = _week_inputs(date(2026, 1, 5), halted=True)
    inputs = replace(
        source_inputs,
        run_id=run_id,
        completeness=OutcomeCompleteness.HALTED,
        outcome_cutoff=cutoff,
        commitment_sha256=commitment.sha256,
        entry_references={
            sid: ref.model_copy(update={"run_id": run_id})
            for sid, ref in source_inputs.entry_references.items()
        },
        period_references={
            sid: ref.model_copy(update={"run_id": run_id})
            for sid, ref in source_inputs.period_references.items()
        },
        random_bundles={
            sid: bundle.model_copy(
                update={
                    "agent_verdicts": tuple(
                        verdict.model_copy(update={"run_id": run_id})
                        for verdict in bundle.agent_verdicts
                    )
                }
            )
            for sid, bundle in source_inputs.random_bundles.items()
        },
    )
    evaluated = evaluate_benchmark_week(inputs)
    unadjusted = [row for row in evaluated if row.variant is BenchmarkVariant.UNADJUSTED]
    assert insert_benchmark_results(db, unadjusted, expected_weeks=(date(2026, 1, 5),)) == 6
    assert insert_benchmark_results(db, unadjusted, expected_weeks=(date(2026, 1, 5),)) == 0
    persisted = as_of.benchmark_results(db, run_id)
    assert {(row.benchmark, row.variant, row.provenance_sha256) for row in persisted} == {
        (row.benchmark, row.variant, row.provenance_sha256) for row in unadjusted
    }
    assert {row.provenance_sha256 for row in persisted} == {
        row.provenance_sha256 for row in unadjusted
    }
    adjusted = [row for row in evaluated if row.variant is BenchmarkVariant.ADJUSTED]
    assert insert_benchmark_results(db, adjusted, expected_weeks=(date(2026, 1, 5),)) == 6
    assert len(as_of.benchmark_results(db, run_id)) == 12
    assert as_of.first_kill_switch_event(db, run_id) == event
    promoted = run.model_copy(update={"status": RunStatus.SCORED})
    write.upsert_run(db, promoted)
    status: str = db.execute(
        text("SELECT status FROM runs WHERE run_id=:run"), {"run": run_id}
    ).scalar_one()
    assert status == "PARTIAL"
    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(text("UPDATE runs SET status='SCORED' WHERE run_id=:run"), {"run": run_id})


def test_result_writer_requires_committed_run_and_matching_admission_material(
    db: Connection,
) -> None:
    run_id = uuid4()
    now = datetime(2026, 1, 5, tzinfo=UTC)
    from store import write

    write.upsert_run(
        db,
        RunRecord(
            run_id=run_id,
            mode=RunMode.BACKTEST,
            as_of=now,
            config_hash="d" * 64,
            status=RunStatus.COMMITTED,
            started_at=now,
            ended_at=now,
        ),
    )
    rows = [
        _result(benchmark, variant, run_id=run_id)
        for benchmark in Benchmark
        for variant in BenchmarkVariant
    ]
    with pytest.raises(ValueError, match="stored run and decision commitment"):
        insert_benchmark_results(db, rows, expected_weeks=(date(2026, 1, 5),))
