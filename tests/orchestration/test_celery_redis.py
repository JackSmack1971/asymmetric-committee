"""Delivery through a real Redis broker: tasks are routed, run once per delivery and acked late."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import redis
from celery.contrib.testing.worker import start_worker

from contracts.enums import RunStatus
from orchestration import tasks
from orchestration.celery_app import app
from tests.orchestration.test_tasks import add_run, make_runtime


@pytest.fixture
def worker(redis_url: str) -> Iterator[None]:
    previous = (app.conf.broker_url, app.conf.result_backend)
    app.conf.update(broker_url=redis_url, result_backend=redis_url, task_always_eager=False)
    redis.Redis.from_url(redis_url).flushdb()  # a private DB: no stale messages from other runs
    with start_worker(app, pool="solo", perform_ping_check=False, shutdown_timeout=30):
        yield
    app.conf.update(broker_url=previous[0], result_backend=previous[1])


def test_a_delivered_task_runs_the_stage_and_a_second_delivery_is_harmless(worker: None) -> None:
    rt, store, pipeline, anchoring, _ = make_runtime()
    rid = add_run(store, RunStatus.COMMITTED)
    tasks.set_runtime(rt)  # the in-process worker thread shares this runtime
    try:
        first = tasks.advance_run_task.apply_async(args=[str(rid)]).get(timeout=30)
        second = tasks.advance_run_task.apply_async(args=[str(rid)]).get(timeout=30)  # redelivery
    finally:
        tasks.set_runtime(None)
    assert first == {"run_id": str(rid), "status": "ANCHORED"}
    assert second == {"run_id": str(rid), "status": "ANCHORED"}
    assert len(anchoring.calls) == 1 and pipeline.calls == []  # the second delivery did no work
    assert tasks.advance_run_task.acks_late is True


def test_a_fatal_error_is_reported_once_through_the_broker_and_not_retried(worker: None) -> None:
    from contracts.commitment import CommitmentIntegrityError

    rt, store, _, anchoring, _ = make_runtime()
    rid = add_run(store, RunStatus.COMMITTED)

    def boom(run_id: object) -> object:
        anchoring.calls.append(run_id)
        raise CommitmentIntegrityError("mismatch")

    rt.anchoring.anchor_run = boom  # type: ignore[method-assign,assignment]
    tasks.set_runtime(rt)
    try:
        result = tasks.advance_run_task.apply_async(args=[str(rid)])
        with pytest.raises(CommitmentIntegrityError):
            result.get(timeout=30)
    finally:
        tasks.set_runtime(None)
    assert len(anchoring.calls) == 1
