"""BacktestOrchestrator over mocked OpenRouter (respx) and a recording sink (P5 step 3)."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

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
    RedTeamVerdictLLM,
    StepArtifacts,
)
from orchestration.pipeline import (
    BacktestOrchestrator,
    LookAheadError,
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
    def __init__(self, fail: bool = False) -> None:
        self.steps: list[StepArtifacts] = []
        self.fail = fail

    def flush_step(self, step: StepArtifacts) -> None:
        if self.fail:
            raise RuntimeError("db down")
        self.steps.append(step)


class Provider:
    """respx responder: answers each request from the partition or CIO input it carries."""

    def __init__(self, loader: Loader, *, rate_limit_first: int = 0) -> None:
        self.loader, self.rate_limit = loader, rate_limit_first
        self.requests: list[list[dict[str, str]]] = []
        self.attempts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if self.attempts <= self.rate_limit:
            return httpx.Response(429, headers={"retry-after": "0"})
        messages = json.loads(request.content)["messages"]
        self.requests.append(messages)
        user = messages[1]["content"]
        part = self.loader.parts.get(user)
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

    @staticmethod
    def _ok(model: str, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": model,
                "choices": [{"message": {"content": json.dumps(body)}}],
                "usage": USAGE,
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


def orchestrator(
    cfg: AppConfig, loader: Loader, sink: RecordingSink, *, concurrency: int = 4
) -> BacktestOrchestrator:
    voter = _client(cfg.models.tiers[ModelTier.FAST], AgentVerdictLLM)
    red = _client(cfg.models.tiers[ModelTier.STRONG], RedTeamVerdictLLM)
    return BacktestOrchestrator(
        config=cfg,
        loader=loader,
        sink=sink,
        client_for=lambda a: red if a is AgentName.RED_TEAM else voter,
        cio_client=_client(cfg.models.tiers[ModelTier.STRONG], CioDecisionLLM),
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
def test_rerun_of_a_step_reuses_the_run_id() -> None:
    loader, sink = Loader(), RecordingSink()
    respx.post(URL).mock(side_effect=Provider(loader))
    orch = orchestrator(cfg_with(), loader, sink)
    a, b = orch.run_step(AS_OF), orch.run_step(AS_OF)
    assert a.run_id == b.run_id


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


@respx.mock
def test_sink_failure_propagates_and_nothing_is_committed() -> None:
    loader, sink = Loader(), RecordingSink(fail=True)
    respx.post(URL).mock(side_effect=Provider(loader))
    with pytest.raises(RuntimeError, match="db down"):
        orchestrator(cfg_with(), loader, sink).run_step(AS_OF)
    assert not sink.steps
