"""The scorability gate over a real Postgres: one-snapshot evidence and end-to-end admission."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from contracts.commitment import MaterialDefectKind, verify_material
from contracts.enums import RunMode, RunStatus
from contracts.models import CommitmentAnchor, StepArtifacts
from evaluation import scorable as s
from orchestration.sink import DecisionSink
from store import scoring_read
from tests.evaluation.test_scorable import Git, Loader
from tests.orchestration.anchoring_support import GIT_COMMIT, BlockHeaders, confirmed_proof
from tests.orchestration.test_sink import AS_OF, run_record
from tests.orchestration.test_store_anchoring import committed, halt
from tests.store import factories as f

ANCHORED_AT = AS_OF + timedelta(hours=1)
BLOCK_TIME = int((AS_OF + timedelta(hours=2)).timestamp())
NOW = AS_OF + timedelta(days=30)


@pytest.fixture
def engine(pg_engine: Engine) -> Iterator[Engine]:
    def wipe() -> None:
        with pg_engine.begin() as c:
            c.execute(text("TRUNCATE runs, securities RESTART IDENTITY CASCADE"))

    wipe()
    yield pg_engine
    wipe()


@pytest.fixture
def sid(engine: Engine) -> int:
    with engine.begin() as c:
        return f.security(c)


def anchor_confirmed(sink: DecisionSink, run_id: UUID) -> bytes:
    """Anchor with a proof that verifies against the returned merkle root; returns the root."""
    material = sink.load_commitment_material(run_id)
    assert material is not None
    proof, root = confirmed_proof(material.stored_sha256)
    assert sink.mark_anchored(
        CommitmentAnchor(
            run_id=run_id,
            sha256=material.stored_sha256,
            ots_proof=proof,
            git_commit=GIT_COMMIT,
            anchored_at=ANCHORED_AT,
        )
    )
    return root


def gate(engine: Engine, run_id: UUID, root: bytes, loader: Loader) -> str:
    return s.run_scoring(
        s.ScoringRequest(run_id),
        source=scoring_read.ScoringReader(engine),
        headers=BlockHeaders(root, BLOCK_TIME),
        git=Git(),
        clock=lambda: NOW,
        load_outcomes=loader,
    )


def refusal(engine: Engine, run_id: UUID, root: bytes = b"\x00" * 32) -> s.ScoringRefusedError:
    loader = Loader()
    with pytest.raises(s.ScoringRefusedError) as info:
        gate(engine, run_id, root, loader)
    assert loader.calls == []
    return info.value


def sql(engine: Engine, statement: str, **params: Any) -> None:
    with engine.begin() as c:
        c.execute(text(statement), params)


# --- end to end -----------------------------------------------------------------------------


def test_an_anchored_backtest_is_admitted_from_the_stored_rows(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    root = anchor_confirmed(sink, run_id)
    loader = Loader()
    assert gate(engine, run_id, root, loader) == "outcomes"
    (ticket,) = loader.calls
    assert ticket.completeness is s.Completeness.COMPLETE and ticket.mode is RunMode.BACKTEST
    material = sink.load_commitment_material(run_id)
    assert material is not None
    assert ticket.commitment_sha256 == verify_material(material)
    # the read side rebuilds exactly the material the write side rebuilds
    ev = scoring_read.ScoringReader(engine).load(run_id)
    assert ev.decisions == material.decisions and ev.snapshot == material.snapshot
    assert ev.commitment is not None and ev.commitment.sha256 == material.stored_sha256
    assert ev.anchor == material.anchor and ev.defects == ()


def test_missing_run_and_missing_commitment(engine: Engine, sid: int) -> None:
    assert refusal(engine, uuid4()).reason is s.RefusalReason.RUN_MISSING
    sink = DecisionSink(engine)
    run_id = uuid4()
    sink.flush_step(StepArtifacts(run=run_record(run_id, RunStatus.ANCHORED)))
    assert refusal(engine, run_id).reason is s.RefusalReason.NO_COMMITMENT


def test_a_committed_but_unanchored_run_is_not_scorable(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    assert refusal(engine, run_id).reason is s.RefusalReason.NOT_SCORABLE_STATUS


@pytest.mark.parametrize("what", ["decision", "book", "cio"])
def test_tampered_rows_are_refused(engine: Engine, sid: int, what: str) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    root = anchor_confirmed(sink, run_id)
    if what == "decision":
        sql(
            engine,
            "UPDATE committee_decisions SET target_weight = 0.07 WHERE run_id = :r",
            r=run_id,
        )
    elif what == "book":
        sql(
            engine,
            "UPDATE portfolio_snapshots SET book = jsonb_set(book, '{positions,0,pooled_p}',"
            " '0.7') WHERE run_id = :r",
            r=run_id,
        )
    else:
        sql(
            engine,
            "UPDATE portfolio_snapshots SET cio = jsonb_set(cio, '{rationale}', '\"edited\"')"
            " WHERE run_id = :r",
            r=run_id,
        )
    assert refusal(engine, run_id, root).reason is s.RefusalReason.COMMITMENT_MISMATCH


def test_unrebuildable_rows_keep_their_defect_kind(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    root = anchor_confirmed(sink, run_id)
    sql(engine, "UPDATE committee_decisions SET entity_token = NULL WHERE run_id = :r", r=run_id)
    err = refusal(engine, run_id, root)
    assert err.reason is s.RefusalReason.MATERIAL_INCOMPLETE
    assert MaterialDefectKind.DECISION_ENTITY_TOKEN_MISSING.value in err.detail

    other = committed(sink, sid)
    root = anchor_confirmed(sink, other)
    sql(engine, "DELETE FROM portfolio_snapshots WHERE run_id = :r", r=other)
    err = refusal(engine, other, root)
    assert err.reason is s.RefusalReason.MATERIAL_INCOMPLETE
    assert MaterialDefectKind.SNAPSHOT_MISSING.value in err.detail


def test_a_halted_run_needs_the_durable_halt_and_a_valid_anchor(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    root = anchor_confirmed(sink, run_id)
    assert sink.record_halt(halt(run_id))
    loader = Loader()
    gate(engine, run_id, root, loader)
    assert loader.calls[0].completeness is s.Completeness.HALTED
    assert loader.calls[0].may_become_scored is False
    # a halt before any anchor: refused
    bare = committed(sink, sid)
    assert sink.record_halt(halt(bare))
    assert refusal(engine, bare).reason is s.RefusalReason.ANCHOR_MISSING
    # the halt reason without its durable event row: refused
    no_event = committed(sink, sid)
    root2 = anchor_confirmed(sink, no_event)
    assert sink.record_halt(halt(no_event))
    sql(engine, "DELETE FROM kill_switch_events WHERE run_id = :r", r=no_event)
    assert refusal(engine, no_event, root2).reason is s.RefusalReason.HALT_EVIDENCE_MISSING


def test_verified_at_in_the_database_does_not_admit_a_run(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    anchor_confirmed(sink, run_id)
    sql(engine, "UPDATE commitment_anchors SET verified_at = now() WHERE run_id = :r", r=run_id)
    assert (
        refusal(engine, run_id, b"\x11" * 32).reason is s.RefusalReason.HEADER_VERIFICATION_FAILED
    )


# --- the single-snapshot guarantee ------------------------------------------------------------


def test_evidence_needs_a_repeatable_read_read_only_transaction(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    with engine.connect() as conn, pytest.raises(RuntimeError, match="REPEATABLE READ"):
        scoring_read.load_scoring_evidence(conn, run_id)
    with engine.connect() as conn:
        rr = conn.execution_options(isolation_level="REPEATABLE READ")
        with rr.begin(), pytest.raises(RuntimeError, match="read_only"):
            scoring_read.load_scoring_evidence(rr, run_id)  # not read-only


def _interleave(
    monkeypatch: pytest.MonkeyPatch, name: str, concurrent: Callable[[], object]
) -> None:
    """Run ``concurrent`` (committing on another connection) just before query ``name``."""
    original = getattr(scoring_read, name)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        concurrent()
        return original(*args, **kwargs)

    monkeypatch.setattr(scoring_read, name, wrapper)


def _read_committed(engine: Engine, run_id: UUID) -> Any:
    """The same reads without the snapshot guarantee (what the guard exists to prevent)."""
    with engine.connect() as conn:
        return scoring_read.load_scoring_evidence(conn, run_id)


def test_a_concurrent_anchor_cannot_tear_the_evidence(
    engine: Engine, sid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)

    def anchor_now() -> None:
        anchor_confirmed(sink, run_id)  # commits on another connection: COMMITTED -> ANCHORED

    _interleave(monkeypatch, "_anchor", anchor_now)
    ev = scoring_read.ScoringReader(engine).load(run_id)
    assert ev.run is not None and ev.run.status is RunStatus.COMMITTED
    assert ev.anchor is None  # neither half of the concurrent commit is visible
    monkeypatch.undo()
    later = scoring_read.ScoringReader(engine).load(run_id)
    assert later.run is not None and later.run.status is RunStatus.ANCHORED
    assert later.anchor is not None


def test_a_concurrent_halt_cannot_tear_the_evidence(
    engine: Engine, sid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    anchor_confirmed(sink, run_id)
    _interleave(monkeypatch, "_events", lambda: sink.record_halt(halt(run_id)) and None)
    ev = scoring_read.ScoringReader(engine).load(run_id)
    assert ev.run is not None and ev.run.status is RunStatus.ANCHORED
    assert ev.kill_switch_events == ()  # not a PARTIAL run with events, nor the reverse
    monkeypatch.undo()
    later = scoring_read.ScoringReader(engine).load(run_id)
    assert later.run is not None and later.run.status is RunStatus.PARTIAL
    assert len(later.kill_switch_events) == 1


def test_a_concurrent_decision_change_cannot_tear_the_evidence(
    engine: Engine, sid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    before = scoring_read.ScoringReader(engine).load(run_id)

    def tamper() -> None:
        sql(
            engine,
            "UPDATE committee_decisions SET target_weight = 0.07 WHERE run_id = :r",
            r=run_id,
        )

    _interleave(monkeypatch, "_decision_rows", tamper)
    ev = scoring_read.ScoringReader(engine).load(run_id)
    assert ev.decisions == before.decisions and ev.snapshot == before.snapshot


def test_the_interleaving_does_tear_without_a_snapshot(
    engine: Engine, sid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: with the guard bypassed the same interleaving mixes two database states."""
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    monkeypatch.setattr(scoring_read, "_require_snapshot_isolation", lambda conn: None)
    _interleave(monkeypatch, "_anchor", lambda: anchor_confirmed(sink, run_id))
    torn = _read_committed(engine, run_id)
    assert torn.run.status is RunStatus.COMMITTED and torn.anchor is not None  # a torn object
