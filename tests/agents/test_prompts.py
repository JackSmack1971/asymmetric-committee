"""System prompts: registry, universal invariants, no identity or raw numbers (§3.1, §12.1)."""

from __future__ import annotations

import re

import pytest

from contracts.enums import AgentName
from contracts.models import AgentVerdictLLM, RedTeamVerdictLLM
from prompts.agents import PROMPTS
from tests.agents.world import IDENTITY

VOTERS = [a for a in PROMPTS if a is not AgentName.RED_TEAM]


def test_registry_has_five_voters_and_the_red_team() -> None:
    assert set(PROMPTS) == {
        AgentName.VALUE,
        AgentName.QUALITY_CATALYST,
        AgentName.INSIDER,
        AgentName.TECHNICAL,
        AgentName.MACRO_NARRATIVE,
        AgentName.RED_TEAM,
    }
    assert all(p.agent is a for a, p in PROMPTS.items())


@pytest.mark.parametrize("agent", VOTERS)
def test_voter_prompt_names_every_output_field_and_the_three_horizons(agent: AgentName) -> None:
    text = PROMPTS[agent].system
    for field in AgentVerdictLLM.model_fields:
        assert field in text, field
    assert "5, 21 and 63 trading days" in text


def test_red_team_prompt_names_its_fields_and_does_not_ask_for_probabilities() -> None:
    text = PROMPTS[AgentName.RED_TEAM].system
    for field in RedTeamVerdictLLM.model_fields:
        assert field in text, field
    assert "p_outperform" not in text


@pytest.mark.parametrize("agent", list(PROMPTS))
def test_prompt_states_universal_invariants(agent: AgentName) -> None:
    text = PROMPTS[agent].system
    assert "JSON" in text
    assert "1 to 5 items" in text
    assert "evidence_id" in text
    assert "calendar date" in text and "ticker" in text and "currency" in text


@pytest.mark.parametrize("agent", list(PROMPTS))
def test_prompt_leaks_no_identity_and_no_raw_number(agent: AgentName) -> None:
    text = PROMPTS[agent].system.casefold()
    for term in IDENTITY:
        assert not re.search(rf"\b{re.escape(term.casefold())}\b", text), term
    assert not re.search(r"\d{4,}", text)  # no raw value or date-like digit run
    assert not re.search(r"\$\s*\d", text)


def test_prompt_versions_are_distinct_content_derived_and_stable() -> None:
    versions = [p.version for p in PROMPTS.values()]
    assert len(set(versions)) == len(versions)
    assert all(re.fullmatch(r"v1-[0-9a-f]{8}", v) for v in versions)
    value = PROMPTS[AgentName.VALUE]
    edited = type(value)(value.agent, value.system + " ")
    assert edited.version != value.version and value.version == PROMPTS[AgentName.VALUE].version


def test_prompt_enum_values_match_the_contract_enums() -> None:
    from contracts.enums import BearSeverity, Stance

    red, voter = PROMPTS[AgentName.RED_TEAM].system, PROMPTS[AgentName.VALUE].system
    assert all(b.value in red for b in BearSeverity)
    assert "low, med or high" in red
    assert all(s.value in voter for s in Stance)
