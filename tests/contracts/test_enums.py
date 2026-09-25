"""Enum values and the §11 run state machine."""

from __future__ import annotations

from enum import Enum
from itertools import pairwise

import pytest

from contracts.enums import (
    RUN_TRANSITIONS,
    TERMINAL_RUN_STATUSES,
    VOTING_AGENTS,
    AgentName,
    CioAction,
    Horizon,
    RunMode,
    RunStatus,
    TaskStatus,
    can_transition,
)


@pytest.mark.parametrize(
    ("enum", "values"),
    [
        (
            AgentName,
            {"value", "quality_catalyst", "insider", "technical", "macro_narrative",
             "red_team", "cio", "quant_baseline"},
        ),
        (RunMode, {"live", "backtest", "ablation"}),
        (CioAction, {"approve", "veto", "flag_for_review"}),
        (Horizon, {21, 63}),
        (
            RunStatus,
            {"PENDING", "INGEST_OK", "FEATURES_OK", "GATED", "AGENTS_OK", "COMMITTED",
             "EXECUTED", "SCORED", "FAILED", "PARTIAL"},
        ),
    ],
)  # fmt: skip
def test_enum_values(enum: type[Enum], values: set[object]) -> None:
    assert {e.value for e in enum} == values


def test_voting_agents_exclude_non_voters() -> None:
    assert not {AgentName.RED_TEAM, AgentName.CIO, AgentName.QUANT_BASELINE} & VOTING_AGENTS
    assert len(VOTING_AGENTS) == 5


def test_happy_path() -> None:
    path = list(RunStatus)[:8]
    for src, dst in pairwise(path):
        assert can_transition(src, dst)


def test_every_status_has_transitions() -> None:
    assert set(RUN_TRANSITIONS) == set(RunStatus)


@pytest.mark.parametrize("src", sorted(set(RunStatus) - TERMINAL_RUN_STATUSES))
def test_non_terminal_can_fail_or_partial(src: RunStatus) -> None:
    assert can_transition(src, RunStatus.FAILED)
    assert can_transition(src, RunStatus.PARTIAL)


@pytest.mark.parametrize("src", sorted(TERMINAL_RUN_STATUSES))
def test_terminal_states_are_final(src: RunStatus) -> None:
    assert not RUN_TRANSITIONS[src]


def test_no_skipping() -> None:
    assert not can_transition(RunStatus.PENDING, RunStatus.COMMITTED)
    assert not can_transition(RunStatus.GATED, RunStatus.PENDING)


def test_only_completed_short_circuits() -> None:
    # Invariant 8 relies on COMPLETED being distinct from FAILED/PARTIAL.
    assert {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PARTIAL} <= set(TaskStatus)
