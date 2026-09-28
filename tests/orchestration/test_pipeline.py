"""BacktestOrchestrator over mocked OpenRouter (respx) and a recording sink (P5 step 3)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

import httpx
import pytest
import respx

from agents.base import ChatClient
from agents.llm.openrouter import BASE_URL, OpenRouterClient
from agents.partitions import EntityData, Partition, Partitioner
from config.loader import AppConfig, BudgetsConfig, load_config
from contracts.data import Security
from contracts.enums import AgentName, Horizon, ModelTier, RunMode, RunStatus
from contracts.models import (
    AgentVerdictLLM,
    CioDecisionLLM,
    ProposedBook,
    RedTeamVerdict,
    RedTeamVerdictLLM,
    RunRecord,
    StepArtifacts,
    VerdictRecord,
)
from orchestration.pipeline import (
    BacktestOrchestrator,
    LookAheadError,
    RunMismatchError,
    StepInputs,
    weekly_steps,
)
from tests.agents.test_base import USAGE, _cite, _value_json
from tests.agents.world import AS_OF, FUTURE, IDENTITY, _securities, build_world
from universe.snapshot import AliasCoverageError

URL = f"{BASE_URL}/chat/completions"
BASE_CFG = load_config(allow_placeholders=True, env={})
FAST = BASE_CFG.models.tiers[ModelTier.FAST]
STRONG = BASE_CFG.models.tiers[ModelTier.STRONG]
PRICED = FAST.primary.model_copy(
    update={"input_price_usd_per_mtok": 1000.0, "output_price_usd_per_mtok": 2000.0}
)
CALL_USD = PRICED.local_cost_usd(USAGE["prompt_tokens"], USAGE["completion_tokens"])  # $1.40
BRANDS = {
    1234567: ["Zephyr"],
    2345678: ["Borealis"],
    3456789: ["Calder"],
    4567890: ["Dunmore"],
}


def cfg_with(budget_usd: float = 1000.0) -> AppConfig:
    models = BASE_CFG.models.model_copy(
        update={
            "tiers": {
                **BASE_CFG.models.tiers,
                ModelTier.FAST: FAST.model_copy(update={"primary": PRICED}),
            }
        }
    )
    pipeline = BASE_CFG.pipeline.model_copy(
        update={"budgets": BudgetsConfig(run_budget_usd=budget_usd)}
    )
    return BASE_CFG.model_copy(update={"models": models, "pipeline": pipeline})


class Loader:
    """Point-in-time loader over the synthetic world; registers every partition it renders."""

    def __init__(self, *, leak: bool = False, securities: list[Security] | None = None) -> None:
        self.world = build_world()
        self.parts: dict[str, Partition] = {}
        self.leak, self.securities = leak, securities or _securities()

    def load(self, as_of: datetime) -> StepInputs:
        w = self.world
        entities: list[EntityData] = []
        for sid in (1, 2, 3, 4):
            e = replace(
                w.entity,
                security_id=sid,
                entity_token=w.tokens[sid],
                features=w.universe[sid - 1],
            )
            if not self.leak:  # a correct loader hands over only rows with available_at <= as_of
                e = replace(
                    e,
                    facts=[f for f in e.facts if f.available_at <= as_of],
                    insiders=[t for t in e.insiders if t.available_at <= as_of],
                    news=[n for n in e.news if n.available_at <= as_of],
                )
            entities.append(e)
        partitioner = Partitioner(
            as_of=as_of,
            masker=w.masker,
            universe=w.universe,
            sectors={sid: "Semiconductor equipment" for sid in w.tokens},
            regime=None,
        )
        for e in entities:
            for p in [*partitioner.build_all(e).values(), partitioner.red_team(e)]:
                self.parts[p.text] = p
        return StepInputs(
            securities=self.securities,
            brands=BRANDS,
            entities=entities,
            partitioner=partitioner,
            base_rates=dict.fromkeys(Horizon, 0.5),
        )


class RecordingSink:
    """In-memory stand-in for ``DecisionSink`` with its persistence semantics.

    ``fail_on`` makes a flush of that run status raise *before* anything is stored (an atomic
    rollback). Stored runs never regress from COMMITTED, and verdict rows are insert-only, as in
    ``store/write.py``.
    """

    def __init__(self, fail: bool = False, fail_on: RunStatus | None = None) -> None:
        self.steps: list[StepArtifacts] = []
        self.runs: dict[UUID, RunRecord] = {}
        self.verdicts: dict[UUID, dict[str, VerdictRecord]] = {}
        self.books: dict[UUID, ProposedBook] = {}
        self.fail_on = RunStatus.COMMITTED if fail else fail_on

    def flush_step(self, step: StepArtifacts) -> None:
        if step.run.status is self.fail_on:
            raise RuntimeError("db down")
        run = self.runs.get(step.run.run_id)
        if run is None or not (run.status in _ADVANCED and step.run.status not in _ADVANCED):
            self.runs[step.run.run_id] = step.run
        rows = self.verdicts.setdefault(step.run.run_id, {})
        for rec in step.verdicts:
            agent = (
                AgentName.RED_TEAM if isinstance(rec.verdict, RedTeamVerdict) else rec.verdict.agent
            )
            rows.setdefault(f"{agent.value}:{rec.security_id}", rec)
        if step.portfolio is not None:
            self.books[step.run.run_id] = step.portfolio.book
        self.steps.append(step)

    def load_run(self, run_id: UUID) -> RunRecord | None:
        return self.runs.get(run_id)

    def load_verdicts(self, run_id: UUID) -> list[VerdictRecord]:
        return list(self.verdicts.get(run_id, {}).values())

    def load_book(self, run_id: UUID) -> ProposedBook | None:
        return self.books.get(run_id)


_ADVANCED = (RunStatus.COMMITTED, RunStatus.ANCHORED, RunStatus.EXECUTED, RunStatus.SCORED)


class HasParts(Protocol):
    parts: dict[str, Partition]


class Provider:
    """respx responder: answers each request from the partition or CIO input it carries.

    ``dead`` names partitions whose calls always get a 429; ``cio`` picks the CIO reply:
    ``ok`` or ``garbage`` (not JSON, so it is repaired once and then discarded).
    """

    def __init__(
        self,
        loader: HasParts,
        *,
        rate_limit_first: int = 0,
        dead: Callable[[Partition], bool] | None = None,
        cio: str = "ok",
        usage: dict[str, Any] | None = None,
    ) -> None:
        self.loader, self.rate_limit = loader, rate_limit_first
        self.dead, self.cio, self.usage = dead, cio, USAGE if usage is None else usage
        self.requests: list[list[dict[str, str]]] = []
        self.agents: list[AgentName | None] = []  # per delivered request; None = the CIO
        self.attempts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if self.attempts <= self.rate_limit:
            return httpx.Response(429, headers={"retry-after": "0"})
        messages = json.loads(request.content)["messages"]
        user = messages[1]["content"]
        part = self.loader.parts.get(user)
        if part is not None and self.dead is not None and self.dead(part):
            return httpx.Response(429, headers={"retry-after": "0"})
        self.requests.append(messages)
        self.agents.append(part.agent if part is not None else None)
        if part is None and self.cio == "garbage":
            return httpx.Response(
                200,
                json={
                    "model": STRONG.primary.slug,
                    "choices": [{"message": {"content": "no json here"}}],
                    "usage": self.usage,
                },
            )
        if part is None:  # the CIO sees the proposed book, not a partition
            tokens = list(dict.fromkeys(re.findall(r"TICKER_\w+", user)))
            body: dict[str, Any] = {
                "decisions": [
                    {"entity_token": t, "action": "approve", "reason": ""} for t in tokens
                ],
                "rationale": "book is balanced",
            }
            return self._ok(STRONG.primary.slug, body)
        if part.agent is AgentName.RED_TEAM:
            body = {
                "bear_severity": "low",
                "falsifiable_risk": "margins compress",
                "horizon_days": 21,
                "key_evidence": [_cite(part)],
            }
            return self._ok(STRONG.primary.slug, body)
        return self._ok(PRICED.slug, json.loads(_value_json(part)))

    def _ok(self, model: str, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": model,
                "choices": [{"message": {"content": json.dumps(body)}}],
                "usage": self.usage,
            },
        )


def _client(entry: Any, out: Any) -> ChatClient:
    return OpenRouterClient(
        httpx.Client(),
        api_key="k",
        entry=entry,
        out_model=out,
        sleep=lambda _s: None,
        jitter=lambda: 1.0,
    )


class MemoryCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


def orchestrator(
    cfg: AppConfig,
    loader: Loader,
    sink: RecordingSink,
    *,
    concurrency: int = 4,
    cache: MemoryCache | None = None,
) -> BacktestOrchestrator:
    voter = _client(cfg.models.tiers[ModelTier.FAST], AgentVerdictLLM)
    red = _client(cfg.models.tiers[ModelTier.STRONG], RedTeamVerdictLLM)
    return BacktestOrchestrator(
        config=cfg,
        loader=loader,
        sink=sink,
        client_for=lambda a: red if a is AgentName.RED_TEAM else voter,
        cio_client=_client(cfg.models.tiers[ModelTier.STRONG], CioDecisionLLM),
        cache=cache,
        concurrency=concurrency,
    )


# --- tests -----------------------------------------------------------------------------------


def test_weekly_steps_are_inclusive_and_seven_days_apart() -> None:
    steps = list(weekly_steps(AS_OF, AS_OF + timedelta(days=14)))
    assert steps == [AS_OF, AS_OF + timedelta(days=7), AS_OF + timedelta(days=14)]
    with pytest.raises(ValueError):
        list(weekly_steps(datetime(2025, 1, 1), AS_OF))


@respx.mock
def test_multi_step_run_commits_one_atomic_flush_per_step() -> None:
    loader, sink = Loader(), RecordingSink()
    respx.post(URL).mock(side_effect=Provider(loader))
    results = orchestrator(cfg_with(), loader, sink).run_backtest(AS_OF, AS_OF + timedelta(days=14))

    assert [r.status for r in results] == [RunStatus.COMMITTED] * 3
    assert len(sink.steps) == 3  # exactly one flush (one transaction) per step
    assert len({r.run_id for r in results}) == 3
    for step in sink.steps:
        assert step.run.mode is RunMode.BACKTEST and step.run.status is RunStatus.COMMITTED
        assert step.commitment is not None and step.portfolio is not None  # invariant 5
        assert step.benchmark_replay_context is not None
        assert len(step.benchmark_replay_context.calibration_fits) == 3
        assert step.benchmark_replay_context.feature_set_versions
        assert len(step.verdicts) == 4 * 5 + 4  # 5 voters x 4 names + red team on all 4
        assert len(step.decisions) == 4 * 3  # 4 names x horizons 5/21/63
        book = step.portfolio.book
        assert book.positions and all(p.target_weight <= 0.08 for p in book.positions)
        assert step.portfolio.cash_weight == pytest.approx(1.0 - book.gross_exposure)
        weights = {p.security_id: p.target_weight for p in book.positions}
        for d in step.decisions:
            assert d.decision.target_weight == weights.get(d.decision.security_id, 0.0)
        assert all(
            v.verdict.model_served in {PRICED.slug, STRONG.primary.slug} for v in step.verdicts
        )
        assert step.run.total_cost_usd > 0


@respx.mock
def test_rate_limit_is_retried_and_step_still_commits() -> None:
    loader, sink = Loader(), RecordingSink()
    provider = Provider(loader, rate_limit_first=2)
    respx.post(URL).mock(side_effect=provider)
    result = orchestrator(cfg_with(), loader, sink, concurrency=1).run_step(AS_OF)
    assert result.status is RunStatus.COMMITTED
    assert provider.attempts == len(provider.requests) + 2
    assert not sink.steps[0].dlq


@respx.mock
def test_budget_breach_flushes_partial_with_dlq_and_stops_backtest() -> None:
    loader, sink = Loader(), RecordingSink()
    respx.post(URL).mock(side_effect=Provider(loader))
    orch = orchestrator(cfg_with(budget_usd=2.0), loader, sink, concurrency=1)
    results = orch.run_backtest(AS_OF, AS_OF + timedelta(days=14))

    assert len(results) == 1 and results[0].status is RunStatus.PARTIAL  # stepping stopped
    (step,) = sink.steps
    assert step.run.status is RunStatus.PARTIAL and step.run.status_reason
    assert step.commitment is None and step.portfolio is None and not step.decisions
    assert step.verdicts  # what was completed before the breach is kept
    assert any(d.error_type == "budget_exceeded" for d in step.dlq)
    assert step.run.total_cost_usd >= 2.0


@respx.mock
def test_prompts_never_contain_identity_strings() -> None:
    loader, sink = Loader(), RecordingSink()
    provider = Provider(loader)
    respx.post(URL).mock(side_effect=provider)
    orchestrator(cfg_with(), loader, sink).run_step(AS_OF)
    text = "\n".join(m["content"] for msgs in provider.requests for m in msgs).casefold()
    for word in IDENTITY:
        assert not re.search(rf"\b{re.escape(word.casefold())}\b", text), word


@respx.mock
def test_leaked_future_row_is_refused_before_any_call() -> None:
    loader, sink = Loader(leak=True), RecordingSink()
    route = respx.post(URL).mock(side_effect=Provider(loader))
    with pytest.raises(LookAheadError):
        orchestrator(cfg_with(), loader, sink).run_step(FUTURE - timedelta(days=1))
    assert route.call_count == 0 and not sink.steps


@respx.mock
def test_missing_brand_alias_halts_before_any_call() -> None:
    loader, sink = Loader(), RecordingSink()
    route = respx.post(URL).mock(side_effect=Provider(loader))
    orch = orchestrator(cfg_with(), loader, sink)
    loader.securities = [s.model_copy(update={"cik": 999}) for s in _securities()[:1]]
    with pytest.raises(AliasCoverageError):
        orch.run_step(AS_OF)
    assert route.call_count == 0 and not sink.steps


# --- run identity, resume and idempotency (invariant 8) -----------------------------------------


def _voter_and_red_calls(provider: Provider) -> int:
    return sum(1 for a in provider.agents if a is not None)


@respx.mock
def test_fresh_attempts_for_identical_mode_config_and_as_of_get_distinct_run_ids() -> None:
    loader, sink = Loader(), RecordingSink()
    respx.post(URL).mock(side_effect=Provider(loader))
    orch = orchestrator(cfg_with(), loader, sink)
    a, b = orch.run_step(AS_OF), orch.run_step(AS_OF)
    assert a.run_id != b.run_id
    assert a.status is b.status is RunStatus.COMMITTED
    assert {a.run_id, b.run_id} == set(sink.runs)  # two independent, fully stored runs


@respx.mock
def test_explicit_run_id_resumes_that_run_and_a_committed_run_short_circuits() -> None:
    loader, sink = Loader(), RecordingSink()
    provider = Provider(loader)
    route = respx.post(URL).mock(side_effect=provider)
    orch = orchestrator(cfg_with(), loader, sink)
    first = orch.run_step(AS_OF)
    calls = route.call_count

    again = orch.run_step(AS_OF, run_id=first.run_id)
    assert again.run_id == first.run_id and again.status is RunStatus.COMMITTED
    assert again.book == first.book
    assert route.call_count == calls  # nothing was re-run, nothing was flushed again
    assert len(sink.steps) == 1

    with pytest.raises(RunMismatchError):  # same id, different as_of: not the same run
        orch.run_step(AS_OF + timedelta(days=7), run_id=first.run_id)


@respx.mock
def test_completed_tasks_short_circuit_and_failed_tasks_retry_within_the_run() -> None:
    loader, sink = Loader(), RecordingSink()
    dead_name = loader.world.tokens[3]
    provider = Provider(
        loader,
        dead=lambda p: p.agent is AgentName.TECHNICAL and p.entity_token == dead_name,
    )
    respx.post(URL).mock(side_effect=provider)
    orch = orchestrator(cfg_with(), loader, sink, concurrency=1)

    partial = orch.run_step(AS_OF)
    assert partial.status is RunStatus.PARTIAL and partial.book is None
    stored = sink.verdicts[partial.run_id]
    assert f"{AgentName.TECHNICAL.value}:3" not in stored  # the dead-lettered task is not COMPLETED
    assert len(stored) == 4 * 5 - 1
    assert sink.steps[0].dlq

    provider.dead, provider.agents = None, []  # the provider recovers; resume the same run
    resumed = orch.run_step(AS_OF, run_id=partial.run_id)
    assert resumed.run_id == partial.run_id and resumed.status is RunStatus.COMMITTED
    ran = [a for a in provider.agents if a is not None]
    # only the failed task plus the red-team pass ran; the 19 stored voter verdicts did not
    assert ran.count(AgentName.TECHNICAL) == 1
    for agent in (AgentName.VALUE, AgentName.INSIDER, AgentName.QUALITY_CATALYST):
        assert agent not in ran
    assert ran.count(AgentName.RED_TEAM) == 4
    assert sink.runs[partial.run_id].status is RunStatus.COMMITTED
    assert len(sink.verdicts[partial.run_id]) == 4 * 5 + 4
    assert sink.steps[-1].commitment is not None


@respx.mock
def test_failed_old_run_cannot_block_a_fresh_run() -> None:
    loader, sink = Loader(), RecordingSink(fail_on=RunStatus.COMMITTED)
    respx.post(URL).mock(side_effect=Provider(loader))
    orch = orchestrator(cfg_with(), loader, sink)
    with pytest.raises(RuntimeError, match="db down"):
        orch.run_step(AS_OF)
    (old_id,) = sink.runs
    assert sink.runs[old_id].status is RunStatus.FAILED

    sink.fail_on = None  # the database is back; a new attempt for the same date and config
    fresh = orch.run_step(AS_OF)
    assert fresh.status is RunStatus.COMMITTED and fresh.run_id != old_id
    assert sink.runs[old_id].status is RunStatus.FAILED  # history is kept, never rewritten
    assert sink.runs[fresh.run_id].status is RunStatus.COMMITTED


# --- persistence, budget and CIO fail closed -----------------------------------------------------


@respx.mock
def test_persistence_failure_never_reports_or_records_a_commit() -> None:
    loader, sink = Loader(), RecordingSink(fail=True)
    provider = Provider(loader)
    respx.post(URL).mock(side_effect=provider)
    orch = orchestrator(cfg_with(), loader, sink)
    with pytest.raises(RuntimeError, match="db down"):
        orch.run_step(AS_OF)

    (run_id,) = sink.runs
    assert sink.runs[run_id].status is RunStatus.FAILED
    assert "db down" in (sink.runs[run_id].status_reason or "")
    assert not sink.books
    assert all(s.commitment is None and s.portfolio is None for s in sink.steps)
    assert len(sink.verdicts[run_id]) == 4 * 5 + 4  # completed work is kept, so a resume is cheap

    sink.fail_on = None
    calls = _voter_and_red_calls(provider)
    resumed = orch.run_step(AS_OF, run_id=run_id)
    assert resumed.status is RunStatus.COMMITTED and resumed.run_id == run_id
    assert _voter_and_red_calls(provider) == calls  # no voter or red-team call was repeated


@respx.mock
def test_a_failure_report_cannot_demote_a_committed_run() -> None:
    loader, sink = Loader(), RecordingSink()
    respx.post(URL).mock(side_effect=Provider(loader))
    result = orchestrator(cfg_with(), loader, sink).run_step(AS_OF)
    late = sink.runs[result.run_id].model_copy(update={"status": RunStatus.FAILED})
    sink.flush_step(StepArtifacts(run=late))
    assert sink.runs[result.run_id].status is RunStatus.COMMITTED


@respx.mock
def test_cio_discard_makes_the_run_partial_with_no_book_and_is_not_an_approval() -> None:
    loader, sink = Loader(), RecordingSink()
    respx.post(URL).mock(side_effect=Provider(loader, cio="garbage"))
    orch = orchestrator(cfg_with(), loader, sink)
    results = orch.run_backtest(AS_OF, AS_OF + timedelta(days=14))

    assert [r.status for r in results] == [RunStatus.PARTIAL]  # stepping stops
    assert results[0].book is None and results[0].reason == "CIO produced no valid decision"
    (step,) = sink.steps
    assert step.run.status is RunStatus.PARTIAL
    assert step.commitment is None and step.portfolio is None and not step.decisions
    assert not sink.books
    assert any(d.agent == AgentName.CIO.value for d in step.dlq)  # the failure is evidenced
    assert len(step.verdicts) == 4 * 5 + 4  # voter and red-team evidence persists


@respx.mock
def test_budget_exhaustion_is_partial_with_no_book_and_stops_remaining_calls() -> None:
    loader, sink = Loader(), RecordingSink()
    provider = Provider(loader)
    respx.post(URL).mock(side_effect=provider)
    orch = orchestrator(cfg_with(budget_usd=2.0), loader, sink, concurrency=1)
    result = orch.run_step(AS_OF)
    assert result.status is RunStatus.PARTIAL and result.book is None
    assert not sink.books and all(s.portfolio is None for s in sink.steps)
    assert len(provider.requests) < 4 * 5  # the remaining calls were never sent
    assert AgentName.RED_TEAM not in provider.agents and None not in provider.agents


# --- accounting is measured, never synthesized ---------------------------------------------------


@respx.mock
def test_verdict_accounting_is_the_measured_per_call_telemetry() -> None:
    loader, sink = Loader(), RecordingSink()
    respx.post(URL).mock(side_effect=Provider(loader))
    orchestrator(cfg_with(), loader, sink).run_step(AS_OF)
    (step,) = sink.steps
    voters = [r for r in step.verdicts if not isinstance(r.verdict, RedTeamVerdict)]
    assert voters and len(step.verdicts) == 4 * 5 + 4
    for rec in step.verdicts:
        assert (rec.tokens_in, rec.tokens_out) == (
            USAGE["prompt_tokens"],
            USAGE["completion_tokens"],
        )
        assert isinstance(rec.latency_ms, int)
    for rec in voters:
        assert rec.cost_usd == pytest.approx(CALL_USD)  # tokens x configured price, not 0
    assert sum(r.cost_usd for r in step.verdicts) <= step.run.total_cost_usd  # CIO is on the run


@respx.mock
def test_cache_hits_store_unknown_telemetry_and_zero_billing_not_fake_tokens() -> None:
    loader, sink, cache = Loader(), RecordingSink(), MemoryCache()
    respx.post(URL).mock(side_effect=Provider(loader))
    orch = orchestrator(cfg_with(), loader, sink, cache=cache)
    orch.run_step(AS_OF)
    second = orch.run_step(AS_OF)  # a fresh run, served from the cache
    recs = sink.steps[-1].verdicts
    assert sink.steps[-1].run.run_id == second.run_id and len(recs) == 4 * 5 + 4
    for rec in recs:
        assert (rec.tokens_in, rec.tokens_out, rec.latency_ms) == (None, None, None)
        assert rec.cost_usd == 0.0  # genuinely nothing billed for this run


@respx.mock
def test_missing_token_counts_are_stored_as_unknown_with_the_provider_cost() -> None:
    loader, sink = Loader(), RecordingSink()
    respx.post(URL).mock(side_effect=Provider(loader, usage={"cost": 0.25}))
    orchestrator(cfg_with(), loader, sink).run_step(AS_OF)
    for rec in sink.steps[0].verdicts:
        assert (rec.tokens_in, rec.tokens_out) == (None, None)
        assert rec.cost_usd == pytest.approx(0.25)


@respx.mock
def test_unusable_usage_fails_closed_instead_of_booking_zero() -> None:
    loader, sink = Loader(), RecordingSink()
    respx.post(URL).mock(side_effect=Provider(loader, usage={}))
    result = orchestrator(cfg_with(), loader, sink, concurrency=1).run_step(AS_OF)
    assert result.status is RunStatus.PARTIAL and result.book is None
    assert not sink.steps[0].verdicts  # nothing was stored with made-up accounting
