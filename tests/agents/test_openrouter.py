"""OpenRouter transport, backoff, cache and dead-letter queue against mocked HTTP (§10.2, §13)."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
import respx

from agents.base import (
    AgentCallResult,
    DiscardReason,
    LLMTransientError,
    cache_key,
    run_agent_call,
)
from agents.llm.openrouter import (
    BASE_URL,
    MAX_RETRIES,
    LLMRequestError,
    OpenRouterClient,
    build_payload,
)
from agents.partitions import Partition
from config.loader import load_config
from contracts.enums import AgentName, ModelTier, ReasoningEffort, RunMode
from contracts.models import AgentVerdictLLM, RedTeamVerdictLLM
from tests.agents.test_base import USAGE, _value_json
from tests.agents.world import build_world

CFG = load_config(allow_placeholders=True, env={})
ENTRY = CFG.models.tiers[ModelTier.FAST]
URL = f"{BASE_URL}/chat/completions"
MESSAGES = [{"role": "system", "content": "rubric"}, {"role": "user", "content": "table"}]


@pytest.fixture(scope="module")
def part() -> Partition:
    world = build_world()
    return world.partitioner.value(world.entity)


def ok_body(content: str, model: str | None = None) -> dict[str, object]:
    return {
        "model": model or ENTRY.primary.slug,
        "choices": [{"message": {"content": content}}],
        "usage": USAGE,
    }


def client(sleeps: list[float] | None = None, limiter: object | None = None) -> OpenRouterClient:
    log = sleeps if sleeps is not None else []
    return OpenRouterClient(
        httpx.Client(),
        api_key="k",
        entry=ENTRY,
        out_model=AgentVerdictLLM,
        limiter=limiter,  # type: ignore[arg-type]
        sleep=log.append,
        jitter=lambda: 1.0,
    )


# --- payload ---------------------------------------------------------------------------------


def test_payload_matches_spec() -> None:
    p = build_payload(ENTRY, AgentVerdictLLM, MESSAGES)
    assert p["models"] == list(ENTRY.all_slugs)
    assert p["provider"] == {"require_parameters": True, "data_collection": "deny"}
    rf = p["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "AgentVerdictLLM"
    props = rf["json_schema"]["schema"]["properties"]
    assert {"p_outperform_5", "p_outperform_21", "p_outperform_63"} <= set(props)
    assert "p_outperform" not in props and "horizon_days" not in props
    assert p["temperature"] == 0.0 and "reasoning" not in p


def test_payload_omits_temperature_and_sets_reasoning() -> None:
    reasoning = ENTRY.primary.model_copy(
        update={"accepts_temperature": False, "reasoning_effort": ReasoningEffort.HIGH}
    )
    p = build_payload(ENTRY.model_copy(update={"primary": reasoning}), RedTeamVerdictLLM, MESSAGES)
    assert "temperature" not in p and p["reasoning"] == {"effort": "high"}


def test_payload_has_no_weight_field() -> None:
    blob = json.dumps(build_payload(ENTRY, AgentVerdictLLM, MESSAGES)).lower()
    assert "weight" not in blob


# --- transport -------------------------------------------------------------------------------


@respx.mock
def test_fallback_served_model_is_returned() -> None:
    route = respx.post(URL).respond(200, json=ok_body("{}", model=ENTRY.fallbacks[0].slug))
    resp = client().complete(MESSAGES)
    assert resp.model == ENTRY.fallbacks[0].slug != ENTRY.primary.slug
    sent = json.loads(route.calls[0].request.content)
    assert sent["models"][0] == ENTRY.primary.slug
    assert route.calls[0].request.headers["authorization"] == "Bearer k"


@respx.mock
def test_retries_429_with_exponential_backoff_then_succeeds() -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(429),
            httpx.Response(200, json=ok_body("{}")),
        ]
    )
    sleeps: list[float] = []
    assert client(sleeps).complete(MESSAGES).content == "{}"
    assert route.call_count == 3
    assert sleeps == [1.0, 2.0]  # jitter pinned to 1.0 -> the full ceiling


@respx.mock
def test_retry_after_header_is_honoured_and_capped() -> None:
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "7"}),
            httpx.Response(429, headers={"retry-after": "9999"}),
            httpx.Response(200, json=ok_body("{}")),
        ]
    )
    sleeps: list[float] = []
    client(sleeps).complete(MESSAGES)
    assert sleeps == [7.0, 30.0]


@respx.mock
def test_gives_up_after_three_retries() -> None:
    route = respx.post(URL).respond(429)
    sleeps: list[float] = []
    with pytest.raises(LLMTransientError):
        client(sleeps).complete(MESSAGES)
    assert route.call_count == MAX_RETRIES + 1 and len(sleeps) == MAX_RETRIES


@respx.mock
def test_transport_error_and_5xx_are_retried() -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.ConnectError("boom"),
            httpx.Response(503),
            httpx.Response(200, json=ok_body("{}")),
        ]
    )
    assert client().complete(MESSAGES).content == "{}"
    assert route.call_count == 3


@respx.mock
def test_upstream_error_inside_200_is_retried() -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(200, json={"error": {"code": 429, "message": "slow"}}),
            httpx.Response(200, json=ok_body("{}")),
        ]
    )
    assert client().complete(MESSAGES).content == "{}"
    assert route.call_count == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": "bad key"}),
        httpx.Response(400),
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"error": {"code": 400, "message": "no"}}),
    ],
)
@respx.mock
def test_hard_errors_are_not_retried(response: httpx.Response) -> None:
    route = respx.post(URL).mock(return_value=response)
    with pytest.raises(LLMRequestError):
        client().complete(MESSAGES)
    assert route.call_count == 1


def test_empty_api_key_rejected() -> None:
    with pytest.raises(LLMRequestError):
        OpenRouterClient(httpx.Client(), api_key="", entry=ENTRY, out_model=AgentVerdictLLM)


@respx.mock
def test_limiter_is_charged_on_every_attempt() -> None:
    class Spy:
        def __init__(self) -> None:
            self.costs: list[int] = []

        def acquire(self, est_tokens: int) -> None:
            self.costs.append(est_tokens)

    spy = Spy()
    respx.post(URL).mock(side_effect=[httpx.Response(429), httpx.Response(200, json=ok_body("{}"))])
    client(limiter=spy).complete(MESSAGES)
    assert len(spy.costs) == 2 and all(c > 0 for c in spy.costs)


# --- cache, DLQ and the 429 storm ------------------------------------------------------------


class DictCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


class ListDLQ:
    def __init__(self) -> None:
        self.records: list[dict[str, str]] = []

    def push(self, record: dict[str, str]) -> None:
        self.records.append(record)


def call(part: Partition, c: OpenRouterClient, **kw: object) -> AgentCallResult:
    args: dict[str, object] = dict(
        models=CFG.models,
        mode=RunMode.LIVE,
        run_id=uuid4(),
        agent=AgentName.VALUE,
        partition=part,
        system_prompt="rubric",
        prompt_version="v1",
        requested_model=ENTRY.primary.slug,
        config_hash="c" * 64,
    )
    return run_agent_call(c, **{**args, **kw})  # type: ignore[arg-type]


@respx.mock
def test_identical_partition_is_never_rebilled(part: Partition) -> None:
    route = respx.post(URL).respond(200, json=ok_body(_value_json(part)))
    cache = DictCache()
    first = call(part, client(), cache=cache)
    second = call(part, client(), cache=cache)
    assert route.call_count == 1
    assert not first.cache_hit and second.cache_hit and second.cost_usd == 0.0
    assert first.verdict is not None and second.verdict is not None
    assert second.verdict.model_served == first.verdict.model_served
    assert second.verdict.run_id != first.verdict.run_id  # envelope is per run


@respx.mock
def test_cache_hit_recomputes_valid_for_this_runs_clock(part: Partition) -> None:
    respx.post(URL).respond(200, json=ok_body(_value_json(part)))
    cache = DictCache()
    call(part, client(), cache=cache)  # LIVE: valid
    late = ENTRY.primary.model_copy(update={"stated_training_cutoff": part.as_of.date()})
    models = CFG.models.model_copy(
        update={
            "tiers": {
                **CFG.models.tiers,
                ModelTier.FAST: ENTRY.model_copy(update={"primary": late}),
            }
        }
    )
    hit = call(part, client(), cache=cache, models=models, mode=RunMode.BACKTEST)
    assert hit.cache_hit and hit.verdict is not None and hit.verdict.valid is False


@respx.mock
def test_cache_key_varies_with_each_component(part: Partition) -> None:
    base = cache_key(AgentName.VALUE, "v1", "m", "c", "h")
    assert (
        len(
            {
                base,
                cache_key(AgentName.INSIDER, "v1", "m", "c", "h"),
                cache_key(AgentName.VALUE, "v2", "m", "c", "h"),
                cache_key(AgentName.VALUE, "v1", "m2", "c", "h"),
                cache_key(AgentName.VALUE, "v1", "m", "c", "h2"),
                cache_key(AgentName.VALUE, "v1", "m", "c2", "h"),
            }
        )
        == 6
    )


@respx.mock
def test_bad_verdicts_are_not_cached(part: Partition) -> None:
    respx.post(URL).respond(200, json=ok_body("not json"))
    cache = DictCache()
    assert call(part, client(), cache=cache).verdict is None
    assert cache.data == {}


@respx.mock
def test_corrupt_cache_entry_is_a_miss(part: Partition) -> None:
    route = respx.post(URL).respond(200, json=ok_body(_value_json(part)))
    cache = DictCache()
    key = cache_key(AgentName.VALUE, "v1", ENTRY.primary.slug, "c" * 64, part.input_hash)
    cache.data[key] = "{garbage"
    assert call(part, client(), cache=cache).verdict is not None
    assert route.call_count == 1


def test_cache_requires_requested_model(part: Partition) -> None:
    with pytest.raises(ValueError, match="config_hash"):
        call(part, client(), cache=DictCache(), requested_model="")


@respx.mock
def test_exhausted_retries_go_to_dlq(part: Partition) -> None:
    respx.post(URL).respond(429)
    dlq = ListDLQ()
    result = call(part, client(), dlq=dlq)
    assert result.verdict is None and result.discard_reason is DiscardReason.DEAD_LETTERED
    assert len(dlq.records) == 1
    assert dlq.records[0]["agent"] == "value" and dlq.records[0]["entity_token"]


@respx.mock
def test_429_storm_completes_without_dropping_requests(part: Partition) -> None:
    """Scaled-down §13 storm: 240 calls (40 names x 6); each one is throttled twice first."""
    attempts: dict[str, int] = {}

    def storm(request: httpx.Request) -> httpx.Response:
        tag = request.headers["x-call"]
        attempts[tag] = attempts.get(tag, 0) + 1
        if attempts[tag] <= 2:
            return httpx.Response(429)
        return httpx.Response(200, json=ok_body(_value_json(part)))

    respx.post(URL).mock(side_effect=storm)
    sleeps: list[float] = []
    results = []
    for n in range(240):
        c = client(sleeps)
        c._http.headers["x-call"] = str(n)
        results.append(call(part, c, dlq=ListDLQ()))
    assert all(r.verdict is not None for r in results)  # nothing dropped
    assert len(sleeps) == 480  # two backoffs per call, none unbounded
    assert max(sleeps) <= 30.0


@respx.mock
def test_total_outage_marks_every_call_dead_lettered(part: Partition) -> None:
    respx.post(URL).respond(429)
    dlq = ListDLQ()
    results = [call(part, client(), dlq=dlq) for _ in range(50)]
    assert all(r.discard_reason is DiscardReason.DEAD_LETTERED for r in results)
    assert len(dlq.records) == 50
