"""Batch runner: fans one monthly snapshot out to (security x agent) calls (§3, §10.2).

Universe loading, feature reads and persistence stay with the caller (invariant 2: agent code has
no database access). The runner gets ready ``EntityData`` and a ``sink`` that stores each verdict
(P5 wires it to ``agent_verdicts``). Calls are blocking ``ChatClient`` calls run in threads; the
Redis token bucket inside the client paces them, and ``concurrency`` only bounds thread use.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from agents.base import (
    AgentCallResult,
    BudgetExceededError,
    ChatClient,
    DeadLetterQueue,
    RunBudget,
    UnknownServedModelError,
    VerdictCache,
    run_agent_call,
)
from agents.partitions import EntityData, Partition, Partitioner
from config.loader import ModelsConfig, UsageUnavailableError
from contracts.enums import AgentName, ModelTier, RunMode, RunStatus
from contracts.models import AgentVerdict, RedTeamVerdict
from prompts.agents import PROMPTS

VOTING: tuple[AgentName, ...] = (
    AgentName.VALUE,
    AgentName.QUALITY_CATALYST,
    AgentName.INSIDER,
    AgentName.TECHNICAL,
    AgentName.MACRO_NARRATIVE,
)
# Failures that end the run early; everything not yet called goes to the DLQ (run is PARTIAL).
_ABORTING = (BudgetExceededError, UsageUnavailableError, UnknownServedModelError)


def task_key(run_id: UUID, stage: AgentName, security_id: int) -> str:
    """Invariant 8: ``(run_id, stage, security_id)``. Only ``COMPLETED`` tasks short-circuit."""
    return f"{run_id}:{stage.value}:{security_id}"


def tier_for(agent: AgentName) -> ModelTier:
    return ModelTier.STRONG if agent is AgentName.RED_TEAM else ModelTier.FAST


@dataclass
class RunnerResult:
    status: RunStatus
    completed: set[str] = field(default_factory=set)  # task keys finished this call
    skipped: set[str] = field(default_factory=set)  # already COMPLETED, not re-run
    discarded: dict[str, str] = field(default_factory=dict)  # task key -> DiscardReason
    aborted: dict[str, str] = field(default_factory=dict)  # task key -> error class name
    cost_usd: float = 0.0
    cache_hits: int = 0


Sink = Callable[[AgentVerdict | RedTeamVerdict], None]


async def run_agents(
    *,
    run_id: UUID,
    mode: RunMode,
    models: ModelsConfig,
    config_hash: str,
    partitioner: Partitioner,
    entities: Sequence[EntityData],
    client_for: Callable[[AgentName], ChatClient],
    sink: Sink,
    red_team_ids: Collection[int] = (),
    completed: Iterable[str] = (),
    cache: VerdictCache | None = None,
    dlq: DeadLetterQueue | None = None,
    budget: RunBudget | None = None,
    concurrency: int = 16,
) -> RunnerResult:
    """Run every voting agent for every entity, plus the red team for ``red_team_ids``.

    ``completed`` holds task keys already COMPLETED for this run; they are skipped. A call that is
    discarded, dead-lettered or aborted is not COMPLETED and runs again on resume.
    """
    done, result = set(completed), RunnerResult(RunStatus.AGENTS_OK)
    stop = asyncio.Event()
    gate = asyncio.Semaphore(concurrency)
    unknown = set(red_team_ids) - {e.security_id for e in entities}
    if unknown:
        raise ValueError(f"red team candidates not in the snapshot: {sorted(unknown)}")

    jobs: list[tuple[AgentName, EntityData, Partition]] = []
    for e in entities:
        parts = partitioner.build_all(e)
        jobs += [(a, e, parts[a]) for a in VOTING]
        if e.security_id in red_team_ids:
            jobs.append((AgentName.RED_TEAM, e, partitioner.red_team(e)))

    def dead_letter(agent: AgentName, e: EntityData, part: Partition, error: str) -> None:
        if dlq is not None:
            dlq.push(
                {
                    "run_id": str(run_id),
                    "agent": agent.value,
                    "entity_token": part.entity_token,
                    "prompt_version": PROMPTS[agent].version,
                    "error": error,
                }
            )

    async def one(agent: AgentName, e: EntityData, part: Partition) -> None:
        key = task_key(run_id, agent, e.security_id)
        if key in done:
            result.skipped.add(key)
            return
        if stop.is_set():
            dead_letter(agent, e, part, "run_aborted")
            result.aborted[key] = "run_aborted"
            return
        prompt = PROMPTS[agent]
        async with gate:
            if stop.is_set():
                dead_letter(agent, e, part, "run_aborted")
                result.aborted[key] = "run_aborted"
                return
            try:
                call: AgentCallResult = await asyncio.to_thread(
                    run_agent_call,
                    client_for(agent),
                    models=models,
                    mode=mode,
                    run_id=run_id,
                    agent=agent,
                    partition=part,
                    system_prompt=prompt.system,
                    prompt_version=prompt.version,
                    requested_model=models.tiers[tier_for(agent)].primary.slug,
                    config_hash=config_hash,
                    cache=cache,
                    dlq=dlq,
                    budget=budget,
                )
            except _ABORTING as exc:
                stop.set()
                result.aborted[key] = type(exc).__name__
                if not isinstance(exc, BudgetExceededError):  # budget path already dead-lettered
                    dead_letter(agent, e, part, type(exc).__name__)
                return
        result.cost_usd += call.cost_usd
        result.cache_hits += call.cache_hit
        if call.verdict is None:
            reason = call.discard_reason.value if call.discard_reason else "discarded"
            result.discarded[key] = reason
            return
        sink(call.verdict)
        result.completed.add(key)

    await asyncio.gather(*(one(*j) for j in jobs))
    if result.aborted or any(r == "dead_lettered" for r in result.discarded.values()):
        result.status = RunStatus.PARTIAL
    return result
