from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from contracts.enums import (
    AgentName,
    DataSufficiency,
    FeedName,
    Horizon,
    OutcomeCompleteness,
    Stance,
)
from contracts.models import AgentVerdict, EvidenceRef, OutcomeRecord, VerdictRecord
from store import as_of
from store.write import insert_agent_verdicts, persist_outcomes
from tests.store import factories as f

RUN_ID = uuid4()


def outcome(horizon: Horizon) -> OutcomeRecord:
    return OutcomeRecord(
        run_id=RUN_ID,
        security_id=1,
        horizon=horizon,
        fwd_return=0.1,
        sector_fwd_return=0.02,
        scored_at=datetime(2024, 2, 1, tzinfo=UTC),
        resolved_at=datetime(2024, 1, 31, tzinfo=UTC),
    )


def test_complete_batch_refuses_missing_horizon_before_any_write() -> None:
    conn = MagicMock()
    with pytest.raises(ValueError, match="horizons 5, 21, and 63"):
        persist_outcomes(
            conn,
            [outcome(Horizon.D5)],
            completeness=OutcomeCompleteness.COMPLETE,
            requested_at=datetime(2024, 2, 1, tzinfo=UTC),
        )
    conn.execute.assert_not_called()


def test_halted_batch_writes_independent_horizons_without_run_promotion() -> None:
    conn = MagicMock()
    conn.execute.return_value.all.return_value = [(1,), (2,)]
    rows = [outcome(Horizon.D5), outcome(Horizon.D21)]
    assert (
        persist_outcomes(
            conn,
            rows,
            completeness=OutcomeCompleteness.HALTED,
            requested_at=datetime(2024, 2, 1, tzinfo=UTC),
        )
        == 2
    )
    conn.execute.assert_called_once()


def test_resolved_evidence_must_precede_ticket_cutoff_and_scoring_must_follow_it() -> None:
    conn = MagicMock()
    row = outcome(Horizon.D5).model_copy(update={"resolved_at": datetime(2024, 2, 2, tzinfo=UTC)})
    with pytest.raises(ValueError, match="dependencies"):
        persist_outcomes(
            conn,
            [row],
            completeness=OutcomeCompleteness.HALTED,
            requested_at=datetime(2024, 2, 1, tzinfo=UTC),
        )
    conn.execute.assert_not_called()


def seed_run(db: Connection, *, status: str = "ANCHORED") -> int:
    sid = f.security(db)
    db.execute(
        text(
            "INSERT INTO runs (run_id, mode, as_of, config_hash, status, started_at) "
            "VALUES (:run, 'backtest', :at, :hash, :status, :at)"
        ),
        {"run": RUN_ID, "at": datetime(2024, 1, 1, tzinfo=UTC), "hash": "c" * 64, "status": status},
    )
    return sid


def seed_anchor(db: Connection) -> None:
    db.execute(
        text(
            "INSERT INTO decision_commitments (run_id, sha256, committed_at) "
            "VALUES (:run, :hash, :at)"
        ),
        {"run": RUN_ID, "hash": "a" * 64, "at": datetime(2024, 1, 2, tzinfo=UTC)},
    )
    db.execute(
        text(
            "INSERT INTO commitment_anchors (run_id, sha256, ots_proof, git_commit, anchored_at) "
            "VALUES (:run, :hash, :proof, 'abc', :at)"
        ),
        {
            "run": RUN_ID,
            "hash": "a" * 64,
            "proof": b"proof",
            "at": datetime(2024, 1, 2, tzinfo=UTC),
        },
    )
    db.execute(
        text("UPDATE commitment_anchors SET verified_at=:at WHERE run_id=:run"),
        {"run": RUN_ID, "at": datetime(2024, 1, 30, tzinfo=UTC)},
    )


def test_complete_outcomes_and_status_transition_are_atomic_and_idempotent(
    db: Connection,
) -> None:
    sid = seed_run(db)
    seed_anchor(db)
    db.execute(
        text(
            "INSERT INTO committee_decisions "
            "(run_id, security_id, horizon, pooled_p, dispersion, weights, target_weight) "
            "VALUES (:run, :sid, :h, 0.5, 0, '{}'::jsonb, 0)"
        ),
        [{"run": RUN_ID, "sid": sid, "h": int(h)} for h in Horizon],
    )
    rows = [outcome(h).model_copy(update={"security_id": sid}) for h in Horizon]
    requested_at = datetime(2024, 2, 1, tzinfo=UTC)
    assert (
        persist_outcomes(
            db,
            rows,
            completeness=OutcomeCompleteness.COMPLETE,
            requested_at=requested_at,
        )
        == 3
    )
    assert (
        db.execute(text("SELECT status FROM runs WHERE run_id=:run"), {"run": RUN_ID}).scalar_one()
        == "SCORED"
    )
    assert (
        persist_outcomes(
            db,
            rows,
            completeness=OutcomeCompleteness.COMPLETE,
            requested_at=requested_at,
        )
        == 0
    )
    verdict = AgentVerdict(
        stance=Stance.BUY,
        p_outperform_5=0.7,
        p_outperform_21=0.6,
        p_outperform_63=0.55,
        key_evidence=(EvidenceRef(source=FeedName.FEATURES, row_id="f1", note="evidence"),),
        risks=(),
        data_sufficiency=DataSufficiency.FULL,
        run_id=RUN_ID,
        agent=AgentName.VALUE,
        entity_token="ENTITY_01",
        as_of=datetime(2024, 1, 1, tzinfo=UTC),
        prompt_version="v1-test",
        model_served="test/model",
        valid=True,
    )
    insert_agent_verdicts(
        db,
        [
            VerdictRecord(
                security_id=sid,
                verdict=verdict,
                tokens_in=1,
                tokens_out=1,
                cost_usd=0,
                latency_ms=1,
            )
        ],
    )
    assert as_of.resolved_forecast_outcomes(db, datetime(2024, 1, 30, tzinfo=UTC), Horizon.D5) == []
    assert as_of.resolved_forecast_outcomes(db, datetime(2024, 1, 31, tzinfo=UTC), Horizon.D5) == []
    history = as_of.resolved_forecast_outcomes(db, datetime(2024, 2, 2, tzinfo=UTC), Horizon.D5)
    assert len(history) == 1
    assert history[0].forecast == 0.7 and history[0].outperformed


def test_complete_batch_and_status_transition_roll_back_together(db: Connection) -> None:
    sid = seed_run(db)
    seed_anchor(db)
    db.execute(
        text(
            "INSERT INTO committee_decisions "
            "(run_id, security_id, horizon, pooled_p, dispersion, weights, target_weight) "
            "VALUES (:run, :sid, :h, 0.5, 0, '{}'::jsonb, 0)"
        ),
        [{"run": RUN_ID, "sid": sid, "h": int(h)} for h in Horizon],
    )
    db.execute(
        text(
            "CREATE FUNCTION reject_scored_status_for_test() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "IF NEW.status = 'SCORED' THEN RAISE EXCEPTION 'forced status failure'; END IF; "
            "RETURN NEW; END $$"
        )
    )
    db.execute(
        text(
            "CREATE TRIGGER reject_scored_status_for_test BEFORE UPDATE ON runs "
            "FOR EACH ROW EXECUTE FUNCTION reject_scored_status_for_test()"
        )
    )

    rows = [outcome(h).model_copy(update={"security_id": sid}) for h in Horizon]
    with pytest.raises(DBAPIError, match="forced status failure"), db.begin_nested():
        persist_outcomes(
            db,
            rows,
            completeness=OutcomeCompleteness.COMPLETE,
            requested_at=datetime(2024, 2, 1, tzinfo=UTC),
        )

    assert (
        db.execute(
            text("SELECT count(*) FROM outcomes WHERE run_id=:run"), {"run": RUN_ID}
        ).scalar_one()
        == 0
    )
    assert (
        db.execute(text("SELECT status FROM runs WHERE run_id=:run"), {"run": RUN_ID}).scalar_one()
        == "ANCHORED"
    )


def test_halted_run_cannot_be_promoted_by_a_generic_status_update(db: Connection) -> None:
    seed_run(db, status="PARTIAL")
    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(text("UPDATE runs SET status='SCORED' WHERE run_id=:run"), {"run": RUN_ID})


def test_outcome_insert_requires_matching_verified_commitment_and_status(db: Connection) -> None:
    sid = seed_run(db)
    rows = [outcome(h).model_copy(update={"security_id": sid}) for h in Horizon]
    with pytest.raises(DBAPIError, match="outcome admission evidence or status"), db.begin_nested():
        persist_outcomes(
            db,
            rows,
            completeness=OutcomeCompleteness.COMPLETE,
            requested_at=datetime(2024, 2, 1, tzinfo=UTC),
        )


def test_outcome_insert_requires_a_durable_halt_for_halted_completeness(db: Connection) -> None:
    sid = seed_run(db, status="PARTIAL")
    seed_anchor(db)
    with pytest.raises(DBAPIError, match="outcome admission evidence or status"), db.begin_nested():
        persist_outcomes(
            db,
            [outcome(Horizon.D5).model_copy(update={"security_id": sid})],
            completeness=OutcomeCompleteness.HALTED,
            requested_at=datetime(2024, 2, 1, tzinfo=UTC),
        )


def test_outcome_insert_requires_scoring_after_verified_anchor(db: Connection) -> None:
    sid = seed_run(db)
    seed_anchor(db)
    db.execute(
        text("UPDATE commitment_anchors SET verified_at=:at WHERE run_id=:run"),
        {"run": RUN_ID, "at": datetime(2024, 2, 2, tzinfo=UTC)},
    )
    rows = [outcome(h).model_copy(update={"security_id": sid}) for h in Horizon]
    with pytest.raises(DBAPIError, match="outcome admission evidence or status"), db.begin_nested():
        persist_outcomes(
            db,
            rows,
            completeness=OutcomeCompleteness.COMPLETE,
            requested_at=datetime(2024, 2, 1, tzinfo=UTC),
        )
