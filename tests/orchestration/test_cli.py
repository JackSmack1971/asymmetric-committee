"""The operator CLI: exit codes, JSON output, and the real inspect/reset paths over Postgres."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from contracts.enums import KillTrigger, RunMode, RunStatus
from contracts.models import CommitmentAnchor, KillSwitchEvent
from orchestration import cli, tasks
from orchestration.pipeline import BacktestOrchestrator, StepResult
from orchestration.sink import DecisionSink
from orchestration.startup import StartupConfigError
from tests.orchestration.anchoring_support import GIT_COMMIT, pending_proof
from tests.orchestration.test_sink import AS_OF, run_record, step
from tests.orchestration.test_tasks import (
    NOW,
    add_run,
    make_runtime,
)
from tests.store import factories as f


def invoke(
    argv: list[str], rt: tasks.Runtime | None = None, backtest: Any = None
) -> tuple[int, dict[str, Any] | None, str]:
    out, err = io.StringIO(), io.StringIO()

    def no_runtime() -> tasks.Runtime:
        raise AssertionError("this command needs no runtime")

    code = cli.run(
        argv,
        runtime_factory=(lambda: rt) if rt is not None else no_runtime,
        backtest_factory=lambda: backtest,
        out=out,
        err=err,
    )
    body = json.loads(out.getvalue()) if out.getvalue() else None
    return code, body, err.getvalue()


# --- commands over the fakes ---------------------------------------------------------------


def test_run_live_prints_the_run_it_advanced() -> None:
    rt, *_ = make_runtime(now=datetime(2024, 3, 1, 22, 15, tzinfo=UTC))
    code, body, _ = invoke(["run-live"], rt)
    assert code == 0 and body is not None
    assert body["status"] == "ANCHORED" and body["created"] is True


def test_the_execute_command_obeys_the_execution_window() -> None:
    rt, store, *_ = make_runtime(now=datetime(2024, 3, 2, 12, 0, tzinfo=UTC))  # a Saturday
    rid = add_run(store, RunStatus.ANCHORED)
    code, body, _ = invoke(["execute", str(rid)], rt)
    assert code == 0 and body == {"run_id": str(rid), "skipped": "too early"}


def test_manual_halt_flattens_unless_told_not_to() -> None:
    rt, store, _, _, execution = make_runtime()
    rid = add_run(store, RunStatus.EXECUTED)
    code, body, _ = invoke(["manual-halt", str(rid)], rt)
    assert code == 0 and body is not None and body["status"] == "PARTIAL"
    assert execution.halted == [(rid, True)]
    invoke(["manual-halt", str(rid), "--no-flatten"], rt)
    assert execution.halted[-1] == (rid, False)


def test_run_backtest_reports_each_step() -> None:
    class Fake:
        def run_backtest(self, start: datetime, end: datetime) -> list[StepResult]:
            self.window = (start, end)
            return [StepResult(uuid4(), start, RunStatus.ANCHORED, None, 1.5)]

    fake = Fake()
    code, body, _ = invoke(
        ["run-backtest", "--start", "2024-01-05", "--end", "2024-02-02"], backtest=fake
    )
    assert code == 0 and body is not None and body["steps"][0]["status"] == "ANCHORED"
    assert fake.window == (
        datetime(2024, 1, 5, 21, 0, tzinfo=UTC),
        datetime(2024, 2, 2, 21, 0, tzinfo=UTC),
    )


def _no_backtest() -> BacktestOrchestrator:
    raise AssertionError("this command builds no backtest orchestrator")


def test_a_startup_refusal_exits_2_and_any_other_failure_exits_1() -> None:
    def refuse() -> tasks.Runtime:
        raise StartupConfigError(["ANCHOR_GIT_REMOTE is not set"])

    err = io.StringIO()
    code = cli.run(["run-live"], runtime_factory=refuse, backtest_factory=_no_backtest, err=err)
    assert code == 2 and "ANCHOR_GIT_REMOTE" in err.getvalue()

    def broken() -> tasks.Runtime:
        raise RuntimeError("db down")

    err = io.StringIO()
    code = cli.run(["run-live"], runtime_factory=broken, backtest_factory=_no_backtest, err=err)
    assert code == 1 and err.getvalue().strip() == "error: RuntimeError: db down"


def test_bad_arguments_are_rejected_before_anything_runs() -> None:
    for argv in (["anchor", "not-a-uuid"], ["run-backtest", "--start", "2024-01-05"], ["nope"]):
        with pytest.raises(SystemExit) as caught:
            invoke(argv)
        assert caught.value.code == 2
    with pytest.raises(SystemExit):  # a timestamp without a zone is ambiguous
        invoke(["run-backtest", "--start", "2024-01-05T10:00:00", "--end", "2024-02-02"])


# --- inspect and reset over a real Postgres ------------------------------------------------


@pytest.fixture
def engine(pg_engine: Engine) -> Iterator[Engine]:
    def wipe() -> None:
        with pg_engine.begin() as c:
            c.execute(text("TRUNCATE runs, securities RESTART IDENTITY CASCADE"))

    wipe()
    yield pg_engine
    wipe()


def real_runtime(engine: Engine) -> tasks.Runtime:
    rt, *_ = make_runtime()
    rt.sink = DecisionSink(engine)
    return rt


def anchored_run(engine: Engine) -> UUID:
    sink = DecisionSink(engine)
    with engine.begin() as c:
        sid = f.security(c)
    run_id = uuid4()
    run = run_record(run_id, RunStatus.COMMITTED).model_copy(
        update={"mode": RunMode.LIVE, "ended_at": AS_OF}
    )
    art = step(run_id, sid, run=run, kill_switch=())
    assert art.commitment is not None
    sink.flush_step(art)
    sha = art.commitment.sha256
    sink.mark_anchored(
        CommitmentAnchor(
            run_id=run_id,
            sha256=sha,
            ots_proof=pending_proof(sha),
            git_commit=GIT_COMMIT,
            anchored_at=NOW,
        )
    )
    return run_id


def test_inspect_recomputes_the_commitment_and_reports_the_anchor(engine: Engine) -> None:
    run_id = anchored_run(engine)
    code, body, _ = invoke(["inspect-run", str(run_id)], real_runtime(engine))
    assert code == 0 and body is not None
    assert body["status"] == "ANCHORED" and body["commitment"]["verified"] is True
    assert body["anchor"]["git_commit"] == GIT_COMMIT
    assert body["anchor"]["ots_pending"] and body["anchor"]["bitcoin_verified_at"] is None
    assert body["halts"] == [] and body["open_orders"] == []


def test_inspect_flags_a_commitment_that_no_longer_recomputes(engine: Engine) -> None:
    run_id = anchored_run(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE committee_decisions SET pooled_p = 0.99"))
    code, body, _ = invoke(["inspect-run", str(run_id)], real_runtime(engine))
    assert code == 0 and body is not None
    assert body["commitment"]["verified"] is False and "recomputed" in body["commitment"]["error"]


def test_inspect_lists_halts_and_unknown_runs_fail(engine: Engine) -> None:
    run_id = anchored_run(engine)
    DecisionSink(engine).record_halt(
        KillSwitchEvent(run_id=run_id, triggered_at=NOW, trigger=KillTrigger.MANUAL, flattened=True)
    )
    _, body, _ = invoke(["inspect-run", str(run_id)], real_runtime(engine))
    assert body is not None and body["status"] == "PARTIAL"
    assert body["halts"][0]["trigger"] == "manual" and body["halts"][0]["flattened"] is True
    code, _, err = invoke(["inspect-run", str(uuid4())], real_runtime(engine))
    assert code == 1 and "does not exist" in err


def test_reset_clears_an_uncommitted_run_and_is_refused_for_a_committed_one(
    engine: Engine,
) -> None:
    sink = DecisionSink(engine)
    with engine.begin() as c:
        sid = f.security(c)
    open_run = uuid4()
    sink.flush_step(step(open_run, sid, commitment=None, kill_switch=()))
    rt = real_runtime(engine)
    code, body, _ = invoke(
        ["reset-run", str(open_run), "--actor", "ops", "--reason", "bad data"], rt
    )
    assert code == 0 and body is not None and body["cleared"]["agent_verdicts"] == 2

    committed = anchored_run(engine)
    code, _, err = invoke(["reset-run", str(committed), "--actor", "ops", "--reason", "x"], rt)
    assert code == 1 and "immutable" in err
    assert sink.load_commitment_material(committed) is not None  # nothing was cleared
