"""Phase 3 end to end: partition, cache, mocked OpenRouter, verdict, DLQ and budget (§10)."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import respx

from agents.base import (
    AgentCallResult,
    BudgetExceededError,
    DiscardReason,
    RunBudget,
    run_agent_call,
)
from agents.llm.openrouter import BASE_URL, OpenRouterClient
from agents.partitions import Partition
from config.loader import load_config
from contracts.enums import AgentName, ModelTier, RunMode
from contracts.models import AgentVerdict, AgentVerdictLLM
from tests.agents.test_base import USAGE, _value_json
from tests.agents.test_openrouter import DictCache, ListDLQ
from tests.agents.world import build_world

CFG = load_config(allow_placeholders=True, env={})
ENTRY = CFG.models.tiers[ModelTier.FAST]
PRICED = ENTRY.primary.model_copy(
    update={"input_price_usd_per_mtok": 1000.0, "output_price_usd_per_mtok": 2000.0}
)
CALL_USD = PRICED.local_cost_usd(USAGE["prompt_tokens"], USAGE["completion_tokens"])  # $1.40
URL = f"{BASE_URL}/chat/completions"
HASH_A, HASH_B = "a" * 64, "b" * 64


@pytest.fixture(scope="module")
def part() -> Partition:
    world = build_world()
    return world.partitioner.value(world.entity)


def _body(content: str) -> dict[str, object]:
    return {
        "model": PRICED.slug,
        "choices": [{"message": {"content": content}}],
        "usage": USAGE,
    }


def _client() -> OpenRouterClient:
    return OpenRouterClient(
        httpx.Client(),
        api_key="k",
        entry=ENTRY,
        out_model=AgentVerdictLLM,
        sleep=lambda _s: None,
        jitter=lambda: 1.0,
    )


def _call(part: Partition, **kw: object) -> AgentCallResult:
    args: dict[str, object] = dict(
        models=CFG.models.model_copy(
            update={
                "tiers": {
                    **CFG.models.tiers,
                    ModelTier.FAST: ENTRY.model_copy(update={"primary": PRICED}),
                }
            }
        ),
        mode=RunMode.LIVE,
        run_id=uuid4(),
        agent=AgentName.VALUE,
        partition=part,
        system_prompt="rubric",
        prompt_version="v1",
        requested_model=PRICED.slug,
        config_hash=HASH_A,
    )
    return run_agent_call(_client(), **{**args, **kw})  # type: ignore[arg-type]


@respx.mock
def test_e2e_partition_to_verdict_success(part: Partition) -> None:
    respx.post(URL).respond(200, json=_body(_value_json(part)))
    run_id = uuid4()
    result = _call(part, run_id=run_id, mode=RunMode.BACKTEST)
    verdict = result.verdict
    assert isinstance(verdict, AgentVerdict)
    assert verdict.run_id == run_id and verdict.model_served == PRICED.slug
    assert verdict.valid is True  # as_of is well past the placeholder cutoff + 60d
    assert result.cost_usd == pytest.approx(CALL_USD) and result.cost_usd > 0
    cited = {(e.source, e.row_id) for e in verdict.key_evidence}
    assert cited and cited <= set(part.evidence)


@respx.mock
def test_e2e_citation_repair_flow(part: Partition) -> None:
    bad = _value_json(part, [{"source": "fundamentals", "row_id": "FIN_Q99", "note": "x"}])
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(200, json=_body(bad)),
            httpx.Response(200, json=_body(_value_json(part))),
        ]
    )
    result = _call(part)
    assert result.verdict is not None and result.calls == 2 and route.call_count == 2
    assert b"FIN_Q99" in route.calls[1].request.content  # explicit correction prompt
    assert result.cost_usd == pytest.approx(2 * CALL_USD)


@respx.mock
def test_e2e_citation_discard_to_dlq(part: Partition) -> None:
    bad = _value_json(part, [{"source": "fundamentals", "row_id": "FIN_Q99", "note": "x"}])
    route = respx.post(URL).respond(200, json=_body(bad))
    cache, dlq = DictCache(), ListDLQ()
    result = _call(part, cache=cache, dlq=dlq)
    assert result.verdict is None
    assert result.discard_reason is DiscardReason.UNPROVIDED_EVIDENCE
    assert route.call_count == 2 and cache.data == {}  # one repair, never cached


@respx.mock
def test_e2e_cache_key_config_hash_isolation(part: Partition) -> None:
    route = respx.post(URL).respond(200, json=_body(_value_json(part)))
    cache = DictCache()
    assert not _call(part, cache=cache, config_hash=HASH_A).cache_hit
    assert _call(part, cache=cache, config_hash=HASH_A).cache_hit
    assert route.call_count == 1
    assert not _call(part, cache=cache, config_hash=HASH_B).cache_hit
    assert route.call_count == 2


@respx.mock
def test_e2e_budget_cap_circuit_breaker(part: Partition) -> None:
    route = respx.post(URL).respond(200, json=_body(_value_json(part)))
    budget, dlq = RunBudget(CALL_USD * 1.5), ListDLQ()
    assert _call(part, budget=budget, dlq=dlq).verdict is not None  # $1.40 < $2.10
    with pytest.raises(BudgetExceededError):
        _call(part, budget=budget, dlq=dlq)  # $2.80 >= $2.10: booked, then halted
    assert route.call_count == 2 and budget.spent_usd == pytest.approx(2 * CALL_USD)
    with pytest.raises(BudgetExceededError):
        _call(part, budget=budget, dlq=dlq)  # suppressed: no request is sent
    assert route.call_count == 2
    assert [r["error"] for r in dlq.records] == ["budget_exceeded"] * 2
