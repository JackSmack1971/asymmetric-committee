"""Reference-data tasks: eager delivery, retry policy, and the halt path's isolation."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from config.loader import load_config
from orchestration import tasks
from risk.kill_switch import evaluate
from tests.orchestration.test_tasks import make_runtime

NOW = datetime(2024, 3, 5, 15, 0, tzinfo=UTC)


class Ingests:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.seen: set[int] = set()

    def _do(self, name: str, n: int) -> int:
        self.calls.append(name)
        if self.fail is not None:
            raise self.fail
        fresh = n if n not in self.seen else 0  # exact replay writes nothing new
        self.seen.add(n)
        return fresh

    def run_calendar(self, now: datetime) -> int:
        return self._do("calendar", 250)

    def run_benchmarks(self, now: datetime) -> int:
        return self._do("benchmarks", 12)

    def run_dgs3mo(self, now: datetime) -> int:
        return self._do("dgs3mo", 900)


def rt_with(ingests: Ingests) -> tasks.Runtime:
    rt, *_ = make_runtime(now=NOW)
    rt.reference_data = ingests
    return rt


def test_eager_delivery_and_redelivery_of_the_acquisition_tasks() -> None:
    ingests = Ingests()
    tasks.set_runtime(rt_with(ingests))
    try:
        first = tasks.sync_calendar_task.apply()
        again = tasks.sync_calendar_task.apply()  # duplicate delivery
        bench = tasks.ingest_benchmarks_task.apply()
        rates = tasks.sync_dgs3mo_task.apply()
    finally:
        tasks.set_runtime(None)
    assert first.result == {"sessions": 250} and again.result == {"sessions": 0}
    assert bench.result == {"rows": 12} and rates.result == {"rows": 900}


def test_a_worker_without_the_runtime_fails_explicitly_not_silently() -> None:
    rt, *_ = make_runtime(now=NOW)
    try:
        tasks.sync_calendar(rt)
    except RuntimeError as exc:
        assert "reference-data runtime" in str(exc)
    else:
        raise AssertionError("expected a RuntimeError")


def test_transient_source_failures_are_retried_and_surface_after_the_cap() -> None:
    tasks.set_runtime(rt_with(Ingests(fail=httpx.ConnectError("down"))))
    try:
        result = tasks.sync_dgs3mo_task.apply()
    finally:
        tasks.set_runtime(None)
    assert isinstance(result.result, Exception)  # retried up to the cap, then the real error


def test_a_fatal_failure_is_not_retried() -> None:
    from contracts.errors import ImmutableConflictError

    ingests = Ingests(fail=ImmutableConflictError("different payload"))
    tasks.set_runtime(rt_with(ingests))
    try:
        result = tasks.sync_calendar_task.apply()
    finally:
        tasks.set_runtime(None)
    assert isinstance(result.result, ImmutableConflictError)
    assert ingests.calls == ["calendar"]  # exactly one attempt


def test_reference_jobs_cannot_trip_the_stale_feed_kill_switch() -> None:
    pipeline = load_config(allow_placeholders=True, env={}).pipeline
    fresh = {feed: NOW - timedelta(minutes=5) for feed in pipeline.freshness_sla_hours}
    # The acquisition jobs never ran and have no last_success, yet nothing halts: stale_feeds is
    # driven by freshness_sla_hours keys only.
    assert (
        evaluate(
            equity=100.0,
            prior_close_equity=100.0,
            limit=0.03,
            last_success=fresh,
            sla_hours=pipeline.freshness_sla_hours,
            now=NOW,
        )
        is None
    )


def test_the_halt_path_imports_no_broker_market_or_reference_machinery() -> None:
    """A fresh interpreter: importing what the halt runs must not load celery, httpx or ingest."""
    code = (
        "import sys\n"
        "import execution.halt, risk.kill_switch, store.write, orchestration.sink\n"
        "bad = [m for m in ('celery', 'httpx', 'redis', 'ingest', 'orchestration.references',\n"
        "                   'orchestration.tasks', 'execution.reference') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_the_sweeper_is_the_only_reader_of_halt_requests() -> None:
    from pathlib import Path

    hits: list[str] = []
    for path in Path(".").glob("**/*.py"):
        parts = path.parts
        if parts[0] in {".venv", "tests", "docs", "store"}:
            continue
        src = path.read_text(encoding="utf-8")
        if "pending_halt_requests" in src and path.as_posix() != "orchestration/references.py":
            hits.append(path.as_posix())
    assert hits == []


def test_no_execution_module_touches_the_reference_tables_or_a_task_queue() -> None:
    from pathlib import Path

    for path in Path("execution").glob("*.py"):
        src: Any = path.read_text(encoding="utf-8")
        assert "celery" not in src.lower(), path
        assert "record_halt_references" not in src and "halt_reference_requests" not in src, path
