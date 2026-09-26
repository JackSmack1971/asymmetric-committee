"""ExecutionStage and the orchestrator hand-off: eligibility, replay safety, state advancement.

Eligibility is checked from a recomputed commitment: only a verified ``ANCHORED`` run trades."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import respx

from contracts.commitment import CommitmentIntegrityError, CommitmentMaterial
from contracts.enums import (
    HALT_REASON_PREFIX,
    AgentName,
    BrokerOrderStatus,
    CioAction,
    FeedName,
    KillTrigger,
    ModelTier,
    OrderSide,
    ReferenceSource,
    RunMode,
    RunStatus,
)
from contracts.models import (
    AgentVerdictLLM,
    CioDecision,
    CioDecisionLLM,
    CioNameDecisionLLM,
    DlqRecord,
    ExecutionRecord,
    KillSwitchEvent,
    PortfolioSnapshot,
    ProposedBook,
    ProposedPosition,
    RedTeamVerdictLLM,
    RunRecord,
)
from execution.executor import refresh_record
from execution.gateway import BrokerError
from orchestration.anchor_stage import AnchorResult
from orchestration.execution_stage import (
    ExecutionNotEligibleError,
    ExecutionPlanError,
    ExecutionResult,
    ExecutionStage,
    MarketView,
    plan_targets,
)
from orchestration.pipeline import BacktestOrchestrator, StepResult
from risk.kill_switch import EquityEvidenceError
from tests.agents.world import AS_OF as WORLD_AS_OF
from tests.execution.fakes import RUN, T0, FakeBroker, FakeClock, MemoryRecorder
from tests.orchestration.anchoring_support import material_for, pending_proof
from tests.orchestration.test_pipeline import (
    BASE_CFG,
    URL,
    Loader,
    Provider,
    RecordingSink,
    _client,
    cfg_with,
)

AS_OF = datetime(2024, 3, 1, 21, 0, tzinfo=UTC)
VIEW = MarketView(symbols={1: "AAA", 2: "BBB"}, decision_prices={1: 100.0, 2: 100.0})


class MemStore(MemoryRecorder):
    """Stand-in for ``DecisionSink`` with the same semantics for the methods the stage uses."""

    def __init__(self) -> None:
        super().__init__()
        self.run: RunRecord | None = None
        self.material: CommitmentMaterial | None = None
        self.events: list[KillSwitchEvent] = []
        self.dlq: list[DlqRecord] = []
        self.advances = 0
        self.fail_mark = False
        self.fail_record_after: int | None = None

    def record_executions(self, records: Sequence[ExecutionRecord]) -> None:
        if self.fail_record_after is not None and self.writes >= self.fail_record_after:
            raise RuntimeError("database unavailable")
        super().record_executions(records)

    def load_run(self, run_id: UUID) -> RunRecord | None:
        return self.run

    def load_commitment_material(self, run_id: UUID) -> CommitmentMaterial | None:
        return self.material

    def load_kill_switch_events(self, run_id: UUID) -> list[KillSwitchEvent]:
        return list(self.events)

    def record_halt(self, event: KillSwitchEvent) -> bool:
        """Event row and ``-> PARTIAL`` together, as ``write.record_halt`` does."""
        if event not in self.events:
            self.events.append(event)
        assert self.run is not None
        if self.run.status not in (RunStatus.COMMITTED, RunStatus.ANCHORED, RunStatus.EXECUTED):
            return False
        self.run = self.run.model_copy(
            update={
                "status": RunStatus.PARTIAL,
                "status_reason": f"{HALT_REASON_PREFIX}{event.trigger.value}",
            }
        )
        return True

    def record_dlq(self, records: tuple[DlqRecord, ...]) -> None:
        self.dlq.extend(records)

    def mark_executed(self, run_id: UUID, ended_at: datetime) -> bool:
        if self.fail_mark:
            raise RuntimeError("database unavailable")
        assert self.run is not None
        if self.run.status is not RunStatus.ANCHORED:
            return False
        self.run = self.run.model_copy(update={"status": RunStatus.EXECUTED})
        self.advances += 1
        return True


def cio(run_id: UUID) -> CioDecision:
    return CioDecision(
        run_id=run_id,
        as_of=AS_OF,
        prompt_version="v1-cio",
        model_served="provider/strong",
        decisions=(
            CioNameDecisionLLM(entity_token="ENTITY_01", action=CioAction.APPROVE, reason="ok"),
            CioNameDecisionLLM(entity_token="ENTITY_02", action=CioAction.APPROVE, reason="ok"),
        ),
        rationale="ok",
    )


def book(run_id: UUID = RUN, weights: tuple[float, ...] = (0.05, 0.03)) -> ProposedBook:
    return ProposedBook(
        run_id=run_id,
        as_of=AS_OF,
        positions=tuple(
            ProposedPosition(
                security_id=i + 1,
                entity_token=f"ENTITY_0{i + 1}",
                sector="Tech",
                pooled_p=0.6,
                target_weight=w,
            )
            for i, w in enumerate(weights)
        ),
    )


def anchored_store(
    *,
    mode: RunMode = RunMode.LIVE,
    with_cio: bool = True,
    status: RunStatus = RunStatus.ANCHORED,
) -> MemStore:
    s = MemStore()
    s.run = RunRecord(
        run_id=RUN,
        mode=mode,
        as_of=AS_OF,
        config_hash="c" * 64,
        status=status,
        started_at=AS_OF,
    )
    b = book()
    snapshot = PortfolioSnapshot(
        run_id=RUN,
        as_of=AS_OF,
        book=b,
        cash_weight=b.cash_weight,
        cio=cio(RUN) if with_cio else None,
    )
    s.material = material_for(s.run, snapshot, committed_at=AS_OF)
    return s


class Rig:
    def __init__(
        self,
        store: MemStore | None = None,
        feeds: Callable[[], Mapping[FeedName, datetime | None]] | None = None,
    ) -> None:
        self.clock = FakeClock()
        self.broker = FakeBroker(self.clock)
        self.store = store or anchored_store()
        self.feeds = feeds or (
            lambda: {f: T0 - timedelta(minutes=1) for f in BASE_CFG.pipeline.freshness_sla_hours}
        )
        self.stage = self.new_stage()

    def new_stage(self) -> ExecutionStage:
        """A fresh stage (as after a process restart) over the same broker and store."""
        return ExecutionStage(
            config=BASE_CFG,
            gateway=self.broker,
            store=self.store,
            market=lambda run: VIEW,
            reference=lambda symbol: lambda: (100.0, ReferenceSource.IEX_MID),
            feeds=self.feeds,
            clock=self.clock,
            sleep=self.clock.sleep,
            executor_options={"poll_attempts": 2},
        )


# --- eligibility: nothing reaches the broker ---------------------------------------------------


@pytest.mark.parametrize(
    "status", [RunStatus.PARTIAL, RunStatus.FAILED, RunStatus.AGENTS_OK, RunStatus.COMMITTED]
)
def test_runs_that_are_not_anchored_never_execute(status: RunStatus) -> None:
    """A COMMITTED run with a perfectly valid commitment still does not trade until anchored."""
    r = Rig()
    assert r.store.run is not None
    r.store.run = r.store.run.model_copy(update={"status": status})
    with pytest.raises(ExecutionNotEligibleError, match="not ANCHORED"):
        r.stage.execute_run(RUN)
    assert r.broker.calls == [] and r.store.advances == 0


@pytest.mark.parametrize("mode", [RunMode.BACKTEST, RunMode.ABLATION])
def test_only_live_runs_execute(mode: RunMode) -> None:
    r = Rig(anchored_store(mode=mode))
    with pytest.raises(ExecutionNotEligibleError, match="never execute"):
        r.stage.execute_run(RUN)
    assert r.broker.calls == []


def test_missing_commitment_or_cio_decision_never_executes() -> None:
    r = Rig()
    r.store.material = None
    with pytest.raises(ExecutionNotEligibleError, match="no decision commitment"):
        r.stage.execute_run(RUN)
    assert r.broker.calls == []

    r = Rig(anchored_store(with_cio=False))
    with pytest.raises(ExecutionNotEligibleError, match="no CIO decision"):
        r.stage.execute_run(RUN)
    assert r.broker.calls == [] and r.store.advances == 0


def test_unknown_run_never_executes() -> None:
    r = Rig()
    r.store.run = None
    with pytest.raises(ExecutionNotEligibleError):
        r.stage.execute_run(RUN)
    assert r.broker.calls == []


# --- the stored hash is never trusted: any mismatch is zero broker calls -------------------------


def _tampered(store: MemStore, how: str) -> None:
    m = store.material
    assert m is not None and m.anchor is not None
    if how == "stored_hash":
        other = "f" * 64
        store.material = m.model_copy(update={"stored_sha256": other})
    elif how == "book":
        heavier = m.snapshot.book.positions[0].model_copy(update={"target_weight": 0.08})
        book_ = m.snapshot.book.model_copy(
            update={"positions": (heavier, *m.snapshot.book.positions[1:])}
        )
        snap = m.snapshot.model_copy(update={"book": book_, "cash_weight": book_.cash_weight})
        store.material = m.model_copy(update={"snapshot": snap})
    elif how == "anchor_hash":
        store.material = m.model_copy(
            update={"anchor": m.anchor.model_copy(update={"sha256": "e" * 64})}
        )
    elif how == "proof_for_other_digest":
        foreign = pending_proof("d" * 64)
        store.material = m.model_copy(
            update={"anchor": m.anchor.model_copy(update={"ots_proof": foreign})}
        )
    elif how == "no_git_commit":
        store.material = m.model_copy(
            update={"anchor": m.anchor.model_copy(update={"git_commit": None})}
        )
    elif how == "no_proof":
        store.material = m.model_copy(
            update={"anchor": m.anchor.model_copy(update={"ots_proof": None})}
        )
    elif how == "no_anchor":
        store.material = m.model_copy(update={"anchor": None})
    else:  # pragma: no cover
        raise AssertionError(how)


@pytest.mark.parametrize(
    "how",
    [
        "stored_hash",
        "book",
        "anchor_hash",
        "proof_for_other_digest",
        "no_git_commit",
        "no_proof",
        "no_anchor",
    ],
)
def test_a_commitment_or_anchor_mismatch_makes_no_broker_call(how: str) -> None:
    r = Rig()
    _tampered(r.store, how)
    with pytest.raises(CommitmentIntegrityError):
        r.stage.execute_run(RUN)
    assert r.broker.calls == [] and r.broker.submits == []  # not even a read
    assert r.store.advances == 0 and r.store.run is not None
    assert r.store.run.status is RunStatus.ANCHORED  # the run is left as it was
    assert [d.agent for d in r.store.dlq] == ["executor"]  # and the failure is evidenced


def test_an_executed_run_is_verified_too_on_replay() -> None:
    r = Rig(anchored_store(status=RunStatus.EXECUTED))
    _tampered(r.store, "stored_hash")
    with pytest.raises(CommitmentIntegrityError):
        r.stage.execute_run(RUN)
    assert r.broker.calls == []


# --- the happy path and replay -----------------------------------------------------------------


def test_an_anchored_run_executes_once_and_advances_exactly_once() -> None:
    r = Rig()
    res = r.stage.execute_run(RUN)
    assert res.status is RunStatus.EXECUTED and res.advanced and r.store.advances == 1
    lim = [c for c in r.broker.submits if c.endswith("-lim")]
    assert sorted(lim) == [f"{RUN}-1-lim", f"{RUN}-2-lim"]  # one order chain per name
    assert len(r.broker.submits) == len(set(r.broker.submits))
    assert {rec.side for rec in r.store.rows.values()} == {OrderSide.BUY}
    assert r.store.run is not None and r.store.run.status is RunStatus.EXECUTED

    again = r.new_stage().execute_run(RUN)  # replay of an EXECUTED run
    assert again.status is RunStatus.EXECUTED and not again.advanced
    assert r.store.advances == 1 and len(r.broker.submits) == 4  # nothing new was sent


def test_a_crash_before_the_state_advance_is_reconciled_not_resent() -> None:
    r = Rig()
    r.store.fail_mark = True
    with pytest.raises(RuntimeError, match="database unavailable"):
        r.stage.execute_run(RUN)
    assert r.store.run is not None and r.store.run.status is RunStatus.ANCHORED  # not EXECUTED
    sent = list(r.broker.submits)
    assert sent  # the orders did go out

    r.store.fail_mark = False
    res = r.new_stage().execute_run(RUN)  # restart: reconcile against the broker
    assert res.status is RunStatus.EXECUTED and res.advanced and r.store.advances == 1
    assert r.broker.submits == sent  # no duplicate order


def test_a_broker_failure_midway_never_marks_executed_and_resume_finishes() -> None:
    r = Rig()
    real = r.broker.submit_limit
    seen: list[str] = []

    def flaky(**kw: object) -> object:
        seen.append(str(kw["client_order_id"]))
        if len(seen) == 2:
            raise BrokerError("503 from broker")  # second name: nothing was accepted
        return real(**kw)  # type: ignore[arg-type]

    r.broker.submit_limit = flaky  # type: ignore[method-assign,assignment]
    with pytest.raises(BrokerError):
        r.stage.execute_run(RUN)
    assert r.store.advances == 0 and r.store.run is not None
    assert r.store.run.status is RunStatus.ANCHORED
    assert [d.agent for d in r.store.dlq] == ["executor"]  # the failure is evidenced

    r.broker.submit_limit = real  # type: ignore[method-assign]
    first_chain = [c for c in r.broker.submits]
    res = r.new_stage().execute_run(RUN)
    assert res.status is RunStatus.EXECUTED and r.store.advances == 1
    assert len(r.broker.submits) == len(set(r.broker.submits))  # nothing sent twice
    assert set(first_chain) <= set(r.broker.submits)


def test_evidence_persistence_failure_cannot_advance() -> None:
    r = Rig()
    r.store.fail_record_after = 3
    with pytest.raises(RuntimeError, match="database unavailable"):
        r.stage.execute_run(RUN)
    assert r.store.advances == 0 and r.store.run is not None
    assert r.store.run.status is RunStatus.ANCHORED


def test_incomplete_execution_is_not_reported_as_executed() -> None:
    r = Rig()
    r.broker.cancel_mode = "stuck"  # the limit order can never be confirmed terminal
    with pytest.raises(BrokerError):  # CancelNotConfirmedError
        r.stage.execute_run(RUN)
    assert r.store.advances == 0
    assert not any(c == "submit_market" for c in r.broker.calls)


# --- kill switch and equity evidence gate execution --------------------------------------------


def _halted_store(trigger: KillTrigger = KillTrigger.DAILY_LOSS) -> MemStore:
    """The state a halt leaves behind: event row and PARTIAL, committed evidence untouched."""
    st = anchored_store()
    st.record_halt(KillSwitchEvent(run_id=RUN, triggered_at=T0, trigger=trigger, daily_loss=0.05))
    return st


def test_a_stored_halt_survives_a_restart_and_blocks_all_trading() -> None:
    r = Rig(_halted_store())
    res = r.new_stage().execute_run(RUN)  # a brand-new stage sees the persisted halt
    assert (
        res.status is RunStatus.PARTIAL
        and not res.advanced
        and (res.reason or "").startswith(HALT_REASON_PREFIX)
    )
    assert r.broker.submits == [] and r.store.advances == 0
    assert r.store.material is not None and r.store.material.anchor is not None  # evidence kept


def test_a_halted_run_is_terminal_and_never_resumes_trading() -> None:
    r = Rig(_halted_store())
    r.store.material = None  # even with nothing to verify, a halted run never reaches eligibility
    for _ in range(2):
        assert r.new_stage().execute_run(RUN).status is RunStatus.PARTIAL
    assert r.broker.submits == [] and r.store.advances == 0


def test_a_halt_before_any_order_marks_the_run_partial_and_keeps_the_commitment() -> None:
    r = Rig()
    r.broker.equity = (90_000.0, 100_000.0)
    res = r.stage.execute_run(RUN)
    assert res.status is RunStatus.PARTIAL and r.broker.submits == []
    (ev,) = r.store.events
    assert ev.trigger is KillTrigger.DAILY_LOSS and ev.peak_drawdown == pytest.approx(0.1)
    assert r.store.run is not None
    assert r.store.run.status_reason == f"{HALT_REASON_PREFIX}daily_loss"
    assert r.store.material is not None and r.store.material.anchor is not None
    assert r.new_stage().execute_run(RUN).status is RunStatus.PARTIAL  # still halted after restart
    assert len(r.store.events) == 1 and r.broker.submits == []


def test_a_halt_during_partial_execution_cancels_orders_and_keeps_every_row() -> None:
    r = Rig()
    r.broker.cancel_mode = "stuck"  # the first name's limit order sits open and cannot be cancelled
    with pytest.raises(BrokerError):
        r.stage.execute_run(RUN)
    sent = list(r.broker.submits)
    assert sent and r.store.rows  # orders went out and their evidence is stored
    rows_before = dict(r.store.rows)

    r.broker.cancel_mode = "confirm"
    res = r.new_stage().manual_halt(RUN, flatten=False)  # operator halts the half-done run
    assert res.status is RunStatus.PARTIAL and res.reason == f"{HALT_REASON_PREFIX}manual"
    assert all(o.status.terminal for o in r.broker.orders.values())  # nothing left working
    assert r.broker.submits == sent  # and nothing new was sent
    assert dict(r.store.rows) == rows_before  # order evidence untouched
    assert r.store.material is not None and r.store.material.anchor is not None


def test_a_manual_flatten_after_execution_marks_the_run_partial_and_keeps_all_evidence() -> None:
    r = Rig()
    assert r.stage.execute_run(RUN).status is RunStatus.EXECUTED
    rows_before, material = dict(r.store.rows), r.store.material
    r.broker.pos = {"AAA": 50.0, "BBB": 30.0}

    res = r.new_stage().manual_halt(RUN)  # flatten by default
    assert res.status is RunStatus.PARTIAL and res.reason == f"{HALT_REASON_PREFIX}manual"
    assert sorted(r.broker.closed) == ["AAA", "BBB"]
    (ev,) = r.store.events
    assert ev.flattened and ev.trigger is KillTrigger.MANUAL
    assert dict(r.store.rows) == rows_before and r.store.material is material  # nothing erased
    assert r.new_stage().execute_run(RUN).status is RunStatus.PARTIAL  # and it stays terminal


def test_missing_equity_history_stops_execution_before_any_order() -> None:
    r = Rig()
    r.broker.history = []
    with pytest.raises(EquityEvidenceError):
        r.stage.execute_run(RUN)
    assert r.broker.submits == [] and r.store.advances == 0


# --- planning ----------------------------------------------------------------------------------


def test_plan_sizes_whole_shares_and_trades_only_the_delta() -> None:
    targets = plan_targets(
        run_id=RUN, book=book(), view=VIEW, equity=100_000.0, positions={"AAA": 20.0, "BBB": 30.0}
    )
    assert [(t.symbol, t.side, t.qty) for t in targets] == [("AAA", OrderSide.BUY, 30.0)]
    sell = plan_targets(
        run_id=RUN,
        book=book(weights=(0.0, 0.03)),
        view=VIEW,
        equity=100_000.0,
        positions={"AAA": 7.0},
    )
    assert (sell[0].symbol, sell[0].side, sell[0].qty) == ("AAA", OrderSide.SELL, 7.0)


def test_plan_fails_closed_on_unknown_names_and_bad_inputs() -> None:
    with pytest.raises(ExecutionPlanError):
        plan_targets(run_id=RUN, book=book(), view=MarketView({1: "AAA"}, {1: 100.0}),
                     equity=1e5, positions={})  # fmt: skip
    with pytest.raises(ExecutionPlanError):
        plan_targets(run_id=RUN, book=book(), view=MarketView(VIEW.symbols, {1: 100.0}),
                     equity=1e5, positions={})  # fmt: skip
    with pytest.raises(ExecutionPlanError):
        plan_targets(run_id=RUN, book=book(), view=VIEW, equity=0.0, positions={})


# --- orchestrator hand-off ---------------------------------------------------------------------


class SpyExecution:
    def __init__(self, status: RunStatus = RunStatus.EXECUTED) -> None:
        self.calls: list[UUID] = []
        self.status = status

    def execute_run(self, run_id: UUID) -> ExecutionResult:
        self.calls.append(run_id)
        return ExecutionResult(run_id, self.status, advanced=True)


class SpyAnchoring:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    def anchor_run(self, run_id: UUID) -> AnchorResult:
        self.calls.append(run_id)
        return AnchorResult(run_id, RunStatus.ANCHORED, advanced=True)


def test_backtests_cannot_be_given_an_execution_stage() -> None:
    with pytest.raises(ValueError, match="LIVE"):
        BacktestOrchestrator(
            config=BASE_CFG, loader=None, sink=None, client_for=None, cio_client=None,  # type: ignore[arg-type]
            anchoring=SpyAnchoring(), execution=SpyExecution(),
        )  # fmt: skip


def test_execution_needs_anchoring() -> None:
    with pytest.raises(ValueError, match="needs anchoring"):
        BacktestOrchestrator(
            config=BASE_CFG, loader=None, sink=None, client_for=None, cio_client=None,  # type: ignore[arg-type]
            mode=RunMode.LIVE, execution=SpyExecution(),
        )  # fmt: skip


def _result(status: RunStatus) -> StepResult:
    return StepResult(RUN, AS_OF, status, None, 0.0)


def _live(spy: SpyExecution, anchors: SpyAnchoring | None = None) -> BacktestOrchestrator:
    return BacktestOrchestrator(
        config=BASE_CFG, loader=None, sink=None, client_for=None, cio_client=None,  # type: ignore[arg-type]
        mode=RunMode.LIVE, anchoring=anchors or SpyAnchoring(), execution=spy,
    )  # fmt: skip


def test_only_a_committed_result_is_anchored_and_only_an_anchored_one_executes() -> None:
    spy, anchors = SpyExecution(), SpyAnchoring()
    orch = _live(spy, anchors)
    for status in (RunStatus.PARTIAL, RunStatus.FAILED, RunStatus.AGENTS_OK, RunStatus.EXECUTED):
        assert orch._maybe_execute(_result(status)).status is status
    assert spy.calls == [] and anchors.calls == []
    done = orch._maybe_execute(_result(RunStatus.COMMITTED))
    assert done.status is RunStatus.EXECUTED
    assert anchors.calls == [RUN] and spy.calls == [RUN]  # anchored first, then executed

    resumed = orch._maybe_execute(_result(RunStatus.ANCHORED))  # already anchored: no re-anchor
    assert resumed.status is RunStatus.EXECUTED and anchors.calls == [RUN]


def test_a_backtest_orchestrator_anchors_and_stops() -> None:
    anchors = SpyAnchoring()
    orch = BacktestOrchestrator(
        config=BASE_CFG, loader=None, sink=None, client_for=None, cio_client=None,  # type: ignore[arg-type]
        mode=RunMode.BACKTEST, anchoring=anchors,
    )  # fmt: skip
    assert orch._maybe_execute(_result(RunStatus.COMMITTED)).status is RunStatus.ANCHORED
    assert anchors.calls == [RUN]


def test_an_anchoring_failure_stops_before_execution() -> None:
    class Boom:
        def anchor_run(self, run_id: UUID) -> AnchorResult:
            raise RuntimeError("remote unreachable")

    spy = SpyExecution()
    orch = BacktestOrchestrator(
        config=BASE_CFG, loader=None, sink=None, client_for=None, cio_client=None,  # type: ignore[arg-type]
        mode=RunMode.LIVE, anchoring=Boom(), execution=spy,
    )  # fmt: skip
    with pytest.raises(RuntimeError):
        orch._maybe_execute(_result(RunStatus.COMMITTED))
    assert spy.calls == []


def test_executor_failure_propagates_and_the_result_is_not_executed() -> None:
    class Boom:
        def execute_run(self, run_id: UUID) -> ExecutionResult:
            raise BrokerError("down")

    orch = BacktestOrchestrator(
        config=BASE_CFG, loader=None, sink=None, client_for=None, cio_client=None,  # type: ignore[arg-type]
        mode=RunMode.LIVE, anchoring=SpyAnchoring(), execution=Boom(),
    )  # fmt: skip
    with pytest.raises(BrokerError):
        orch._maybe_execute(_result(RunStatus.COMMITTED))


# --- the real pipeline in front of the stage -----------------------------------------------------


def live_orchestrator(
    sink: RecordingSink,
    loader: Loader,
    spy: SpyExecution,
    anchors: SpyAnchoring | None = None,
    *,
    budget: float = 1000.0,
) -> BacktestOrchestrator:
    cfg = cfg_with(budget)
    voter = _client(cfg.models.tiers[ModelTier.FAST], AgentVerdictLLM)
    red = _client(cfg.models.tiers[ModelTier.STRONG], RedTeamVerdictLLM)
    return BacktestOrchestrator(
        config=cfg,
        loader=loader,
        sink=sink,
        client_for=lambda a: red if a is AgentName.RED_TEAM else voter,
        cio_client=_client(cfg.models.tiers[ModelTier.STRONG], CioDecisionLLM),
        concurrency=4,
        mode=RunMode.LIVE,
        anchoring=anchors or SpyAnchoring(),
        execution=spy,
    )


@respx.mock
def test_fresh_committed_live_run_is_anchored_then_executed_and_a_resume_reconciles() -> None:
    loader, sink, spy, anchors = Loader(), RecordingSink(), SpyExecution(), SpyAnchoring()
    route = respx.post(URL).mock(side_effect=Provider(loader))
    orch = live_orchestrator(sink, loader, spy, anchors)

    first = orch.run_step(WORLD_AS_OF)
    assert first.status is RunStatus.EXECUTED and spy.calls == [first.run_id]
    assert anchors.calls == [first.run_id]
    assert sink.runs[first.run_id].mode is RunMode.LIVE
    assert len(sink.steps) == 1  # the commit was flushed before anchoring or executing
    llm_calls = route.call_count

    sink.runs[first.run_id] = sink.runs[first.run_id].model_copy(  # the anchor was lost
        update={"status": RunStatus.COMMITTED}
    )
    resumed = orch.run_step(WORLD_AS_OF, run_id=first.run_id)
    assert resumed.run_id == first.run_id and anchors.calls == [first.run_id] * 2
    assert spy.calls == [first.run_id] * 2
    assert route.call_count == llm_calls and len(sink.steps) == 1  # reconcile, not recompute

    sink.runs[first.run_id] = sink.runs[first.run_id].model_copy(  # anchored, execution lost
        update={"status": RunStatus.ANCHORED}
    )
    orch.run_step(WORLD_AS_OF, run_id=first.run_id)
    assert anchors.calls == [first.run_id] * 2 and spy.calls == [first.run_id] * 3

    sink.runs[first.run_id] = sink.runs[first.run_id].model_copy(
        update={"status": RunStatus.EXECUTED}
    )
    orch.run_step(WORLD_AS_OF, run_id=first.run_id)
    assert len(spy.calls) == 3  # an EXECUTED run is returned as is


@respx.mock
def test_a_halted_run_is_never_resumed_only_a_new_run_starts_fresh() -> None:
    loader, sink, spy, anchors = Loader(), RecordingSink(), SpyExecution(), SpyAnchoring()
    route = respx.post(URL).mock(side_effect=Provider(loader))
    orch = live_orchestrator(sink, loader, spy, anchors)
    first = orch.run_step(WORLD_AS_OF)
    calls = route.call_count

    sink.runs[first.run_id] = sink.runs[first.run_id].model_copy(
        update={"status": RunStatus.PARTIAL, "status_reason": f"{HALT_REASON_PREFIX}manual"}
    )
    again = orch.run_step(WORLD_AS_OF, run_id=first.run_id)
    assert again.status is RunStatus.PARTIAL and again.run_id == first.run_id
    assert route.call_count == calls and len(sink.steps) == 1  # nothing recomputed or rewritten
    assert anchors.calls == [first.run_id] and spy.calls == [first.run_id]  # nothing re-run

    fresh = orch.run_step(WORLD_AS_OF)  # recovery from a halt: a fresh run_id
    assert fresh.run_id != first.run_id and fresh.status is RunStatus.EXECUTED


@respx.mock
def test_partial_runs_never_reach_the_executor() -> None:
    loader, sink, spy = Loader(), RecordingSink(), SpyExecution()
    respx.post(URL).mock(side_effect=Provider(loader, cio="garbage"))  # no valid CIO decision
    result = live_orchestrator(sink, loader, spy).run_step(WORLD_AS_OF)
    assert result.status is RunStatus.PARTIAL and result.book is None
    assert spy.calls == []

    loader2, sink2, spy2 = Loader(), RecordingSink(), SpyExecution()
    respx.post(URL).mock(side_effect=Provider(loader2))
    over = live_orchestrator(sink2, loader2, spy2, budget=2.0).run_step(WORLD_AS_OF)
    assert over.status is RunStatus.PARTIAL and spy2.calls == []


@respx.mock
def test_a_failed_commit_flush_never_reaches_the_executor() -> None:
    loader, spy = Loader(), SpyExecution()
    sink = RecordingSink(fail=True)  # the COMMITTED flush raises before anything is stored
    respx.post(URL).mock(side_effect=Provider(loader))
    with pytest.raises(RuntimeError):
        live_orchestrator(sink, loader, spy).run_step(WORLD_AS_OF)
    assert spy.calls == []


@respx.mock
def test_executor_failure_leaves_the_anchored_run_anchored() -> None:
    class Boom:
        def execute_run(self, run_id: UUID) -> ExecutionResult:
            raise BrokerError("down")

    loader, sink = Loader(), RecordingSink()
    respx.post(URL).mock(side_effect=Provider(loader))
    orch = live_orchestrator(sink, loader, SpyExecution())
    orch._execution = Boom()
    with pytest.raises(BrokerError):
        orch.run_step(WORLD_AS_OF)
    (run,) = sink.runs.values()
    assert run.status is RunStatus.COMMITTED  # anchoring is a spy here; not FAILED, not EXECUTED
    assert len(sink.steps) == 1  # and no failure flush demoted or rewrote it


# --- reconciliation with the broker ---------------------------------------------------------------


def _stuck_run() -> tuple[Rig, dict[str, ExecutionRecord], list[str]]:
    r = Rig()
    r.broker.cancel_mode = "stuck"
    with pytest.raises(BrokerError):
        r.stage.execute_run(RUN)  # every limit order went out and is recorded as working
    before = dict(r.store.rows)
    assert len(before) == 2 and all(not rec.status.terminal for rec in before.values())
    return r, before, list(r.broker.submits)


def test_reconcile_records_a_fill_the_broker_reports_without_sending_anything() -> None:
    r, before, sent = _stuck_run()
    for order in r.broker.orders.values():
        r.broker.fill(order.broker_order_id, order.qty, 101.0)  # filled after our process died
    assert r.new_stage().reconcile_orders(RUN) == 2 and r.broker.submits == sent

    filled = r.store.rows[f"{RUN}-1-lim"]
    assert filled.status is BrokerOrderStatus.FILLED and filled.fill_price == pytest.approx(101.0)
    assert filled.slippage_bps == pytest.approx(
        100.0
    )  # against the reference stored at first sight
    assert filled.reference_price == before[f"{RUN}-1-lim"].reference_price
    assert r.new_stage().reconcile_orders(RUN) == 0  # terminal rows are not looked at again


def test_reconcile_records_a_cancel_the_broker_confirmed() -> None:
    r, _, sent = _stuck_run()
    for order in r.broker.orders.values():
        r.broker.set_status(order.broker_order_id, BrokerOrderStatus.CANCELED)
    assert r.new_stage().reconcile_orders(RUN) == 2 and r.broker.submits == sent
    assert r.store.rows[f"{RUN}-1-lim"].status is BrokerOrderStatus.CANCELED


def test_reconcile_refuses_a_broker_order_that_is_not_the_stored_one() -> None:
    r = Rig()
    r.stage.execute_run(RUN)
    stored = next(iter(r.store.rows.values()))
    other = next(o for o in r.broker.orders.values() if o.broker_order_id != stored.broker_order_id)
    with pytest.raises(ValueError, match="does not match"):
        refresh_record(stored, other)


# --- the whole book in shared phases --------------------------------------------------------------


class Crash(BaseException):
    """A process dying mid-run; nothing may swallow it."""


def test_the_stage_waits_one_window_for_the_whole_book() -> None:
    r = Rig()
    res = r.stage.execute_run(RUN)
    assert res.status is RunStatus.EXECUTED
    assert [s for s in r.clock.slept if s >= 60] == [timedelta(minutes=15).total_seconds()]
    assert r.broker.submits[:2] == [f"{RUN}-1-lim", f"{RUN}-2-lim"]  # both limits out first
    assert {c.rsplit("-", 1)[1] for c in r.broker.submits[2:]} == {"mkt"}


def test_a_restart_during_the_shared_wait_resumes_without_a_second_order() -> None:
    r = Rig()

    def crash(_seconds: float) -> None:
        raise Crash

    r.stage._sleep = crash
    with pytest.raises(Crash):
        r.stage.execute_run(RUN)
    assert r.broker.submits == [f"{RUN}-1-lim", f"{RUN}-2-lim"]
    assert r.store.run is not None and r.store.run.status is RunStatus.ANCHORED

    res = r.new_stage().execute_run(RUN)  # the restarted process reads Postgres + the broker
    assert res.status is RunStatus.EXECUTED
    assert len(set(r.broker.submits)) == len(r.broker.submits)  # nothing was sent twice
    assert all(o.status.terminal for o in r.broker.orders.values())


def test_a_feed_going_stale_during_the_window_halts_before_any_market_order() -> None:
    # News is 50 minutes old at the start (SLA 1h) and 65 minutes old after the 15-minute window.
    r = Rig(
        feeds=lambda: {f: T0 - timedelta(minutes=50) for f in BASE_CFG.pipeline.freshness_sla_hours}
    )
    res = r.stage.execute_run(RUN)
    assert res.status is RunStatus.PARTIAL and "kill switch engaged" in (res.reason or "")
    assert [f"{RUN}-1-lim", f"{RUN}-2-lim"] == r.broker.submits  # no new exposure after the halt
    assert not r.broker.open_orders_now()  # everything cancelled and confirmed terminal
    assert [e.trigger for e in r.store.events] == [KillTrigger.STALE_FEED]  # halt recorded
    assert r.store.run is not None and (r.store.run.status_reason or "").startswith(
        HALT_REASON_PREFIX
    )
    assert r.store.material is not None and r.store.advances == 0  # commitment kept, never EXECUTED
    assert {rec.status for rec in r.store.rows.values()} == {BrokerOrderStatus.CANCELED}
    assert r.new_stage().execute_run(RUN).status is RunStatus.PARTIAL  # terminal after restart
    assert r.broker.submits == [f"{RUN}-1-lim", f"{RUN}-2-lim"]


def test_an_operator_halt_during_the_window_stops_the_market_phase() -> None:
    r = Rig()
    r.clock.on_sleep.append(lambda: r.new_stage().manual_halt(RUN, flatten=False))
    res = r.stage.execute_run(RUN)
    assert res.status is RunStatus.PARTIAL
    assert not any(c.endswith("-mkt") for c in r.broker.submits) and r.store.advances == 0
    assert [e.trigger for e in r.store.events] == [KillTrigger.MANUAL]
