"""Celery tasks (§11): thin stage wrappers over Postgres-backed logic.

Where the truth lives. Postgres (run status, verdicts, commitments, anchors, orders, halt events)
and the broker (looked up by our stable client order ids) are the business truth. Redis is only
the message transport, the LLM cache and the rate limiter: losing it loses no state, because
``sweep_runs`` rebuilds the work queue from Postgres. Nothing here uses a Redis lock for
correctness; mutual exclusion is a Postgres advisory lock (``DecisionSink.run_lock``) and every
state move is a conditional update.

At-least-once delivery is harmless. Tasks are acked late, so a worker crash redelivers them, and
each stage is safe to run twice: agent work resumes from stored verdicts, anchoring reuses the
manifest on the remote, execution finds its orders by client id and never resends, and a second
concurrent delivery finds the run lock taken and does nothing.

Retries. Only transient infrastructure failures are retried (``is_transient``). Everything else,
above all a commitment mismatch, is raised as is: no automatic retry, an operator looks.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx
import redis.exceptions
from celery import Task
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from contracts.commitment import CommitmentIntegrityError, CommitmentMaterial
from contracts.enums import HALT_REASON_PREFIX, FeedName, RunMode, RunStatus
from contracts.errors import ImmutableConflictError, RunHaltedError
from contracts.models import (
    CommitmentAnchor,
    DlqRecord,
    ExecutionRecord,
    KillSwitchEvent,
    RunRecord,
)
from evaluation.anchoring import AnchorBindingError, AnchorUnavailableError
from execution.executor import CancelNotConfirmedError, OverfillError
from execution.gateway import BrokerError, DuplicateClientOrderError
from ingest.scheduled import FeedRunResult, IngestFailedError
from orchestration.anchor_stage import UpgradeSummary
from orchestration.celery_app import app
from orchestration.pipeline import Anchors, StepResult
from orchestration.schedule import (
    MarketCalendar,
    NoSessionError,
    WindowState,
    execution_window,
    weekly_as_of,
    window_state,
)

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 600
# A pipeline run younger than this may still be in flight on another worker: the sweeper leaves it.
STALE_AFTER = timedelta(minutes=45)

_STARTED = (
    RunStatus.PENDING,
    RunStatus.INGEST_OK,
    RunStatus.FEATURES_OK,
    RunStatus.GATED,
    RunStatus.AGENTS_OK,
)


# --- what the tasks need -------------------------------------------------------------------------


class TaskStore(Protocol):
    def claim_run(
        self,
        *,
        mode: RunMode,
        as_of: datetime,
        config_hash: str,
        run_id: UUID,
        started_at: datetime,
        fresh: bool = False,
    ) -> tuple[RunRecord, bool]: ...

    def load_run(self, run_id: UUID) -> RunRecord | None: ...

    def list_runs(self, *, mode: RunMode, statuses: Sequence[RunStatus]) -> list[RunRecord]: ...

    def run_ids_with_open_orders(self) -> list[UUID]: ...

    def record_dlq(self, records: tuple[DlqRecord, ...]) -> None: ...

    def anchors_awaiting_confirmation(self) -> list[CommitmentAnchor]: ...

    def load_commitment_material(self, run_id: UUID) -> CommitmentMaterial | None: ...

    def load_kill_switch_events(self, run_id: UUID) -> list[KillSwitchEvent]: ...

    def load_open_executions(self, run_id: UUID) -> list[ExecutionRecord]: ...

    def reset_run(
        self, run_id: UUID, *, actor: str, reason: str, now: datetime
    ) -> dict[str, int]: ...

    def run_lock(self, run_id: UUID, stage: str) -> Any: ...  # context manager yielding bool


class Steps(Protocol):
    def run_step(self, as_of: datetime, *, run_id: UUID | None = None) -> StepResult: ...


class Executes(Protocol):
    def execute_run(self, run_id: UUID) -> Any: ...

    def reconcile_orders(self, run_id: UUID) -> int: ...

    def manual_halt(self, run_id: UUID, *, flatten: bool = True) -> Any: ...


class Upgrades(Protocol):
    def upgrade_all(self) -> UpgradeSummary: ...


class Ingests(Protocol):
    def run_feed(self, feed: FeedName, *, now: datetime) -> FeedRunResult: ...


class ReferenceIngests(Protocol):
    """P6.3 acquisition: calendar + coverage, benchmark bars, DGS3MO vintages."""

    def run_calendar(self, now: datetime) -> int: ...

    def run_benchmarks(self, now: datetime) -> int: ...

    def run_dgs3mo(self, now: datetime) -> int: ...


class JobOutcome(Protocol):
    written: int
    errors: list[str]


class CorporateActionJobs(Protocol):
    """P6.4: action polls with coverage, listing-status polls + Form 25, delisting derivation."""

    def run_corporate_actions(self, now: datetime) -> JobOutcome: ...

    def run_listing_status(self, now: datetime) -> JobOutcome: ...

    def run_delistings(self, now: datetime) -> JobOutcome: ...


class References(Protocol):
    """P6.3 run-scoped references: live capture polling, backtest builder, halt sweeper."""

    def capture_due(self, now: datetime, *, on_error: Callable[[UUID, Exception], None]) -> int: ...

    def build_backtest(self, run_id: UUID, now: datetime) -> Any: ...

    def sweep_halt_requests(
        self, now: datetime, *, on_error: Callable[[UUID, Exception], None]
    ) -> int: ...


class ReferenceJobFailedError(RuntimeError):
    """Some runs failed in a reference poll; the others were still processed."""

    def __init__(self, job: str, errors: list[str]) -> None:
        super().__init__(f"{job}: {len(errors)} failure(s): " + "; ".join(errors[:3]))
        self.errors = errors


@dataclass
class Runtime:
    """Everything the tasks use, built once per worker process (``orchestration/bootstrap.py``)."""

    sink: TaskStore
    pipeline: Steps  # a LIVE orchestrator with anchoring and *no* execution: it stops at ANCHORED
    anchoring: Anchors
    execution: Executes
    upgrader: Upgrades
    calendar: MarketCalendar
    config_hash: str
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    new_run_id: Callable[[], UUID] = uuid4
    ingest: Ingests | None = None
    reference_data: ReferenceIngests | None = None
    references: References | None = None
    corporate_actions: CorporateActionJobs | None = None


_runtime: Runtime | None = None
_factory: Callable[[], Runtime] | None = None


def set_runtime(rt: Runtime | None) -> None:
    global _runtime
    _runtime = rt


def set_runtime_factory(factory: Callable[[], Runtime] | None) -> None:
    global _factory, _runtime
    _factory, _runtime = factory, None


def runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        if _factory is None:
            # Deferred: bootstrap imports this module. Building the real runtime runs every
            # startup gate first, so an unconfigured worker fails closed before any side effect.
            from orchestration.bootstrap import build_runtime

            _runtime = build_runtime()
        else:
            _runtime = _factory()
    return _runtime


# --- retry policy --------------------------------------------------------------------------------

_NEVER_RETRIED = (
    ImmutableConflictError,
    CommitmentIntegrityError,
    AnchorBindingError,
    OverfillError,
    DuplicateClientOrderError,
    RunHaltedError,
)


def is_transient(exc: BaseException) -> bool:
    """True only for failures where trying again later can succeed and is safe.

    Safe because every stage is idempotent (see the module docstring). Unknown exceptions are not
    retried: a bug or a broken invariant must surface, not loop. A commitment mismatch, a live
    endpoint, an ineligible run, a look-ahead or budget failure are never in this set.
    """
    if isinstance(exc, _NEVER_RETRIED):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 425, 429) or exc.response.status_code >= 500
    if isinstance(
        exc,
        httpx.TransportError | AnchorUnavailableError | CancelNotConfirmedError | IngestFailedError,
    ):
        return True
    if isinstance(exc, OperationalError | InterfaceError):
        return True
    if isinstance(exc, DBAPIError):
        return bool(exc.connection_invalidated)
    if isinstance(exc, redis.exceptions.ConnectionError | redis.exceptions.TimeoutError):
        return True
    # A plain BrokerError is a network or 5xx failure at the broker; orders are found by client id.
    return type(exc) is BrokerError


def backoff_seconds(retries: int, *, jitter: Callable[[], float] = random.random) -> float:
    return float(min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2**retries) + 10 * jitter())


def with_retry(task: Task[Any, Any], call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return call()
    except Exception as exc:
        if is_transient(exc) and task.request.retries < MAX_RETRIES:
            log.warning("%s: transient %s, retrying", task.name, type(exc).__name__)
            raise task.retry(exc=exc, countdown=backoff_seconds(task.request.retries)) from exc
        raise


# --- the task logic (plain functions over a Runtime, tested directly) -----------------------------


@contextmanager
def _locked(rt: Runtime, run_id: UUID, stage: str) -> Iterator[bool]:
    with rt.sink.run_lock(run_id, stage) as got:
        yield bool(got)


def _is_halted(run: RunRecord) -> bool:
    return run.status is RunStatus.PARTIAL and (run.status_reason or "").startswith(
        HALT_REASON_PREFIX
    )


def ingest_feed(rt: Runtime, feed: FeedName, *, now: datetime | None = None) -> dict[str, Any]:
    """Poll one feed. ``feed_health`` is written by the job itself, on success and on failure; a
    failed or partial poll raises so Celery retries it (idempotent) and it shows as failed."""
    if rt.ingest is None:
        raise RuntimeError("this worker has no ingestion runtime")
    result = rt.ingest.run_feed(feed, now=now or rt.clock())
    if not result.ok:
        raise IngestFailedError(feed, result.errors)
    return {"feed": feed.value, "rows": result.rows}


def _reference_ingest(rt: Runtime) -> ReferenceIngests:
    if rt.reference_data is None:
        raise RuntimeError("this worker has no reference-data runtime")
    return rt.reference_data


def _references(rt: Runtime) -> References:
    if rt.references is None:
        raise RuntimeError("this worker has no reference-capture runtime")
    return rt.references


def sync_calendar(rt: Runtime, *, now: datetime | None = None) -> dict[str, Any]:
    """Fetch the calendar window and its coverage atomically. Replays are exact no-ops."""
    return {"sessions": _reference_ingest(rt).run_calendar(now or rt.clock())}


def ingest_benchmarks(rt: Runtime, *, now: datetime | None = None) -> dict[str, Any]:
    """Raw SIP daily bars for SPY and the sector ETFs, through the existing bar path."""
    return {"rows": _reference_ingest(rt).run_benchmarks(now or rt.clock())}


def sync_dgs3mo(rt: Runtime, *, now: datetime | None = None) -> dict[str, Any]:
    return {"rows": _reference_ingest(rt).run_dgs3mo(now or rt.clock())}


def capture_due_references(rt: Runtime, *, now: datetime | None = None) -> dict[str, Any]:
    """Poll for runs whose D0 open + delay has arrived. Idempotent: written rows are skipped."""
    errors: list[str] = []
    written = _references(rt).capture_due(
        now or rt.clock(), on_error=lambda run_id, exc: errors.append(f"{run_id}: {exc!r}")
    )
    if errors:
        raise ReferenceJobFailedError("capture_due_references", errors)
    return {"written": written}


def sweep_halt_references(rt: Runtime, *, now: datetime | None = None) -> dict[str, Any]:
    """The only consumer of halt-reference requests (nothing dispatches from the halt path)."""
    errors: list[str] = []
    written = _references(rt).sweep_halt_requests(
        now or rt.clock(), on_error=lambda run_id, exc: errors.append(f"{run_id}: {exc!r}")
    )
    if errors:
        raise ReferenceJobFailedError("sweep_halt_references", errors)
    return {"written": written}


def _corporate_actions(rt: Runtime) -> CorporateActionJobs:
    if rt.corporate_actions is None:
        raise RuntimeError("this worker has no corporate-actions runtime")
    return rt.corporate_actions


def _job(name: str, outcome: JobOutcome) -> dict[str, Any]:
    """A failed unit wrote nothing and is retried at the next cadence, not immediately (the error
    is not transient), so a provider outage cannot become a retry storm."""
    if outcome.errors:
        raise ReferenceJobFailedError(name, outcome.errors)
    return {"written": outcome.written}


def sync_corporate_actions(rt: Runtime, *, now: datetime | None = None) -> dict[str, Any]:
    return _job(
        "sync_corporate_actions", _corporate_actions(rt).run_corporate_actions(now or rt.clock())
    )


def poll_listing_status(rt: Runtime, *, now: datetime | None = None) -> dict[str, Any]:
    return _job("poll_listing_status", _corporate_actions(rt).run_listing_status(now or rt.clock()))


def derive_delistings(rt: Runtime, *, now: datetime | None = None) -> dict[str, Any]:
    return _job("derive_delistings", _corporate_actions(rt).run_delistings(now or rt.clock()))


def build_backtest_references(
    rt: Runtime, run_id: UUID, *, now: datetime | None = None
) -> dict[str, Any]:
    with _locked(rt, run_id, "references") as got:
        if not got:
            return {"run_id": str(run_id), "skipped": "locked"}
        result = _references(rt).build_backtest(run_id, now or rt.clock())
        return {"run_id": str(run_id), "state": result.state.value, "written": result.written}


def weekly_run(rt: Runtime, *, now: datetime | None = None, fresh: bool = False) -> dict[str, Any]:
    """Claim (or find) the run for the latest week-final close and advance it to ANCHORED."""
    now = now or rt.clock()
    as_of = weekly_as_of(rt.calendar, now)
    if as_of is None:
        return {"skipped": "not a week-final close"}
    try:
        run, created = rt.sink.claim_run(
            mode=RunMode.LIVE,
            as_of=as_of,
            config_hash=rt.config_hash,
            run_id=rt.new_run_id(),
            started_at=now,
            fresh=fresh,
        )
    except RunHaltedError:
        return {"skipped": "halted", "as_of": as_of.isoformat()}
    out = advance_run(rt, run.run_id)
    return {**out, "created": created}


def advance_run(rt: Runtime, run_id: UUID) -> dict[str, Any]:
    """Move one live run forward: agents -> COMMITTED -> ANCHORED. Never executes."""
    with _locked(rt, run_id, "advance") as got:
        if not got:
            return {"run_id": str(run_id), "skipped": "locked"}
        run = rt.sink.load_run(run_id)
        if run is None or run.mode is not RunMode.LIVE:
            return {"run_id": str(run_id), "skipped": "not a live run"}
        if run.status in _STARTED:
            # The pipeline anchors as soon as it commits (its stages are idempotent and resume
            # from stored verdicts), so one call takes a fresh run all the way to ANCHORED.
            result = rt.pipeline.run_step(run.as_of, run_id=run_id)
            return {"run_id": str(run_id), "status": result.status.value}
        if run.status is RunStatus.COMMITTED:
            anchored = rt.anchoring.anchor_run(run_id)
            return {"run_id": str(run_id), "status": anchored.status.value}
        return {"run_id": str(run_id), "status": run.status.value}


def execute(rt: Runtime, run_id: UUID, *, now: datetime | None = None) -> dict[str, Any]:
    """Send a live run's orders, only inside its execution window (first session after the close,
    from open + 30 min). Outside it nothing is sent; a stale run is left for an operator."""
    now = now or rt.clock()
    run = rt.sink.load_run(run_id)
    if run is None or run.mode is not RunMode.LIVE:
        return {"run_id": str(run_id), "skipped": "not a live run"}
    if _is_halted(run):  # terminal: only make sure nothing is left working, then reconcile
        with _locked(rt, run_id, "execute") as got:
            if not got:
                return {"run_id": str(run_id), "skipped": "locked"}
            rt.execution.execute_run(run_id)
            rt.execution.reconcile_orders(run_id)
        return {"run_id": str(run_id), "status": run.status.value, "halted": True}
    if run.status not in (RunStatus.ANCHORED, RunStatus.EXECUTED):
        return {"run_id": str(run_id), "skipped": f"status {run.status.value}"}
    if run.status is RunStatus.ANCHORED:
        state = window_state(now, execution_window(rt.calendar, run.as_of))
        if state is WindowState.EARLY:
            return {"run_id": str(run_id), "skipped": "too early"}
        if state is WindowState.EXPIRED:
            _note_expired(rt, run)
            return {"run_id": str(run_id), "skipped": "execution window expired"}
    with _locked(rt, run_id, "execute") as got:
        if not got:
            return {"run_id": str(run_id), "skipped": "locked"}
        res = rt.execution.execute_run(run_id)
        rt.execution.reconcile_orders(run_id)
    return {"run_id": str(run_id), "status": res.status.value, "advanced": bool(res.advanced)}


def _note_expired(rt: Runtime, run: RunRecord) -> None:
    """Evidence that an anchored plan went stale unexecuted (deduplicated by content)."""
    rt.sink.record_dlq(
        (
            DlqRecord(
                run_id=run.run_id,
                as_of=run.as_of,
                agent="executor",
                error_type="execution_window_expired",
                payload={"detail": "the first session after the close ended without execution"},
            ),
        )
    )


def reconcile(rt: Runtime, run_id: UUID) -> dict[str, Any]:
    with _locked(rt, run_id, "execute") as got:
        if not got:
            return {"run_id": str(run_id), "skipped": "locked"}
        return {"run_id": str(run_id), "changed": rt.execution.reconcile_orders(run_id)}


def sweep(
    rt: Runtime,
    *,
    advance: Callable[[str], object],
    execute_: Callable[[str], object],
    reconcile_: Callable[[str], object],
    now: datetime | None = None,
) -> dict[str, list[str]]:
    """Rebuild the work queue from Postgres (this is the crash and Redis-loss recovery).

    Enqueues, for LIVE runs only: stale unfinished pipeline runs and committed runs (advance),
    anchored runs whose execution window is open (execute), and every run whose stored orders
    still look working (reconcile). Each task is idempotent, so enqueuing one that is already
    queued or running is harmless.
    """
    now = now or rt.clock()
    out: dict[str, list[str]] = {"advance": [], "execute": [], "reconcile": []}
    active = [*_STARTED, RunStatus.COMMITTED, RunStatus.ANCHORED]
    for run in rt.sink.list_runs(mode=RunMode.LIVE, statuses=active):
        rid = str(run.run_id)
        if run.status in _STARTED:
            if now - run.started_at >= STALE_AFTER:
                advance(rid)
                out["advance"].append(rid)
        elif run.status is RunStatus.COMMITTED:
            advance(rid)
            out["advance"].append(rid)
        else:
            try:
                state = window_state(now, execution_window(rt.calendar, run.as_of))
            except NoSessionError:
                continue
            if state is WindowState.OPEN:
                execute_(rid)
                out["execute"].append(rid)
    for run_id in rt.sink.run_ids_with_open_orders():
        reconcile_(str(run_id))
        out["reconcile"].append(str(run_id))
    return out


# --- Celery entry points --------------------------------------------------------------------------

_COMMON: dict[str, Any] = {"bind": True, "acks_late": True, "ignore_result": False}


@app.task(name="orchestration.run_weekly_pipeline", **_COMMON)
def run_weekly_pipeline(self: Task[Any, Any], fresh: bool = False) -> dict[str, Any]:
    return with_retry(self, lambda: weekly_run(runtime(), fresh=fresh))


@app.task(name="orchestration.ingest_feed", **_COMMON)
def ingest_feed_task(self: Task[Any, Any], feed: str) -> dict[str, Any]:
    return with_retry(self, lambda: ingest_feed(runtime(), FeedName(feed)))


@app.task(name="orchestration.sync_calendar", **_COMMON)
def sync_calendar_task(self: Task[Any, Any]) -> dict[str, Any]:
    return with_retry(self, lambda: sync_calendar(runtime()))


@app.task(name="orchestration.ingest_benchmarks", **_COMMON)
def ingest_benchmarks_task(self: Task[Any, Any]) -> dict[str, Any]:
    return with_retry(self, lambda: ingest_benchmarks(runtime()))


@app.task(name="orchestration.sync_dgs3mo", **_COMMON)
def sync_dgs3mo_task(self: Task[Any, Any]) -> dict[str, Any]:
    return with_retry(self, lambda: sync_dgs3mo(runtime()))


@app.task(name="orchestration.capture_due_references", **_COMMON)
def capture_due_references_task(self: Task[Any, Any]) -> dict[str, Any]:
    return with_retry(self, lambda: capture_due_references(runtime()))


@app.task(name="orchestration.sweep_halt_references", **_COMMON)
def sweep_halt_references_task(self: Task[Any, Any]) -> dict[str, Any]:
    return with_retry(self, lambda: sweep_halt_references(runtime()))


@app.task(name="orchestration.sync_corporate_actions", **_COMMON)
def sync_corporate_actions_task(self: Task[Any, Any]) -> dict[str, Any]:
    return with_retry(self, lambda: sync_corporate_actions(runtime()))


@app.task(name="orchestration.poll_listing_status", **_COMMON)
def poll_listing_status_task(self: Task[Any, Any]) -> dict[str, Any]:
    return with_retry(self, lambda: poll_listing_status(runtime()))


@app.task(name="orchestration.derive_delistings", **_COMMON)
def derive_delistings_task(self: Task[Any, Any]) -> dict[str, Any]:
    return with_retry(self, lambda: derive_delistings(runtime()))


@app.task(name="orchestration.build_backtest_references", **_COMMON)
def build_backtest_references_task(self: Task[Any, Any], run_id: str) -> dict[str, Any]:
    return with_retry(self, lambda: build_backtest_references(runtime(), UUID(run_id)))


@app.task(name="orchestration.advance_run", **_COMMON)
def advance_run_task(self: Task[Any, Any], run_id: str) -> dict[str, Any]:
    return with_retry(self, lambda: advance_run(runtime(), UUID(run_id)))


@app.task(name="orchestration.execute_run", **_COMMON)
def execute_run_task(self: Task[Any, Any], run_id: str) -> dict[str, Any]:
    return with_retry(self, lambda: execute(runtime(), UUID(run_id)))


@app.task(name="orchestration.reconcile_orders", **_COMMON)
def reconcile_orders_task(self: Task[Any, Any], run_id: str) -> dict[str, Any]:
    return with_retry(self, lambda: reconcile(runtime(), UUID(run_id)))


@app.task(name="orchestration.upgrade_anchors", **_COMMON)
def upgrade_anchors_task(self: Task[Any, Any]) -> dict[str, Any]:
    def call() -> dict[str, Any]:
        s = runtime().upgrader.upgrade_all()
        return {
            "checked": s.checked,
            "upgraded": s.upgraded,
            "confirmed": s.confirmed,
            "failed": s.failed,
        }

    return with_retry(self, call)


@app.task(name="orchestration.sweep_runs", **_COMMON)
def sweep_runs_task(self: Task[Any, Any]) -> dict[str, Any]:
    def call() -> dict[str, Any]:
        found = sweep(
            runtime(),
            advance=lambda r: advance_run_task.delay(r),
            execute_=lambda r: execute_run_task.delay(r),
            reconcile_=lambda r: reconcile_orders_task.delay(r),
        )
        return {k: v for k, v in found.items()}

    return with_retry(self, call)
