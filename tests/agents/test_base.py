"""Verdict building, backtest validity, evidence validation, repair/discard (§3.1, §10.2, §13)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from agents.base import (
    CUTOFF_BUFFER,
    AgentCallResult,
    ChatResponse,
    DiscardReason,
    Message,
    UnknownServedModelError,
    run_agent_call,
    verdict_valid,
)
from agents.partitions import Partition
from config.loader import ModelsConfig, UsageUnavailableError, load_config
from contracts.enums import AgentName, FeedName, ModelTier, RunMode
from contracts.models import AgentVerdict, RedTeamVerdict
from tests.agents.world import build_world

MODELS = load_config(allow_placeholders=True, env={}).models
PRIMARY = MODELS.tiers[ModelTier.FAST].primary
FALLBACK = MODELS.tiers[ModelTier.FAST].fallbacks[0]
STRONG = MODELS.tiers[ModelTier.STRONG].primary
USAGE = {"prompt_tokens": 1000, "completion_tokens": 200}


@pytest.fixture(scope="module")
def value_part() -> Partition:
    world = build_world()
    return world.partitioner.value(world.entity)


@pytest.fixture(scope="module")
def red_part() -> Partition:
    world = build_world()
    return world.partitioner.red_team(world.entity)


def _cite(part: Partition) -> dict[str, str]:
    source, row_id = sorted(part.evidence, key=lambda k: (k[0].value, k[1]))[0]
    return {"source": source.value, "row_id": row_id, "note": "a cited row"}


def _value_json(part: Partition, evidence: list[dict[str, str]] | None = None) -> str:
    return json.dumps(
        {
            "stance": "buy",
            "p_outperform": 0.6,
            "horizon_days": 21,
            "key_evidence": evidence if evidence is not None else [_cite(part)],
            "risks": ["thin margins"],
            "data_sufficiency": "full",
        }
    )


class Scripted:
    """Replays canned responses and records the messages of every call."""

    def __init__(self, *responses: ChatResponse) -> None:
        self.responses = list(responses)
        self.calls: list[list[Message]] = []

    def complete(self, messages: Sequence[Message]) -> ChatResponse:
        self.calls.append(list(messages))
        return self.responses.pop(0)


def _run(
    client: Scripted,
    part: Partition,
    *,
    agent: AgentName = AgentName.VALUE,
    mode: RunMode = RunMode.LIVE,
    models: ModelsConfig = MODELS,
) -> AgentCallResult:
    return run_agent_call(
        client,
        models=models,
        mode=mode,
        run_id=uuid4(),
        agent=agent,
        partition=part,
        system_prompt="rubric",
        prompt_version="v1",
    )


def test_verdict_records_served_model_not_requested(value_part: Partition) -> None:
    # The request asked for PRIMARY; the provider fell back to FALLBACK (invariant 7).
    client = Scripted(ChatResponse(FALLBACK.slug, _value_json(value_part), USAGE))
    result = _run(client, value_part)
    assert isinstance(result.verdict, AgentVerdict)
    assert result.verdict.model_served == FALLBACK.slug != PRIMARY.slug
    assert result.verdict.entity_token == value_part.entity_token
    assert result.verdict.as_of == value_part.as_of
    assert result.verdict.prompt_version == "v1"
    assert result.calls == 1
    assert result.cost_usd == pytest.approx(FALLBACK.local_cost_usd(1000, 200))


def test_system_and_user_messages(value_part: Partition) -> None:
    client = Scripted(ChatResponse(PRIMARY.slug, _value_json(value_part), USAGE))
    _run(client, value_part)
    assert client.calls[0] == [
        {"role": "system", "content": "rubric"},
        {"role": "user", "content": value_part.text},
    ]


def test_hallucinated_citation_is_repaired_once(value_part: Partition) -> None:
    bad = _value_json(value_part, [{"source": "fundamentals", "row_id": "FIN_Q99", "note": "x"}])
    client = Scripted(
        ChatResponse(PRIMARY.slug, bad, USAGE),
        ChatResponse(PRIMARY.slug, _value_json(value_part), USAGE),
    )
    result = _run(client, value_part)
    assert result.verdict is not None and result.calls == 2
    repair = client.calls[1]
    assert repair[-2] == {"role": "assistant", "content": bad}
    assert "FIN_Q99" in repair[-1]["content"]
    assert result.cost_usd == pytest.approx(2 * PRIMARY.local_cost_usd(1000, 200))


def test_hallucinated_citation_twice_is_discarded(value_part: Partition) -> None:
    bad = _value_json(value_part, [{"source": "fundamentals", "row_id": "FIN_Q99", "note": "x"}])
    client = Scripted(
        ChatResponse(PRIMARY.slug, bad, USAGE), ChatResponse(PRIMARY.slug, bad, USAGE)
    )
    result = _run(client, value_part)
    assert result.verdict is None
    assert result.discard_reason is DiscardReason.UNPROVIDED_EVIDENCE
    assert result.calls == 2  # never a third
    assert result.cost_usd > 0  # both calls were billed


def test_citation_of_right_id_wrong_feed_is_rejected(value_part: Partition) -> None:
    source, row_id = sorted(value_part.evidence, key=lambda k: (k[0].value, k[1]))[0]
    other = next(f for f in FeedName if f is not source)
    bad = _value_json(value_part, [{"source": other.value, "row_id": row_id, "note": "x"}])
    client = Scripted(
        ChatResponse(PRIMARY.slug, bad, USAGE), ChatResponse(PRIMARY.slug, bad, USAGE)
    )
    assert _run(client, value_part).verdict is None


def test_malformed_json_repaired_then_discarded(value_part: Partition) -> None:
    client = Scripted(
        ChatResponse(PRIMARY.slug, "not json", USAGE), ChatResponse(PRIMARY.slug, "{}", USAGE)
    )
    result = _run(client, value_part)
    assert result.verdict is None and result.discard_reason is DiscardReason.UNPARSEABLE


def test_one_good_citation_among_bad_is_still_rejected(value_part: Partition) -> None:
    mixed = _value_json(
        value_part,
        [_cite(value_part), {"source": "fundamentals", "row_id": "FIN_Q99", "note": "x"}],
    )
    client = Scripted(
        ChatResponse(PRIMARY.slug, mixed, USAGE), ChatResponse(PRIMARY.slug, mixed, USAGE)
    )
    assert _run(client, value_part).verdict is None


def test_red_team_uses_its_own_envelope(red_part: Partition) -> None:
    payload = json.dumps(
        {
            "bear_severity": "high",
            "falsifiable_risk": "margins compress",
            "horizon_days": 21,
            "key_evidence": [_cite(red_part)],
        }
    )
    client = Scripted(ChatResponse(STRONG.slug, payload, USAGE))
    result = _run(client, red_part, agent=AgentName.RED_TEAM)
    assert isinstance(result.verdict, RedTeamVerdict)
    assert result.verdict.model_served == STRONG.slug


def test_partition_agent_mismatch_raises(value_part: Partition) -> None:
    with pytest.raises(ValueError, match="partition is for value"):
        _run(Scripted(), value_part, agent=AgentName.INSIDER)


def test_unknown_served_model_fails_closed(value_part: Partition) -> None:
    client = Scripted(ChatResponse("vendor/unlisted", _value_json(value_part), USAGE))
    with pytest.raises(UnknownServedModelError):
        _run(client, value_part)


def test_missing_usage_fails_closed(value_part: Partition) -> None:
    client = Scripted(ChatResponse(PRIMARY.slug, _value_json(value_part), None))
    with pytest.raises(UsageUnavailableError):
        _run(client, value_part)


# --- valid (§3.1, §12.1) ---------------------------------------------------------------------


def _at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 21, 0, tzinfo=UTC)


def test_valid_boundary_is_strictly_after_cutoff_plus_60_days() -> None:
    cutoff = PRIMARY.effective_cutoff
    edge = cutoff + CUTOFF_BUFFER
    assert not verdict_valid(RunMode.BACKTEST, _at(edge), PRIMARY)
    assert verdict_valid(RunMode.BACKTEST, _at(edge + timedelta(days=1)), PRIMARY)
    assert not verdict_valid(RunMode.BACKTEST, _at(cutoff - timedelta(days=400)), PRIMARY)


@pytest.mark.parametrize("mode", [RunMode.LIVE, RunMode.ABLATION])
def test_valid_always_true_outside_backtest(mode: RunMode) -> None:
    assert verdict_valid(mode, _at(PRIMARY.effective_cutoff - timedelta(days=400)), PRIMARY)


def test_measured_cutoff_later_than_stated_invalidates() -> None:
    measured = PRIMARY.model_copy(
        update={"measured_effective_cutoff": PRIMARY.stated_training_cutoff + timedelta(days=200)}
    )
    when = _at(PRIMARY.stated_training_cutoff + CUTOFF_BUFFER + timedelta(days=30))
    assert verdict_valid(RunMode.BACKTEST, when, PRIMARY)
    assert not verdict_valid(RunMode.BACKTEST, when, measured)


def test_backtest_run_flags_verdict_from_contaminated_fallback(value_part: Partition) -> None:
    # as_of (2025) is after PRIMARY's placeholder cutoff (2000) but not after a later fallback's.
    late = FALLBACK.model_copy(update={"stated_training_cutoff": date(2025, 2, 1)})
    models = MODELS.model_copy(
        update={
            "tiers": {
                **MODELS.tiers,
                ModelTier.FAST: MODELS.tiers[ModelTier.FAST].model_copy(
                    update={"fallbacks": (late,)}
                ),
            }
        }
    )
    client = Scripted(
        ChatResponse(PRIMARY.slug, _value_json(value_part), USAGE),
        ChatResponse(FALLBACK.slug, _value_json(value_part), USAGE),
    )
    ok = _run(client, value_part, mode=RunMode.BACKTEST, models=models)
    flagged = _run(client, value_part, mode=RunMode.BACKTEST, models=models)
    assert ok.verdict is not None and ok.verdict.valid is True
    assert flagged.verdict is not None and flagged.verdict.valid is False
    assert flagged.verdict.model_served == FALLBACK.slug  # persisted, just excluded downstream
