"""Anchoring, halt, claim and reset transitions against a real Postgres (service-gated)."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from contracts.commitment import verify_material
from contracts.enums import KillTrigger, RunMode, RunStatus
from contracts.errors import AnchorIncompleteError, ResetRefusedError, RunHaltedError
from contracts.models import CommitmentAnchor, KillSwitchEvent, RunRecord, StepArtifacts
from orchestration.sink import DecisionSink
from tests.orchestration.anchoring_support import GIT_COMMIT, pending_proof
from tests.orchestration.test_sink import AS_OF, run_record, step
from tests.store import factories as f

CONFIG = "c" * 64
LATER = AS_OF + timedelta(hours=1)


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


def committed(sink: DecisionSink, sid: int, *, status: RunStatus = RunStatus.COMMITTED) -> UUID:
    run_id = uuid4()
    run = run_record(run_id, status).model_copy(update={"ended_at": AS_OF})
    sink.flush_step(step(run_id, sid, run=run, kill_switch=()))
    return run_id


def full_anchor(sink: DecisionSink, run_id: UUID) -> CommitmentAnchor:
    material = sink.load_commitment_material(run_id)
    assert material is not None
    return CommitmentAnchor(
        run_id=run_id,
        sha256=material.stored_sha256,
        ots_proof=pending_proof(material.stored_sha256),
        git_commit=GIT_COMMIT,
        anchored_at=LATER,
    )


def status_of(sink: DecisionSink, run_id: UUID) -> RunStatus:
    run = sink.load_run(run_id)
    assert run is not None
    return run.status


def halt(run_id: UUID, trigger: KillTrigger = KillTrigger.MANUAL, **kw: Any) -> KillSwitchEvent:
    return KillSwitchEvent(run_id=run_id, triggered_at=LATER, trigger=trigger, **kw)


def count(engine: Engine, table: str) -> int:
    with engine.connect() as c:
        return int(c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


# --- ANCHORED --------------------------------------------------------------------------------


def test_mark_anchored_writes_the_anchor_and_moves_the_run_once(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    anchor = full_anchor(sink, run_id)
    assert sink.mark_anchored(anchor) is True
    assert status_of(sink, run_id) is RunStatus.ANCHORED
    assert sink.mark_anchored(anchor) is False  # exactly one caller moves it
    stored = sink.load_anchor(run_id)
    assert stored is not None and stored.git_commit == GIT_COMMIT and stored.ots_proof is not None
    material = sink.load_commitment_material(run_id)
    assert material is not None and material.anchor == stored
    assert verify_material(material) == anchor.sha256  # anchor and recomputed hash agree


def test_an_incomplete_anchor_never_anchors(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    good = full_anchor(sink, run_id)
    for bad in (
        good.model_copy(update={"git_commit": None}),
        good.model_copy(update={"ots_proof": None}),
    ):
        with pytest.raises(AnchorIncompleteError):
            sink.mark_anchored(bad)
    assert (
        status_of(sink, run_id) is RunStatus.COMMITTED and count(engine, "commitment_anchors") == 0
    )


def test_an_anchor_must_name_the_runs_own_commitment_hash(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    wrong = "e" * 64
    forged = CommitmentAnchor(
        run_id=run_id,
        sha256=wrong,
        ots_proof=pending_proof(wrong),
        git_commit=GIT_COMMIT,
        anchored_at=LATER,
    )
    with pytest.raises(IntegrityError):  # the composite foreign key refuses it
        sink.mark_anchored(forged)
    assert status_of(sink, run_id) is RunStatus.COMMITTED  # and the state did not move
    assert count(engine, "commitment_anchors") == 0


def test_an_uncommitted_run_cannot_be_anchored(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = uuid4()
    sink.flush_step(StepArtifacts(run=run_record(run_id)))
    orphan = CommitmentAnchor(
        run_id=run_id,
        sha256="a" * 64,
        ots_proof=pending_proof("a" * 64),
        git_commit=GIT_COMMIT,
        anchored_at=LATER,
    )
    with pytest.raises(IntegrityError):
        sink.mark_anchored(orphan)


def test_only_an_anchored_run_can_execute(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    assert sink.mark_executed(run_id, LATER) is False
    sink.mark_anchored(full_anchor(sink, run_id))
    assert sink.mark_executed(run_id, LATER) is True
    assert status_of(sink, run_id) is RunStatus.EXECUTED


def test_anchor_upgrades_keep_the_hash_and_are_listed_until_confirmed(
    engine: Engine, sid: int
) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    anchor = full_anchor(sink, run_id)
    sink.mark_anchored(anchor)
    assert [a.run_id for a in sink.anchors_awaiting_confirmation()] == [run_id]
    sink.record_anchor(anchor.model_copy(update={"verified_at": LATER + timedelta(hours=1)}))
    assert sink.anchors_awaiting_confirmation() == []
    assert status_of(sink, run_id) is RunStatus.ANCHORED  # upgrades never move the run


# --- the audited halt transition --------------------------------------------------------------


@pytest.mark.parametrize("via", ["committed", "anchored", "executed"])
def test_a_halt_marks_a_committed_run_partial_and_keeps_all_evidence(
    engine: Engine, sid: int, via: str
) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    if via != "committed":
        sink.mark_anchored(full_anchor(sink, run_id))
    if via == "executed":
        assert sink.mark_executed(run_id, LATER)
    before = {t: count(engine, t) for t in ("decision_commitments", "commitment_anchors", "orders")}

    moved = sink.record_halt(halt(run_id, flattened=True))
    assert moved is True
    run = sink.load_run(run_id)
    assert run is not None and run.status is RunStatus.PARTIAL
    assert run.status_reason == "kill_switch:manual"
    after = {t: count(engine, t) for t in before}
    assert after == before  # no commitment, anchor or order row was touched
    assert count(engine, "kill_switch_events") == 1
    assert sink.load_commitment_material(run_id) is not None  # still verifiable


def test_a_halt_before_commit_records_the_event_but_moves_nothing(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = uuid4()
    sink.flush_step(StepArtifacts(run=run_record(run_id)))
    assert sink.record_halt(halt(run_id)) is False
    assert (
        status_of(sink, run_id) is RunStatus.AGENTS_OK and count(engine, "kill_switch_events") == 1
    )


def test_a_halted_run_is_terminal_nothing_overwrites_it(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    sink.record_halt(halt(run_id, KillTrigger.DAILY_LOSS, daily_loss=0.05))
    for status in (RunStatus.COMMITTED, RunStatus.ANCHORED, RunStatus.EXECUTED, RunStatus.FAILED):
        sink.flush_step(StepArtifacts(run=run_record(run_id, status)))
        run = sink.load_run(run_id)
        assert run is not None and run.status is RunStatus.PARTIAL
        assert run.status_reason == "kill_switch:daily_loss"
    assert sink.mark_executed(run_id, LATER) is False
    assert (
        sink.mark_anchored(full_anchor(sink, run_id)) is False
    )  # the anchor may be stored, the state stays


def test_a_second_halt_adds_evidence_without_rewriting_the_first_reason(
    engine: Engine, sid: int
) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    sink.record_halt(halt(run_id, KillTrigger.STALE_FEED))
    assert sink.record_halt(halt(run_id, KillTrigger.MANUAL, flattened=True)) is False
    run = sink.load_run(run_id)
    assert run is not None and run.status_reason == "kill_switch:stale_feed"
    assert count(engine, "kill_switch_events") == 2


# --- one run per (mode, as_of, config) ---------------------------------------------------------


def claim(
    sink: DecisionSink, *, fresh: bool = False, as_of: datetime = AS_OF
) -> tuple[RunRecord, bool]:
    return sink.claim_run(
        mode=RunMode.LIVE,
        as_of=as_of,
        config_hash=CONFIG,
        run_id=uuid4(),
        started_at=LATER,
        fresh=fresh,
    )


def test_claims_return_the_same_run_however_many_workers_ask(engine: Engine) -> None:
    sink = DecisionSink(engine)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: claim(sink), range(16)))
    assert len({r.run_id for r, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert count(engine, "runs") == 1 and results[0][0].status is RunStatus.PENDING


def test_a_failed_run_never_blocks_a_fresh_one(engine: Engine) -> None:
    sink = DecisionSink(engine)
    first, _ = claim(sink)
    sink.flush_step(StepArtifacts(run=first.model_copy(update={"status": RunStatus.FAILED})))
    second, created = claim(sink)
    assert created and second.run_id != first.run_id  # failed-run -> new run, new run_id
    assert claim(sink) == (second, False)


def test_a_halted_run_blocks_an_automatic_claim_until_an_operator_asks_for_a_fresh_one(
    engine: Engine, sid: int
) -> None:
    sink = DecisionSink(engine)
    run_id = committed(sink, sid)
    with engine.begin() as c:  # make it the live run this as_of and config would claim
        c.execute(
            text("UPDATE runs SET mode = 'live', config_hash = :h WHERE run_id = :r"),
            {"h": CONFIG, "r": run_id},
        )
    sink.record_halt(halt(run_id))
    with pytest.raises(RunHaltedError):
        claim(sink)  # a duplicate beat must not silently start a new run
    fresh, created = claim(sink, fresh=True)
    assert created and fresh.run_id != run_id  # recovery is a fresh run_id
    assert status_of(sink, run_id) is RunStatus.PARTIAL


def test_listing_runs_by_status(engine: Engine) -> None:
    sink = DecisionSink(engine)
    run, _ = claim(sink)
    other, _ = claim(sink, as_of=AS_OF + timedelta(days=7))
    assert {r.run_id for r in sink.list_runs(mode=RunMode.LIVE, statuses=[RunStatus.PENDING])} == {
        run.run_id,
        other.run_id,
    }
    assert sink.list_runs(mode=RunMode.BACKTEST, statuses=[RunStatus.PENDING]) == []


# --- reset -------------------------------------------------------------------------------------


def test_reset_clears_an_uncommitted_run_but_keeps_what_it_cleared_in_the_audit(
    engine: Engine, sid: int
) -> None:
    sink = DecisionSink(engine)
    run_id = uuid4()
    sink.flush_step(step(run_id, sid, commitment=None, kill_switch=()))
    assert count(engine, "agent_verdicts") == 2 and count(engine, "dlq_records") == 1

    cleared = sink.reset_run(run_id, actor="operator", reason="bad partition", now=LATER)
    assert cleared == {"agent_verdicts": 2, "committee_decisions": 2, "portfolio_snapshots": 1}
    assert count(engine, "agent_verdicts") == 0 and count(engine, "committee_decisions") == 0
    assert count(engine, "dlq_records") == 1  # failure evidence survives a reset
    run = sink.load_run(run_id)
    assert run is not None and run.status is RunStatus.PENDING and run.total_cost_usd == 1.25
    with engine.connect() as c:
        row = c.execute(text("SELECT actor, prior_status, cleared FROM run_resets")).one()
    assert row[0] == "operator" and row[1] == "AGENTS_OK"
    assert len(row[2]["agent_verdicts"]) == 2  # what was cleared is preserved

    assert sink.load_verdicts(run_id) == []  # every task key is retryable again


@pytest.mark.parametrize("evidence", ["commitment", "anchor", "orders", "halt"])
def test_reset_is_refused_once_the_run_holds_audit_evidence(
    engine: Engine, sid: int, evidence: str
) -> None:
    sink = DecisionSink(engine)
    if evidence in ("commitment", "anchor"):
        run_id = committed(sink, sid)
        if evidence == "anchor":
            sink.mark_anchored(full_anchor(sink, run_id))
    else:
        run_id = uuid4()
        sink.flush_step(step(run_id, sid, commitment=None, kill_switch=()))
        if evidence == "halt":
            sink.record_halt(halt(run_id))
        else:
            with engine.begin() as c:
                c.execute(
                    text(
                        "INSERT INTO orders (run_id, broker_order_id, security_id, side, qty,"
                        " status, submitted_at, client_order_id, kind, filled_qty, decision_price,"
                        " reference_price, reference_source) VALUES (:r, 'B1', :s, 'buy', 1,"
                        " 'open', now(), :c, 'limit', 0, 1, 1, 'iex_mid')"
                    ),
                    {"r": run_id, "s": sid, "c": f"{run_id}-{sid}-lim"},
                )
    before = {t: count(engine, t) for t in ("agent_verdicts", "committee_decisions", "runs")}
    with pytest.raises(ResetRefusedError):
        sink.reset_run(run_id, actor="operator", reason="oops", now=LATER)
    assert {t: count(engine, t) for t in before} == before
    assert count(engine, "run_resets") == 0


def test_reset_of_an_unknown_run_is_refused(engine: Engine) -> None:
    with pytest.raises(ResetRefusedError):
        DecisionSink(engine).reset_run(uuid4(), actor="a", reason="b", now=LATER)


def test_the_reset_audit_is_append_only(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = uuid4()
    sink.flush_step(step(run_id, sid, commitment=None, kill_switch=()))
    sink.reset_run(run_id, actor="operator", reason="x", now=LATER)
    for sql in ("UPDATE run_resets SET reason = 'edited'", "DELETE FROM run_resets"):
        with engine.connect() as c, pytest.raises(DBAPIError, match="insert-only"):
            c.execute(text(sql))


def test_reset_is_refused_while_a_worker_holds_the_run(engine: Engine, sid: int) -> None:
    sink = DecisionSink(engine)
    run_id = uuid4()
    sink.flush_step(step(run_id, sid, commitment=None, kill_switch=()))
    with sink.run_lock(run_id, "advance") as held:
        assert held is True  # a worker is mid-pipeline for this run
        with pytest.raises(ResetRefusedError, match="being processed"):
            sink.reset_run(run_id, actor="operator", reason="x", now=LATER)
        assert count(engine, "agent_verdicts") == 2 and count(engine, "run_resets") == 0
    assert sink.reset_run(run_id, actor="operator", reason="x", now=LATER)  # free again
