"""Turns one partition into one stored verdict (§3.1, §10.2).

This is the only module that builds an ``AgentVerdict``/``RedTeamVerdict`` from LLM output and the
only place ``valid`` is computed. Transport (OpenRouter HTTP, rate limiting) sits behind the
``ChatClient`` protocol in ``agents/llm/``; the cache and dead-letter queue are protocols here.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, TypeGuard
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


class LLMTransientError(RuntimeError):
    """A call still failing after its retries (429, 5xx, network). Goes to the dead-letter queue."""


class BudgetExceededError(RuntimeError):
    """Cumulative run cost reached ``run_budget_usd``: abort the run's remaining calls (§10.2).

    The caller marks the run ``PARTIAL``; every candidate not yet called goes to the dead-letter
    queue with reason ``budget_exceeded``.
    """


class RunBudget:
    """Thread-safe cumulative cost counter with a hard ceiling."""

    def __init__(self, max_usd: float, spent_usd: float = 0.0) -> None:
        """``spent_usd`` carries earlier attempts' spend when a run is resumed."""
        if max_usd <= 0:
            raise ValueError("max_usd must be positive")
        if spent_usd < 0:
            raise ValueError("spent_usd must not be negative")
        self.max_usd = max_usd
        self._spent = spent_usd
        self._lock = threading.Lock()

    @property
    def spent_usd(self) -> float:
        with self._lock:
            return self._spent

    @property
    def exceeded(self) -> bool:
        return self.spent_usd >= self.max_usd

    def charge(self, usd: float) -> None:
        """Book a call's cost, then raise if the ceiling is reached (spend stays recorded)."""
        with self._lock:
            self._spent += usd
            over = self._spent >= self.max_usd
        if over:
            raise BudgetExceededError(f"run cost {self.spent_usd:.4f} >= budget {self.max_usd}")


class VerdictCache(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...


class DeadLetterQueue(Protocol):
    def push(self, record: dict[str, str]) -> None: ...


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
    DEAD_LETTERED = "dead_lettered"  # transport failed after retries; the run must go PARTIAL


@dataclass(frozen=True)
class CallTelemetry:
    """What one verdict actually cost, measured per call and summed over its repair attempts.

    ``None`` means unknown, never zero: a cache hit made no call (no tokens, no latency; billed
    ``0.0``), and a provider that reported only ``usage.cost`` gives no token counts.
    """

    cost_usd: float
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int | None


@dataclass(frozen=True)
class AgentCallResult:
    """A verdict, or ``None`` when discarded as *invalid* (a failed call, never persisted)."""

    verdict: AgentVerdict | RedTeamVerdict | None
    cost_usd: float
    calls: int
    discard_reason: DiscardReason | None = None
    cache_hit: bool = False
    telemetry: CallTelemetry | None = None  # set whenever ``verdict`` is


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


def _is_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


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


def cache_key(
    agent: AgentName,
    prompt_version: str,
    requested_model: str,
    config_hash: str,
    input_hash: str,
) -> str:
    """Pre-request key (§10.2). It uses the *requested* model: ``model_served`` is not known yet.

    ``config_hash`` keeps a changed model config (tiers, params, schema) from serving stale output.
    """
    parts = (agent.value, prompt_version, requested_model, config_hash, input_hash)
    return "llm_eval:" + hashlib.sha256("".join(parts).encode()).hexdigest()


def _cache_entry(out: AgentVerdictLLM | RedTeamVerdictLLM, served: ServedModel) -> str:
    return json.dumps({"model_served": served.slug, "output": out.model_dump(mode="json")})


def _from_cache(
    cached: str,
    *,
    models: ModelsConfig,
    mode: RunMode,
    run_id: UUID,
    agent: AgentName,
    partition: Partition,
    prompt_version: str,
) -> AgentVerdict | RedTeamVerdict | None:
    """Rebuild a verdict from a cache entry, or ``None`` (treated as a miss) if it is unusable.

    The key does not contain ``as_of`` (partition text holds only relative days), so the envelope
    and ``valid`` are recomputed for this run from the stored output and the model that served it.
    """
    out_model = RedTeamVerdictLLM if agent is AgentName.RED_TEAM else AgentVerdictLLM
    try:
        entry = json.loads(cached)
        served = models.model_for_response(entry["model_served"])
        out = out_model.model_validate(entry["output"])
    except (ValueError, KeyError, TypeError):
        return None
    if unprovided_evidence(out.key_evidence, partition):
        return None
    return _envelope(
        out,
        run_id=run_id,
        agent=agent,
        partition=partition,
        prompt_version=prompt_version,
        served=served,
        valid=verdict_valid(mode, partition.as_of, served),
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
    requested_model: str = "",
    config_hash: str = "",
    cache: VerdictCache | None = None,
    dlq: DeadLetterQueue | None = None,
    budget: RunBudget | None = None,
) -> AgentCallResult:
    """One call plus at most one repair attempt (§10.2 Validation).

    With a ``cache``, an identical (agent, prompt_version, requested model, partition) is never
    re-billed. A call that keeps failing in transit is pushed to ``dlq`` and reported as
    ``DEAD_LETTERED`` so the caller can mark the run ``PARTIAL``.

    Pydantic parse, then the evidence check. A second failure discards the call. Cost accrues on
    every call, repaired or not. ``UsageUnavailableError``/``UnknownServedModelError`` propagate so
    the caller can abort the run as ``PARTIAL`` instead of booking $0.

    With a ``budget``, every call's cost is booked; once the ceiling is reached the job is pushed
    to ``dlq`` and ``BudgetExceededError`` propagates (a call already over budget is never sent).
    """
    if partition.agent is not agent:
        raise ValueError(f"partition is for {partition.agent.value}, not {agent.value}")
    out_model = RedTeamVerdictLLM if agent is AgentName.RED_TEAM else AgentVerdictLLM
    key = None
    if cache is not None:
        if not requested_model or not config_hash:
            raise ValueError("requested_model and config_hash are required with a cache")
        key = cache_key(agent, prompt_version, requested_model, config_hash, partition.input_hash)
        if (cached := cache.get(key)) is not None and (
            hit := _from_cache(
                cached,
                models=models,
                mode=mode,
                run_id=run_id,
                agent=agent,
                partition=partition,
                prompt_version=prompt_version,
            )
        ):
            return AgentCallResult(
                hit, 0.0, 0, cache_hit=True, telemetry=CallTelemetry(0.0, None, None, None)
            )
    messages: list[Message] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": partition.text},
    ]
    cost = 0.0
    tokens_in: int | None = 0
    tokens_out: int | None = 0
    latency_s = 0.0
    reason = DiscardReason.UNPARSEABLE

    def dead_letter(error: str) -> None:
        if dlq is not None:
            dlq.push(
                {
                    "run_id": str(run_id),
                    "agent": agent.value,
                    "entity_token": partition.entity_token,
                    "prompt_version": prompt_version,
                    "error": error,
                }
            )

    for attempt in range(2):
        if budget is not None and budget.exceeded:
            dead_letter("budget_exceeded")
            raise BudgetExceededError("run budget already exhausted")
        began = time.perf_counter()
        try:
            response = client.complete(messages)
        except LLMTransientError as e:
            dead_letter(str(e))
            return AgentCallResult(None, cost, attempt, DiscardReason.DEAD_LETTERED)
        latency_s += time.perf_counter() - began
        served = _served_model(models, response.model)
        call_usd = served.call_cost(response.usage).usd
        cost += call_usd
        usage = response.usage or {}
        p_tok, c_tok = usage.get("prompt_tokens"), usage.get("completion_tokens")
        tokens_in = tokens_in + p_tok if tokens_in is not None and _is_count(p_tok) else None
        tokens_out = tokens_out + c_tok if tokens_out is not None and _is_count(c_tok) else None
        if budget is not None:
            try:
                budget.charge(call_usd)
            except BudgetExceededError:
                dead_letter("budget_exceeded")
                raise
        parsed = _parse(out_model, response.content)
        if isinstance(parsed, str):
            problem, reason = parsed, DiscardReason.UNPARSEABLE
        else:
            missing = unprovided_evidence(parsed.key_evidence, partition)
            if not missing:
                if cache is not None and key is not None:
                    cache.set(key, _cache_entry(parsed, served))
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
                    telemetry=CallTelemetry(cost, tokens_in, tokens_out, round(latency_s * 1000)),
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
