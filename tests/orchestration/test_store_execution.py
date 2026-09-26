"""Durable kill switch, the atomic ANCHORED -> EXECUTED move and the stage on real Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from contracts.enums import KillTrigger, ReferenceSource, RunMode, RunStatus
from contracts.models import CommitmentAnchor, KillSwitchEvent, StepArtifacts
from orchestration.execution_stage import ExecutionStage, MarketView
from orchestration.sink import DecisionSink
from risk.kill_switch import KillSwitch, was_flattened
from tests.execution.fakes import T0, FakeBroker, FakeClock
from tests.orchestration.anchoring_support import GIT_COMMIT, pending_proof
from tests.orchestration.test_pipeline import BASE_CFG
from tests.orchestration.test_sink import AS_OF, run_record, step
from tests.store import factories as f


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


def live_committed(run_id: UUID, sid: int) -> StepArtifacts:
    run = run_record(run_id, RunStatus.COMMITTED).model_copy(update={"mode": RunMode.LIVE})
    return step(run_id, sid, run=run, kill_switch=())


def live_anchored(sink: DecisionSink, run_id: UUID, sid: int) -> None:
    """Commit a LIVE run, then anchor it exactly as the anchor stage does."""
    first = live_committed(run_id, sid)
    assert first.commitment is not None
    sink.flush_step(first)
    sha = first.commitment.sha256
    anchor = CommitmentAnchor(
        run_id=run_id,
        sha256=sha,
        ots_proof=pending_proof(sha),
        git_commit=GIT_COMMIT,
        anchored_at=AS_OF,
    )
    assert sink.mark_anchored(anchor) is True


def halt_event(
    run_id: UUID, trigger: KillTrigger, minutes: int = 0, **kw: object
) -> KillSwitchEvent:
    return KillSwitchEvent(
        run_id=run_id,
        triggered_at=AS_OF + timedelta(minutes=minutes),
        trigger=trigger,
        **kw,  # type: ignore[arg-type]
    )


def test_kill_switch_state_is_rebuilt_from_the_database(engine: Engine, sid: int) -> None:
    run_id = uuid4()
    sink = DecisionSink(engine)
    live_anchored(sink, run_id, sid)
    auto = halt_event(run_id, KillTrigger.DAILY_LOSS, 0, daily_loss=0.04, peak_drawdown=0.09)
    assert sink.record_halt(auto) is True  # this call moved the run
    assert sink.record_halt(auto) is False  # a replayed event is a no-op row-wise
    sink.record_halt(halt_event(run_id, KillTrigger.MANUAL, 5, flattened=True))
    halted = sink.load_run(run_id)
    assert halted is not None and halted.status is RunStatus.PARTIAL
    assert halted.status_reason == "kill_switch:daily_loss"  # the first trigger names the halt

    events = DecisionSink(engine).load_kill_switch_events(run_id)  # a "new process"
    assert [e.trigger for e in events] == [KillTrigger.DAILY_LOSS, KillTrigger.MANUAL]
    assert events[0].peak_drawdown == pytest.approx(0.09)
    ks = KillSwitch.from_events(events)
    assert ks.halted and ks.trigger is KillTrigger.DAILY_LOSS and was_flattened(events)
    assert DecisionSink(engine).load_kill_switch_events(uuid4()) == []  # scoped to the run
    assert not KillSwitch.from_events(DecisionSink(engine).load_kill_switch_events(uuid4())).halted


def test_mark_executed_advances_only_an_anchored_run_and_only_once(
    engine: Engine, sid: int
) -> None:
    sink = DecisionSink(engine)
    committed, anchored, partial = uuid4(), uuid4(), uuid4()
    sink.flush_step(live_committed(committed, sid))
    live_anchored(sink, anchored, sid)
    sink.flush_step(StepArtifacts(run=run_record(partial, RunStatus.PARTIAL)))

    assert sink.mark_executed(partial, AS_OF) is False
    assert sink.mark_executed(uuid4(), AS_OF) is False
    assert sink.mark_executed(committed, AS_OF) is False  # COMMITTED never executes
    assert sink.mark_executed(anchored, AS_OF) is True
    assert sink.mark_executed(anchored, AS_OF) is False  # exactly once
    run = sink.load_run(anchored)
    assert run is not None and run.status is RunStatus.EXECUTED
    still = sink.load_run(committed)
    assert still is not None and still.status is RunStatus.COMMITTED
    partial_run = sink.load_run(partial)
    assert partial_run is not None and partial_run.status is RunStatus.PARTIAL


def test_stage_over_the_real_store_executes_once_then_reconciles(engine: Engine, sid: int) -> None:
    run_id = uuid4()
    sink = DecisionSink(engine)
    live_anchored(sink, run_id, sid)
    clock = FakeClock()
    broker = FakeBroker(clock)

    def stage() -> ExecutionStage:
        return ExecutionStage(
            config=BASE_CFG, gateway=broker, store=sink,
            market=lambda run: MarketView({sid: "AAA"}, {sid: 100.0}),
            reference=lambda symbol: lambda: (100.0, ReferenceSource.IEX_MID),
            feeds=lambda: dict.fromkeys(BASE_CFG.pipeline.freshness_sla_hours, T0),
            clock=clock, sleep=clock.sleep, executor_options={"poll_attempts": 2},
        )  # fmt: skip

    res = stage().execute_run(run_id)
    assert res.status is RunStatus.EXECUTED and res.advanced
    sent = list(broker.submits)
    assert sent[0] == f"{run_id}-{sid}-lim"
    with engine.connect() as c:
        rows = c.execute(text("SELECT client_order_id, status FROM orders ORDER BY 1")).all()
        status = c.execute(text("SELECT status FROM runs WHERE run_id = :r"), {"r": run_id})
        assert status.scalar_one() == "EXECUTED"
    assert [r[0] for r in rows] == sorted(sent)  # every order is stored, evidence before EXECUTED

    again = stage().execute_run(run_id)  # restart after success
    assert again.status is RunStatus.EXECUTED and not again.advanced
    assert broker.submits == sent


def test_a_halt_persisted_in_postgres_blocks_a_restarted_stage(engine: Engine, sid: int) -> None:
    run_id = uuid4()
    sink = DecisionSink(engine)
    live_anchored(sink, run_id, sid)
    clock = FakeClock()
    broker = FakeBroker(clock, equity=(90_000.0, 100_000.0))  # -10% day

    def stage() -> ExecutionStage:
        return ExecutionStage(
            config=BASE_CFG, gateway=broker, store=sink,
            market=lambda run: MarketView({sid: "AAA"}, {sid: 100.0}),
            reference=lambda symbol: lambda: (100.0, ReferenceSource.IEX_MID),
            feeds=lambda: {k: T0 for k in BASE_CFG.pipeline.freshness_sla_hours},
            clock=clock, sleep=clock.sleep, executor_options={"poll_attempts": 2},
        )  # fmt: skip

    assert stage().execute_run(run_id).status is RunStatus.PARTIAL
    broker.equity = (100_000.0, 100_000.0)  # the account "recovers"; the halt must not clear itself
    assert stage().execute_run(run_id).status is RunStatus.PARTIAL
    assert broker.submits == []
    (ev,) = sink.load_kill_switch_events(run_id)
    assert ev.trigger is KillTrigger.DAILY_LOSS
    with engine.connect() as c:  # halting never erased the commitment or the anchor
        for table in ("decision_commitments", "commitment_anchors"):
            assert c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 1


def test_open_orders_are_found_until_the_broker_state_is_recorded_as_terminal(
    engine: Engine, sid: int
) -> None:
    from contracts.enums import BrokerOrderStatus, OrderKind, OrderSide
    from contracts.models import ExecutionRecord

    run_id = uuid4()
    sink = DecisionSink(engine)
    live_anchored(sink, run_id, sid)
    working = ExecutionRecord(
        run_id=run_id, security_id=sid, client_order_id=f"{run_id}-{sid}-lim", broker_order_id="B1",
        kind=OrderKind.LIMIT, side=OrderSide.BUY, qty=10.0, filled_qty=0.0, limit_price=100.25,
        decision_price=100.0, reference_price=100.0, reference_source=ReferenceSource.IEX_MID,
        status=BrokerOrderStatus.OPEN, submitted_at=AS_OF,
    )  # fmt: skip
    sink.record_executions([working])
    assert sink.run_ids_with_open_orders() == [run_id]
    assert [r.broker_order_id for r in sink.load_open_executions(run_id)] == ["B1"]
    assert sink.load_open_executions(uuid4()) == []

    sink.record_executions([working.model_copy(update={"status": BrokerOrderStatus.CANCELED})])
    assert sink.run_ids_with_open_orders() == [] and sink.load_open_executions(run_id) == []
