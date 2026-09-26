"""Invariant 4 (§12.1 items 2-3, §13): no identity, raw number or calendar date in any partition."""

from __future__ import annotations

import re

import pytest

from agents.partitions import Partition
from contracts.enums import AgentName
from tests.agents.leak import calendar_dates, numeric_leaks
from tests.agents.world import (
    IDENTITY,
    RAW_FACT,
    RAW_POST_HOLDINGS,
    RAW_TXN_PRICE,
    RAW_TXN_SHARES,
    World,
    build_world,
)


@pytest.fixture(scope="module")
def world() -> World:
    return build_world()


@pytest.fixture(scope="module")
def partitions(world: World) -> dict[AgentName, Partition]:
    built = world.partitioner.build_all(world.entity)
    built[AgentName.RED_TEAM] = world.partitioner.red_team(world.entity)
    return built


def _find(text: str, word: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.IGNORECASE) is not None


def test_no_identity_string_in_any_partition(partitions: dict[AgentName, Partition]) -> None:
    assert set(partitions) == {
        AgentName.VALUE,
        AgentName.QUALITY_CATALYST,
        AgentName.INSIDER,
        AgentName.TECHNICAL,
        AgentName.MACRO_NARRATIVE,
        AgentName.RED_TEAM,
    }
    for agent, part in partitions.items():
        for word in IDENTITY:
            assert not _find(part.text, word), f"{word!r} leaked into the {agent.value} partition"


def test_harness_would_catch_an_unmasked_partition(world: World) -> None:
    """Negative control: raw news text (no masker) trips the same check."""
    raw = world.entity.news[0].headline + " " + world.entity.news[0].summary
    assert any(_find(raw, w) for w in IDENTITY)


def test_no_raw_number_or_date_in_any_partition(partitions: dict[AgentName, Partition]) -> None:
    raw = [
        *RAW_FACT.values(),
        RAW_TXN_SHARES,
        RAW_TXN_PRICE,
        RAW_POST_HOLDINGS,
        61_234_567_000.0 * 0.985,
        12_345_678.0,
        2_500_000_000.0,
        61_200_000_000.0,
        201.5678,
        9_999.0,
    ]
    for agent, part in partitions.items():
        assert numeric_leaks(part.text, raw) == [], agent.value
        assert calendar_dates(part.text) == [], agent.value


def test_person_and_entity_tokens_replace_names_in_news(
    partitions: dict[AgentName, Partition], world: World
) -> None:
    news = partitions[AgentName.QUALITY_CATALYST].text
    assert "TICKER_01" in news and re.search(r"EXEC_[0-9A-F]{8}", news)
    assert "[AMOUNT]" in news and "[DATE]" in news and "[SHARES]" in news
    assert world.tokens[1] == "TICKER_01"


def test_insider_rows_use_stable_exec_tokens_and_role_tokens(
    partitions: dict[AgentName, Partition],
) -> None:
    text = partitions[AgentName.INSIDER].text
    tokens = set(re.findall(r"EXEC_[0-9A-F]{8}", text))
    assert len(tokens) == 2  # Jane Doe and Chidi Okafor
    assert "ROLE_CEO" in text and "ROLE_DIRECTOR" in text
    assert "Chief Executive" not in text
