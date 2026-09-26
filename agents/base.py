"""Turns one partition into one stored verdict (§3.1, §10.2).

This is the only module that builds an ``AgentVerdict``/``RedTeamVerdict`` from LLM output and the
only place ``valid`` is computed. Transport (OpenRouter HTTP, cache, rate limiting) sits behind the
``ChatClient`` protocol and lands in later P3 steps, so everything here is testable with a fake.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError

from agents.partitions import Partition
from config.loader import ModelsConfig, ServedModel
from contracts.enums import AgentName, RunMode
from contracts.models import (
    AgentVerdict,
    AgentVerdictLLM,
    EvidenceRef,
    RedTeamVerdict,
    RedTeamVerdictLLM,
)

# §12.1 item 1: a backtest window is valid only strictly after the effective cutoff plus 60 days.
CUTOFF_BUFFER = timedelta(days=60)

Message = dict[str, str]


class UnknownServedModelError(RuntimeError):
    """``response.model`` is not in ``config/models.yaml``: cost and cutoff are unknowable."""


@dataclass(frozen=True)
class ChatResponse:
    model: str  # response.model: the model that actually answered (invariant 7)
    content: str  # the JSON text of the structured output
    usage: dict[str, Any] | None


class ChatClient(Protocol):
    def complete(self, messages: Sequence[Message]) -> ChatResponse: ...


class DiscardReason(StrEnum):
    UNPARSEABLE = "unparseable"
    UNPROVIDED_EVIDENCE = "unprovided_evidence"


@dataclass(frozen=True)
class AgentCallResult:
    """A verdict, or ``None`` when discarded as *invalid* (a failed call, never persisted)."""

    verdict: AgentVerdict | RedTeamVerdict | None
    cost_usd: float
    calls: int
    discard_reason: DiscardReason | None = None


def verdict_valid(mode: RunMode, as_of: datetime, served: ServedModel) -> bool:
    """Backtest validity of a verdict from the served model's effective cutoff (§3.1)."""
    if mode is not RunMode.BACKTEST:
        return True
    return as_of.astimezone(UTC).date() > served.effective_cutoff + CUTOFF_BUFFER


def unprovided_evidence(
    evidence: Sequence[EvidenceRef], partition: Partition
) -> tuple[EvidenceRef, ...]:
    """Citations that do not name a row the agent was actually shown."""
    return tuple(e for e in evidence if (e.source, e.row_id) not in partition.evidence)


def _served_model(models: ModelsConfig, response_model: str) -> ServedModel:
    try:
        return models.model_for_response(response_model)
    except ValueError as e:
        raise UnknownServedModelError(str(e)) from e


def _parse(
    out_model: type[AgentVerdictLLM] | type[RedTeamVerdictLLM], content: str
) -> AgentVerdictLLM | RedTeamVerdictLLM | str:
    """The parsed output, or an error description for the repair prompt."""
    try:
        return out_model.model_validate(json.loads(content))
    except (json.JSONDecodeError, ValidationError) as e:
        return f"output did not match the schema: {e}"


def _repair_prompt(problem: str) -> str:
    return (
        f"Your previous reply was rejected: {problem}. Reply again with JSON that matches the "
        "schema, citing only evidence_id values that appear in the input table."
    )


def run_agent_call(
    client: ChatClient,
    *,
    models: ModelsConfig,
    mode: RunMode,
    run_id: UUID,
    agent: AgentName,
    partition: Partition,
    system_prompt: str,
    prompt_version: str,
) -> AgentCallResult:
    """One call plus at most one repair attempt (§10.2 Validation).

    Pydantic parse, then the evidence check. A second failure discards the call. Cost accrues on
    every call, repaired or not. ``UsageUnavailableError``/``UnknownServedModelError`` propagate so
    the caller can abort the run as ``PARTIAL`` instead of booking $0.
    """
    if partition.agent is not agent:
        raise ValueError(f"partition is for {partition.agent.value}, not {agent.value}")
    out_model = RedTeamVerdictLLM if agent is AgentName.RED_TEAM else AgentVerdictLLM
    messages: list[Message] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": partition.text},
    ]
    cost = 0.0
    reason = DiscardReason.UNPARSEABLE
    for attempt in range(2):
        response = client.complete(messages)
        served = _served_model(models, response.model)
        cost += served.call_cost(response.usage).usd
        parsed = _parse(out_model, response.content)
        if isinstance(parsed, str):
            problem, reason = parsed, DiscardReason.UNPARSEABLE
        else:
            missing = unprovided_evidence(parsed.key_evidence, partition)
            if not missing:
                return AgentCallResult(
                    _envelope(
                        parsed,
                        run_id=run_id,
                        agent=agent,
                        partition=partition,
                        prompt_version=prompt_version,
                        served=served,
                        valid=verdict_valid(mode, partition.as_of, served),
                    ),
                    cost,
                    attempt + 1,
                )
            ids = ", ".join(f"{e.source.value}:{e.row_id}" for e in missing)
            problem, reason = (
                f"cited evidence not in the input: {ids}",
                (DiscardReason.UNPROVIDED_EVIDENCE),
            )
        messages = [
            *messages,
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": _repair_prompt(problem)},
        ]
    return AgentCallResult(None, cost, 2, reason)


def _envelope(
    out: AgentVerdictLLM | RedTeamVerdictLLM,
    *,
    run_id: UUID,
    agent: AgentName,
    partition: Partition,
    prompt_version: str,
    served: ServedModel,
    valid: bool,
) -> AgentVerdict | RedTeamVerdict:
    if isinstance(out, RedTeamVerdictLLM):
        return RedTeamVerdict.from_llm(
            out,
            run_id=run_id,
            entity_token=partition.entity_token,
            as_of=partition.as_of,
            prompt_version=prompt_version,
            model_served=served.slug,
            valid=valid,
        )
    return AgentVerdict.from_llm(
        out,
        run_id=run_id,
        agent=agent,
        entity_token=partition.entity_token,
        as_of=partition.as_of,
        prompt_version=prompt_version,
        model_served=served.slug,
        valid=valid,
    )
