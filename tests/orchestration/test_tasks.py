"""Celery task logic: retry policy, idempotent delivery, the execution window and the sweeper.

The task bodies are plain functions over a ``Runtime``, so they are tested directly; a couple of
tests also drive the registered Celery tasks eagerly. Postgres-backed tests (run lock, sweeper
over real rows) are service-gated like the rest of the store tests.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import redis.exceptions
from celery.exceptions import Retry
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

from config.loader import UsageUnavailableError
from contracts.commitment import CommitmentIntegrityError
from contracts.enums import RunMode, RunStatus
from contracts.errors import RunHaltedError
from contracts.models import DlqRecord, RunRecord
from evaluation.anchoring import AnchorBindingError, AnchorUnavailableError
from execution.alpaca import LiveTradingError
from execution.executor import CancelNotConfirmedError, OverfillError
from execution.gateway import BrokerError, DuplicateClientOrderError
from orchestration import tasks
from orchestration.anchor_stage import AnchorNotEligibleError, AnchorResult, UpgradeSummary
from orchestration.execution_stage import ExecutionNotEligibleError, ExecutionPlanError
from orchestration.pipeline import LookAheadError, RunMismatchError, StepResult
from orchestration.sink import DecisionSink
from risk.kill_switch import EquityEvidenceError
from tests.orchestration.test_schedule import Calendar, et
from tests.orchestration.test_sink import run_record

AS_OF = et(2024, 3, 1, 16, 0)  # a Friday close
MON_OPEN = et(2024, 3, 4, 10, 0)
NOW = et(2024, 3, 4, 10, 30)  # inside Monday's execution window
CONFIG = "c" * 64


# --- retry policy --------------------------------------------------------------------------------


def _status(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://x.example")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))


TRANSIENT: list[BaseException] = [
    httpx.ConnectError("down"),
    httpx.ReadTimeout("slow"),
    _status(503),
    _status(429),
    OperationalError("SELECT 1", {}, Exception("connection lost")),
    AnchorUnavailableError("calendar down"),
    CancelNotConfirmedError("still pending"),
    redis.exceptions.ConnectionError("redis down"),
    BrokerError("GET /v2/orders: timeout"),
]
FATAL: list[BaseException] = [
    CommitmentIntegrityError("mismatch"),
    AnchorBindingError("bad proof"),
    AnchorNotEligibleError("not committed"),
    ExecutionNotEligibleError("not anchored"),
    ExecutionPlanError("no price"),
    LiveTradingError("live url"),
    LookAheadError("future row"),
    RunMismatchError("other run"),
    RunHaltedError("halted"),
    OverfillError("overfilled"),
    DuplicateClientOrderError("dup"),
    UsageUnavailableError("no usage"),
    EquityEvidenceError("no history"),
    _status(404),
    _status(400),
    ValueError("bug"),
    KeyError("bug"),
    IntegrityError("INSERT", {}, Exception("fk")),
]


@pytest.mark.parametrize("exc", TRANSIENT, ids=lambda e: type(e).__name__)
def test_transient_failures_are_retried(exc: BaseException) -> None:
    assert tasks.is_transient(exc)


@pytest.mark.parametrize("exc", FATAL, ids=lambda e: type(e).__name__)
def test_everything_else_is_never_retried_above_all_a_commitment_mismatch(
    exc: BaseException,
) -> None:
    assert not tasks.is_transient(exc)


class FakeTask:
    """The parts of a bound Celery task ``with_retry`` uses."""

    name = "orchestration.test"

    def __init__(self, retries: int = 0) -> None:
        self.request = SimpleNamespace(retries=retries)
        self.retried: list[tuple[Exception, float]] = []

    def retry(self, *, exc: Exception, countdown: float) -> Retry:
        self.retried.append((exc, countdown))
        return Retry("retry", exc=exc)


def _boom(exc: BaseException) -> Any:
    def call() -> dict[str, Any]:
        raise exc

    return call


def test_a_transient_failure_asks_celery_to_retry_with_backoff() -> None:
    task = FakeTask(retries=1)
    with pytest.raises(Retry):
        tasks.with_retry(task, _boom(httpx.ConnectError("down")))  # type: ignore[arg-type]
    (exc, countdown) = task.retried[0]
    assert isinstance(exc, httpx.ConnectError)
    assert tasks.RETRY_BASE_SECONDS * 2 <= countdown <= tasks.RETRY_BASE_SECONDS * 2 + 10


def test_retries_are_capped_and_then_the_real_error_surfaces() -> None:
    task = FakeTask(retries=tasks.MAX_RETRIES)
    with pytest.raises(httpx.ConnectError):
        tasks.with_retry(task, _boom(httpx.ConnectError("down")))  # type: ignore[arg-type]
    assert task.retried == []


@pytest.mark.parametrize("exc", FATAL, ids=lambda e: type(e).__name__)
def test_a_fatal_failure_is_raised_as_is_without_asking_for_a_retry(exc: BaseException) -> None:
    task = FakeTask()
    with pytest.raises(type(exc)):
        tasks.with_retry(task, _boom(exc))  # type: ignore[arg-type]
    assert task.retried == []


def test_backoff_grows_and_is_capped() -> None:
    no_jitter = lambda: 0.0  # noqa: E731
    assert [tasks.backoff_seconds(n, jitter=no_jitter) for n in range(3)] == [30, 60, 120]
    assert tasks.backoff_seconds(20, jitter=no_jitter) == tasks.RETRY_MAX_SECONDS


# --- fakes for the logic tests --------------------------------------------------------------------


class MemStore:
    def __init__(self) -> None:
        self.runs: dict[UUID, RunRecord] = {}
        self.dlq: list[DlqRecord] = []
        self.open_orders: list[UUID] = []
        self.held: set[tuple[UUID, str]] = set()
        self.claims = 0
        self.halted_as_of: set[datetime] = set()

    def claim_run(
        self,
        *,
        mode: RunMode,
        as_of: datetime,
        config_hash: str,
        run_id: UUID,
        started_at: datetime,
        fresh: bool = False,
    ) -> tuple[RunRecord, bool]:
        self.claims += 1
        for r in self.runs.values():
            if (r.mode, r.as_of, r.config_hash) == (mode, as_of, config_hash):
                return r, False
        if as_of in self.halted_as_of and not fresh:
            raise RunHaltedError("halted")
        run = RunRecord(
            run_id=run_id,
            mode=mode,
            as_of=as_of,
            config_hash=config_hash,
            status=RunStatus.PENDING,
            started_at=started_at,
        )
        self.runs[run_id] = run
        return run, True

    def load_run(self, run_id: UUID) -> RunRecord | None:
        return self.runs.get(run_id)

    def list_runs(self, *, mode: RunMode, statuses: Any) -> list[RunRecord]:
        return [r for r in self.runs.values() if r.mode is mode and r.status in statuses]

    def run_ids_with_open_orders(self) -> list[UUID]:
        return list(self.open_orders)

    def record_dlq(self, records: tuple[DlqRecord, ...]) -> None:
        self.dlq.extend(r for r in records if r not in self.dlq)

    def anchors_awaiting_confirmation(self) -> list[Any]:
        return []

    def load_commitment_material(self, run_id: UUID) -> Any:
        return None

    def load_kill_switch_events(self, run_id: UUID) -> list[Any]:
        return []

    def load_open_executions(self, run_id: UUID) -> list[Any]:
        return []

    def reset_run(self, run_id: UUID, *, actor: str, reason: str, now: datetime) -> dict[str, int]:
        return {}

    @contextmanager
    def run_lock(self, run_id: UUID, stage: str) -> Iterator[bool]:
        key = (run_id, stage)
        if key in self.held:
            yield False
            return
        self.held.add(key)
        try:
            yield True
        finally:
            self.held.discard(key)


class Pipeline:
    def __init__(self, store: MemStore) -> None:
        self.store, self.calls = store, []  # type: ignore[var-annotated]

    def run_step(self, as_of: datetime, *, run_id: UUID | None = None) -> StepResult:
        assert run_id is not None
        self.calls.append(run_id)
        run = self.store.runs[run_id]
        self.store.runs[run_id] = run.model_copy(update={"status": RunStatus.ANCHORED})
        return StepResult(run_id, as_of, RunStatus.ANCHORED, None, 0.0)


class Anchoring:
    def __init__(self, store: MemStore) -> None:
        self.store, self.calls = store, []  # type: ignore[var-annotated]

    def anchor_run(self, run_id: UUID) -> AnchorResult:
        self.calls.append(run_id)
        self.store.runs[run_id] = self.store.runs[run_id].model_copy(
            update={"status": RunStatus.ANCHORED}
        )
        return AnchorResult(run_id, RunStatus.ANCHORED, True)


class Execution:
    def __init__(self, store: MemStore) -> None:
        self.store = store
        self.executed: list[UUID] = []
        self.reconciled: list[UUID] = []
        self.halted: list[tuple[UUID, bool]] = []

    def execute_run(self, run_id: UUID) -> Any:
        self.executed.append(run_id)
        run = self.store.runs[run_id]
        if run.status is RunStatus.ANCHORED:
            self.store.runs[run_id] = run.model_copy(update={"status": RunStatus.EXECUTED})
        return SimpleNamespace(status=self.store.runs[run_id].status, advanced=True)

    def reconcile_orders(self, run_id: UUID) -> int:
        self.reconciled.append(run_id)
        return 0

    def manual_halt(self, run_id: UUID, *, flatten: bool = True) -> Any:
        self.halted.append((run_id, flatten))
        run = self.store.runs[run_id]
        halted = run.model_copy(
            update={"status": RunStatus.PARTIAL, "status_reason": "kill_switch:manual"}
        )
        self.store.runs[run_id] = halted
        return SimpleNamespace(status=halted.status, reason=halted.status_reason)


class Upgrader:
    def upgrade_all(self) -> UpgradeSummary:
        return UpgradeSummary(checked=2, upgraded=1)


def make_runtime(
    now: datetime = NOW,
) -> tuple[tasks.Runtime, MemStore, Pipeline, Anchoring, Execution]:
    store = MemStore()
    pipeline, anchoring, execution = Pipeline(store), Anchoring(store), Execution(store)
    rt = tasks.Runtime(
        sink=store,
        pipeline=pipeline,
        anchoring=anchoring,
        execution=execution,
        upgrader=Upgrader(),
        calendar=Calendar(),
        config_hash=CONFIG,
        clock=lambda: now,
        new_run_id=uuid4,
    )
    return rt, store, pipeline, anchoring, execution


def add_run(
    store: MemStore,
    status: RunStatus,
    *,
    mode: RunMode = RunMode.LIVE,
    as_of: datetime = AS_OF,
    started_at: datetime = AS_OF,
    reason: str | None = None,
) -> UUID:
    run = RunRecord(
        run_id=uuid4(),
        mode=mode,
        as_of=as_of,
        config_hash=CONFIG,
        status=status,
        started_at=started_at,
        status_reason=reason,
    )
    store.runs[run.run_id] = run
    return run.run_id


# --- weekly pipeline and duplicate delivery -------------------------------------------------------


def test_the_weekly_task_runs_only_at_a_week_final_close_and_only_once_per_week() -> None:
    rt, store, pipeline, _, execution = make_runtime(now=et(2024, 3, 5, 17, 15))  # Tuesday
    assert tasks.weekly_run(rt) == {"skipped": "not a week-final close"}
    assert store.claims == 0 and pipeline.calls == []

    first = tasks.weekly_run(rt, now=et(2024, 3, 1, 17, 15))  # Friday evening
    assert first["created"] is True and first["status"] == "ANCHORED"
    again = tasks.weekly_run(rt, now=et(2024, 3, 1, 17, 20))  # a duplicate beat or delivery
    assert again["created"] is False and len(store.runs) == 1
    assert len(pipeline.calls) == 1  # the run was already ANCHORED: nothing was redone
    assert execution.executed == []  # Friday never trades


def test_a_halted_week_is_not_silently_restarted_but_a_fresh_run_can_be_asked_for() -> None:
    rt, store, pipeline, _, _ = make_runtime()
    store.halted_as_of.add(AS_OF)
    out = tasks.weekly_run(rt, now=et(2024, 3, 1, 17, 15))
    assert out["skipped"] == "halted" and pipeline.calls == []
    fresh = tasks.weekly_run(rt, now=et(2024, 3, 1, 17, 15), fresh=True)
    assert fresh["created"] is True and len(pipeline.calls) == 1  # recovery = a fresh run_id


def test_a_second_delivery_while_the_first_holds_the_lock_does_nothing() -> None:
    rt, store, pipeline, anchoring, _ = make_runtime()
    rid = add_run(store, RunStatus.COMMITTED)
    store.held.add((rid, "advance"))
    assert tasks.advance_run(rt, rid)["skipped"] == "locked"
    assert pipeline.calls == [] and anchoring.calls == []


@pytest.mark.parametrize(
    ("status", "pipeline_calls", "anchor_calls"),
    [
        (RunStatus.PENDING, 1, 0),
        (RunStatus.AGENTS_OK, 1, 0),
        (RunStatus.COMMITTED, 0, 1),
        (RunStatus.ANCHORED, 0, 0),
        (RunStatus.EXECUTED, 0, 0),
        (RunStatus.PARTIAL, 0, 0),
        (RunStatus.FAILED, 0, 0),
    ],
)
def test_advance_runs_only_the_stage_the_run_is_waiting_for(
    status: RunStatus, pipeline_calls: int, anchor_calls: int
) -> None:
    rt, store, pipeline, anchoring, _ = make_runtime()
    rid = add_run(store, status)
    tasks.advance_run(rt, rid)
    assert (len(pipeline.calls), len(anchoring.calls)) == (pipeline_calls, anchor_calls)


def test_advance_never_touches_non_live_runs() -> None:
    rt, store, pipeline, anchoring, _ = make_runtime()
    rid = add_run(store, RunStatus.COMMITTED, mode=RunMode.BACKTEST)
    assert tasks.advance_run(rt, rid)["skipped"] == "not a live run"
    assert pipeline.calls == [] and anchoring.calls == []


# --- the execution window -------------------------------------------------------------------------


def test_execution_waits_for_the_window_and_never_trades_a_stale_plan() -> None:
    rt, store, _, _, execution = make_runtime()
    rid = add_run(store, RunStatus.ANCHORED)
    assert tasks.execute(rt, rid, now=et(2024, 3, 4, 9, 45))["skipped"] == "too early"
    assert tasks.execute(rt, rid, now=et(2024, 3, 2, 12))["skipped"] == "too early"  # the weekend
    assert execution.executed == []

    stale = tasks.execute(rt, rid, now=et(2024, 3, 4, 16, 0))  # the session has ended
    assert stale["skipped"] == "execution window expired" and execution.executed == []
    tasks.execute(rt, rid, now=et(2024, 3, 5, 10, 30))  # and the next day is no better
    assert execution.executed == []
    assert [d.error_type for d in store.dlq] == ["execution_window_expired"]  # evidenced once
    assert store.runs[rid].status is RunStatus.ANCHORED  # left for an operator, not failed


def test_execution_inside_the_window_executes_then_reconciles_and_a_replay_is_safe() -> None:
    rt, store, _, _, execution = make_runtime()
    rid = add_run(store, RunStatus.ANCHORED)
    out = tasks.execute(rt, rid, now=NOW)
    assert out["status"] == "EXECUTED" and execution.executed == [rid] == execution.reconciled
    again = tasks.execute(rt, rid, now=et(2024, 3, 6, 9))  # redelivered after the window
    assert again["status"] == "EXECUTED"  # an EXECUTED run replays through the stage, no window


def test_execution_needs_the_run_lock_and_an_executable_status() -> None:
    rt, store, _, _, execution = make_runtime()
    rid = add_run(store, RunStatus.ANCHORED)
    store.held.add((rid, "execute"))
    assert tasks.execute(rt, rid, now=NOW)["skipped"] == "locked"
    for status in (RunStatus.COMMITTED, RunStatus.FAILED, RunStatus.PARTIAL, RunStatus.AGENTS_OK):
        other = add_run(store, status)
        assert tasks.execute(rt, other, now=NOW)["skipped"].startswith("status")
    assert execution.executed == []


def test_a_halted_run_is_only_settled_and_reconciled_never_traded() -> None:
    rt, store, _, _, execution = make_runtime()
    rid = add_run(store, RunStatus.PARTIAL, reason="kill_switch:manual")
    out = tasks.execute(rt, rid, now=NOW)
    assert out["halted"] is True
    assert execution.executed == [rid] and execution.reconciled == [rid]  # settle, then reconcile


# --- the sweeper: the work queue is rebuilt from Postgres alone -----------------------------------


def _sweep(rt: tasks.Runtime, now: datetime) -> dict[str, list[str]]:
    return tasks.sweep(
        rt, advance=lambda r: None, execute_=lambda r: None, reconcile_=lambda r: None, now=now
    )


def test_the_sweeper_rebuilds_the_queue_from_stored_state() -> None:
    rt, store, *_ = make_runtime()
    fresh_pending = add_run(store, RunStatus.PENDING, started_at=NOW - timedelta(minutes=5))
    stale_pending = add_run(store, RunStatus.AGENTS_OK, started_at=NOW - timedelta(hours=2))
    committed = add_run(store, RunStatus.COMMITTED)
    anchored = add_run(store, RunStatus.ANCHORED)
    for status in (RunStatus.EXECUTED, RunStatus.FAILED, RunStatus.SCORED):
        add_run(store, status)
    halted = add_run(store, RunStatus.PARTIAL, reason="kill_switch:daily_loss")
    add_run(store, RunStatus.COMMITTED, mode=RunMode.BACKTEST)
    store.open_orders = [halted]

    out = _sweep(rt, NOW)
    assert sorted(out["advance"]) == sorted(map(str, [stale_pending, committed]))
    assert str(fresh_pending) not in out["advance"]  # may still be running on another worker
    assert out["execute"] == [str(anchored)] and out["reconcile"] == [str(halted)]


def test_the_sweeper_holds_an_anchored_run_until_its_window_opens_and_drops_it_after() -> None:
    rt, store, *_ = make_runtime()
    anchored = add_run(store, RunStatus.ANCHORED)
    assert _sweep(rt, et(2024, 3, 2, 12))["execute"] == []  # the weekend
    assert _sweep(rt, et(2024, 3, 4, 9, 59))["execute"] == []
    assert _sweep(rt, et(2024, 3, 4, 10, 0))["execute"] == [str(anchored)]
    assert _sweep(rt, et(2024, 3, 4, 16, 0))["execute"] == []  # stale: never enqueued again


def test_a_lost_queue_costs_nothing_because_the_sweeper_re_enqueues_and_tasks_are_idempotent() -> (
    None
):
    rt, store, pipeline, anchoring, _ = make_runtime()
    rid = add_run(store, RunStatus.COMMITTED)
    queued: list[str] = []
    for _ in range(2):  # the broker lost the first message; the sweeper sends it again
        tasks.sweep(
            rt, advance=queued.append, execute_=queued.append, reconcile_=queued.append, now=NOW
        )
    assert queued == [str(rid), str(rid)]
    for message in queued:  # both get delivered: at-least-once
        tasks.advance_run(rt, UUID(message))
    assert len(anchoring.calls) == 1  # the second delivery found the run already ANCHORED
    tasks.execute(rt, rid, now=NOW)
    tasks.execute(rt, rid, now=NOW)  # a duplicate delivery of the execute task
    assert store.runs[rid].status is RunStatus.EXECUTED and pipeline.calls == []


# --- the registered Celery tasks ------------------------------------------------------------------


def test_the_celery_tasks_are_registered_with_late_acks_and_run_eagerly() -> None:
    rt, store, *_ = make_runtime()
    rid = add_run(store, RunStatus.COMMITTED)
    tasks.set_runtime(rt)
    try:
        result = tasks.advance_run_task.apply(args=[str(rid)])
        assert result.get() == {"run_id": str(rid), "status": "ANCHORED"}
        assert tasks.advance_run_task.acks_late is True
        swept = tasks.upgrade_anchors_task.apply().get()
        assert swept == {"checked": 2, "upgraded": 1, "confirmed": 0, "failed": 0}
    finally:
        tasks.set_runtime(None)


def test_a_fatal_error_in_a_task_is_not_retried_by_celery() -> None:
    rt, store, _, anchoring, _ = make_runtime()
    rid = add_run(store, RunStatus.COMMITTED)

    def boom(run_id: UUID) -> AnchorResult:
        anchoring.calls.append(run_id)
        raise CommitmentIntegrityError("mismatch")

    rt.anchoring.anchor_run = boom  # type: ignore[method-assign]
    tasks.set_runtime(rt)
    try:
        result = tasks.advance_run_task.apply(args=[str(rid)])
        assert isinstance(result.result, CommitmentIntegrityError)
        assert len(anchoring.calls) == 1  # exactly one attempt
    finally:
        tasks.set_runtime(None)


def test_beat_is_scheduled_from_the_trading_calendar_not_fixed_dates() -> None:
    from orchestration.celery_app import app

    schedule = app.conf.beat_schedule
    assert {v["task"] for v in schedule.values()} == {
        "orchestration.run_weekly_pipeline",
        "orchestration.sweep_runs",
        "orchestration.upgrade_anchors",
        "orchestration.ingest_feed",
        "orchestration.sync_calendar",
        "orchestration.ingest_benchmarks",
        "orchestration.sync_dgs3mo",
        "orchestration.capture_due_references",
        "orchestration.sweep_halt_references",
        "orchestration.sync_corporate_actions",
        "orchestration.poll_listing_status",
        "orchestration.derive_delistings",
    }
    # P6.3 reference jobs are interval polls, never a fixed weekday/clock cron (D0 comes from the
    # stored calendar), and nothing dispatches from the halt path.
    for name, entry in schedule.items():
        if name.startswith("reference-"):
            assert isinstance(entry["schedule"], timedelta), name
    assert app.conf.task_acks_late is True and str(app.conf.timezone) == "America/New_York"
    assert app.conf.broker_transport_options["visibility_timeout"] >= 6 * 3600


# --- Postgres: the run lock and the sweeper over real rows ----------------------------------------


@pytest.fixture
def engine(pg_engine: Engine) -> Iterator[Engine]:
    def wipe() -> None:
        with pg_engine.begin() as c:
            c.execute(text("TRUNCATE runs, securities RESTART IDENTITY CASCADE"))

    wipe()
    yield pg_engine
    wipe()


def test_the_run_lock_is_exclusive_across_connections_and_released_afterwards(
    engine: Engine,
) -> None:
    sink = DecisionSink(engine)
    rid = uuid4()
    with sink.run_lock(rid, "execute") as first:
        assert first is True
        results: list[bool] = []

        def other() -> None:
            with sink.run_lock(rid, "execute") as got:
                results.append(got)
            with sink.run_lock(rid, "advance") as other_stage:  # another stage is independent
                results.append(other_stage)

        t = threading.Thread(target=other)
        t.start()
        t.join()
        assert results == [False, True]
    with sink.run_lock(rid, "execute") as again:  # released when the block ended
        assert again is True


def test_the_sweeper_reads_real_rows_and_a_lost_lock_holder_frees_its_run(engine: Engine) -> None:
    sink = DecisionSink(engine)
    from tests.orchestration.test_sink import step

    with engine.begin() as c:
        from tests.store import factories as f

        sid = f.security(c)
    live_committed = uuid4()
    run = run_record(live_committed, RunStatus.COMMITTED).model_copy(
        update={"mode": RunMode.LIVE, "ended_at": AS_OF, "started_at": AS_OF}
    )
    sink.flush_step(step(live_committed, sid, run=run, kill_switch=()))
    rt, *_ = make_runtime()
    rt.sink = sink  # the real store; everything else stays fake
    queued: list[str] = []
    out = tasks.sweep(
        rt, advance=queued.append, execute_=queued.append, reconcile_=queued.append, now=NOW
    )
    assert out["advance"] == [str(live_committed)] == queued  # found in Postgres, not in Redis
