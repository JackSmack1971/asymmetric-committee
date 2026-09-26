"""Batch runner over a mocked chat client: fan-out, persistence, idempotency, budget, DLQ."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest

from agents.base import ChatResponse as Reply
from agents.base import Message, RunBudget, UnknownServedModelError
from agents.partitions import EntityData, Partition, Partitioner
from agents.runner import VOTING, RunnerResult, run_agents, task_key
from config.loader import load_config
from contracts.enums import AgentName, ModelTier, RunMode, RunStatus
from contracts.models import AgentVerdict, RedTeamVerdict
from prompts.agents import PROMPTS
from tests.agents.test_base import USAGE, _cite, _value_json
from tests.agents.test_openrouter import DictCache, ListDLQ
from tests.agents.world import build_world

CFG = load_config(allow_placeholders=True, env={})
FAST = CFG.models.tiers[ModelTier.FAST]
STRONG = CFG.models.tiers[ModelTier.STRONG]
PRICED = FAST.primary.model_copy(
    update={"input_price_usd_per_mtok": 1000.0, "output_price_usd_per_mtok": 2000.0}
)
PRICED_MODELS = CFG.models.model_copy(
    update={
        "tiers": {**CFG.models.tiers, ModelTier.FAST: FAST.model_copy(update={"primary": PRICED})}
    }
)
CALL_USD = PRICED.local_cost_usd(USAGE["prompt_tokens"], USAGE["completion_tokens"])
HASH = "c" * 64
N_VOTER_CALLS = 4 * len(VOTING)


class Fake:
    """Answers each partition with a valid verdict that cites one of its own rows."""

    def __init__(self, parts: dict[str, Partition], bad: set[str] | None = None) -> None:
        self.parts, self.bad = parts, bad or set()
        self.calls: list[str] = []

    def complete(self, messages: Sequence[Message]) -> Reply:
        part = self.parts[messages[1]["content"]]
        self.calls.append(f"{part.entity_token}:{part.agent.value}")
        if part.agent is AgentName.RED_TEAM:
            body = json.dumps(
                {
                    "bear_severity": "med",
                    "falsifiable_risk": "margins compress",
                    "horizon_days": 21,
                    "key_evidence": [_cite(part)],
                }
            )
            return Reply(STRONG.primary.slug, body, USAGE)
        if part.entity_token in self.bad:
            return Reply(PRICED.slug, "not json", USAGE)
        return Reply(PRICED.slug, _value_json(part), USAGE)


class Sink:
    def __init__(self) -> None:
        self.rows: list[AgentVerdict | RedTeamVerdict] = []

    def __call__(self, v: AgentVerdict | RedTeamVerdict) -> None:
        self.rows.append(v)


def _setup() -> tuple[list[EntityData], Partitioner, dict[str, Partition]]:
    world = build_world()
    entities = [
        replace(
            world.entity,
            security_id=sid,
            entity_token=world.tokens[sid],
            features=world.universe[sid - 1],
        )
        for sid in (1, 2, 3, 4)
    ]
    parts: dict[str, Partition] = {}
    for e in entities:
        parts |= {p.text: p for p in world.partitioner.build_all(e).values()}
        red = world.partitioner.red_team(e)
        parts[red.text] = red
    return entities, world.partitioner, parts


def _run(
    fake: Fake,
    *,
    run_id: UUID | None = None,
    red: Sequence[int] = (1, 2),
    **kw: Any,
) -> tuple[RunnerResult, Sink]:
    entities, partitioner, _ = _setup()
    sink = Sink()
    result = asyncio.run(
        run_agents(
            run_id=run_id or uuid4(),
            mode=RunMode.LIVE,
            models=PRICED_MODELS,
            config_hash=HASH,
            partitioner=partitioner,
            entities=entities,
            client_for=lambda _a: fake,
            sink=sink,
            red_team_ids=red,
            **kw,
        )
    )
    return result, sink


@pytest.fixture(scope="module")
def parts() -> dict[str, Partition]:
    return _setup()[2]


def test_fans_out_every_voter_per_entity_plus_red_team_for_candidates(
    parts: dict[str, Partition],
) -> None:
    fake = Fake(parts)
    result, sink = _run(fake)
    assert result.status is RunStatus.AGENTS_OK
    assert len(sink.rows) == N_VOTER_CALLS + 2 == len(result.completed) == len(fake.calls)
    reds = [v for v in sink.rows if isinstance(v, RedTeamVerdict)]
    assert {v.entity_token for v in reds} == {"TICKER_01", "TICKER_02"}
    assert all(v.model_served == STRONG.primary.slug for v in reds)
    assert all(v.prompt_version == PROMPTS[v.agent].version for v in sink.rows)
    assert result.cost_usd > 0


def test_rerun_skips_completed_tasks_only(parts: dict[str, Partition]) -> None:
    run_id = uuid4()
    first, _ = _run(Fake(parts, bad={"TICKER_03"}), run_id=run_id, red=())
    assert len(first.discarded) == len(VOTING)  # every TICKER_03 call failed, twice
    fake = Fake(parts)
    second, sink = _run(fake, run_id=run_id, red=(), completed=first.completed)
    assert second.skipped == first.completed
    assert len(fake.calls) == len(VOTING) and len(sink.rows) == len(VOTING)  # only the failed ones
    assert {v.entity_token for v in sink.rows} == {"TICKER_03"}


def test_task_keys_are_run_scoped() -> None:
    a, b = uuid4(), uuid4()
    assert task_key(a, AgentName.VALUE, 1) != task_key(b, AgentName.VALUE, 1)
    assert task_key(a, AgentName.VALUE, 1) != task_key(a, AgentName.INSIDER, 1)


def test_repair_failure_is_discarded_not_persisted_and_not_partial(
    parts: dict[str, Partition],
) -> None:
    result, sink = _run(Fake(parts, bad={"TICKER_02"}), red=())
    assert len(result.discarded) == len(VOTING) and result.status is RunStatus.AGENTS_OK
    assert all(v.entity_token != "TICKER_02" for v in sink.rows)


def test_budget_exceeded_halts_dispatch_routes_rest_to_dlq_and_marks_partial(
    parts: dict[str, Partition],
) -> None:
    fake, dlq = Fake(parts), ListDLQ()
    result, sink = _run(fake, red=(), budget=RunBudget(CALL_USD * 2.5), dlq=dlq, concurrency=1)
    assert result.status is RunStatus.PARTIAL
    assert len(fake.calls) == 3  # the third call books the ceiling; nothing is sent after it
    assert len(sink.rows) == 2  # the over-budget call's verdict is not kept
    assert len(result.aborted) + len(result.completed) == N_VOTER_CALLS
    assert len(dlq.records) == N_VOTER_CALLS - 2
    assert {r["error"] for r in dlq.records} == {"budget_exceeded", "run_aborted"}
    assert all(r["prompt_version"] for r in dlq.records)


def test_unknown_served_model_aborts_the_run_as_partial(parts: dict[str, Partition]) -> None:
    class Rogue(Fake):
        def complete(self, messages: Sequence[Message]) -> Reply:
            return Reply("nobody/unlisted", "{}", USAGE)

    dlq = ListDLQ()
    result, sink = _run(Rogue(parts), red=(), dlq=dlq, concurrency=1)
    assert result.status is RunStatus.PARTIAL and sink.rows == []
    assert UnknownServedModelError.__name__ in result.aborted.values()
    assert len(dlq.records) == N_VOTER_CALLS


def test_cache_hits_are_not_recalled(parts: dict[str, Partition]) -> None:
    cache, fake = DictCache(), Fake(parts)
    _run(fake, red=(), cache=cache)
    n = len(fake.calls)
    result, _ = _run(fake, red=(), cache=cache)
    assert len(fake.calls) == n and result.cache_hits == N_VOTER_CALLS


def test_red_team_candidate_outside_snapshot_is_rejected(parts: dict[str, Partition]) -> None:
    with pytest.raises(ValueError, match="not in the snapshot"):
        _run(Fake(parts), red=(99,))
