"""§13 row 1: producers and consumers share enum types, and each producer's output
validates as the consumer's input."""

from __future__ import annotations

import typing
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from contracts import enums
from contracts import models as m


def enum_types(model: type[BaseModel], field: str) -> set[type[Enum]]:
    found: set[type[Enum]] = set()

    def walk(tp: Any) -> None:
        if isinstance(tp, type) and issubclass(tp, Enum):
            found.add(tp)
        elif isinstance(tp, type) and issubclass(tp, BaseModel):
            return
        elif isinstance(tp, Enum):  # Literal[AgentName.RED_TEAM]
            found.add(type(tp))
        for arg in typing.get_args(tp):
            walk(arg)

    walk(model.model_fields[field].annotation)
    return found


# (producer, field) -> (consumer, field); stage names follow §2.
PAIRS = [
    # agents -> committee
    ((m.AgentVerdict, "agent"), (m.AgentWeight, "agent")),
    ((m.AgentVerdict, "horizon_days"), (m.CommitteeDecision, "horizon_days")),
    ((m.RedTeamVerdict, "bear_severity"), (m.CommitteeDecision, "bear_severity")),
    ((m.RedTeamVerdict, "horizon_days"), (m.CommitteeDecision, "horizon_days")),
    # partitioner -> agents (evidence partitions)
    ((m.EvidenceRef, "source"), (m.EvidenceRef, "source")),
    # CIO -> execution / dashboard
    ((m.CioNameDecisionLLM, "action"), (m.CioDecision, "decisions")),
    # execution: intent -> fill
    ((m.OrderIntent, "side"), (m.FillReport, "side")),
    # evaluator: verdict/outcome/score share horizon and agent
    ((m.AgentVerdict, "horizon_days"), (m.OutcomeRecord, "horizon")),
    ((m.OutcomeRecord, "horizon"), (m.AgentScore, "horizon")),
    ((m.AgentVerdict, "agent"), (m.AgentScore, "agent")),
    ((m.RedTeamVerdict, "agent"), (m.AgentScore, "agent")),
    ((m.CioDecision, "agent"), (m.AgentScore, "agent")),
]


def _ids(pair: Any) -> str:
    (p, pf), (c, cf) = pair
    return f"{p.__name__}.{pf}->{c.__name__}.{cf}"


@pytest.mark.parametrize("pair", PAIRS, ids=[_ids(p) for p in PAIRS])
def test_pair_shares_enum(pair: Any) -> None:
    (prod, pf), (cons, cf) = pair
    produced = enum_types(prod, pf)
    assert produced, f"{prod.__name__}.{pf} is not enum-typed"
    if cons is m.CioDecision:  # nested: decisions -> CioNameDecisionLLM.action
        consumed = enum_types(m.CioNameDecisionLLM, "action")
    else:
        consumed = enum_types(cons, cf)
    assert produced & consumed, "no shared enum type"


def test_all_enums_come_from_contracts() -> None:
    import contracts.models as models_mod

    for obj in vars(models_mod).values():
        if isinstance(obj, type) and issubclass(obj, m.Contract):
            for name in obj.model_fields:
                for e in enum_types(obj, name):
                    assert e.__module__ == enums.__name__, f"{obj.__name__}.{name}"


NOW = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)


def test_pipeline_chain_validates() -> None:
    """Build each stage's input from the previous stage's output, as the pipeline will."""
    run_id = uuid4()
    ev = m.EvidenceRef(
        source=enums.FeedName.FEATURES, row_id="feat:1", note="12-1 momentum 80th pct"
    )
    llm_out = m.AgentVerdictLLM.model_validate_json(
        m.AgentVerdictLLM(
            stance=enums.Stance.BUY,
            p_outperform=0.62,
            horizon_days=enums.Horizon.D21,
            key_evidence=(ev,),
            risks=(),
            data_sufficiency=enums.DataSufficiency.FULL,
        ).model_dump_json()
    )
    env = {"run_id": run_id, "as_of": NOW, "prompt_version": "v1", "model_served": "x/y"}
    verdicts = [
        m.AgentVerdict.from_llm(
            llm_out,
            agent=a,
            entity_token="TICKER_01",
            run_id=run_id,
            as_of=NOW,
            prompt_version="v1",
            model_served="x/y",
            valid=True,
        )
        for a in sorted(enums.VOTING_AGENTS)
    ]
    assert all(v.model_served == env["model_served"] and v.valid for v in verdicts)
    red_out = m.RedTeamVerdictLLM(
        bear_severity=enums.BearSeverity.HIGH,
        falsifiable_risk="Gross margin falls below 40% next quarter",
        horizon_days=verdicts[0].horizon_days,
        key_evidence=(ev,),
    )
    red = m.RedTeamVerdict.from_llm(
        red_out,
        run_id=run_id,
        entity_token="TICKER_01",
        as_of=NOW,
        prompt_version="v1",
        model_served="x/y",
        valid=True,
    )
    assert red.valid
    cd = m.CommitteeDecision(
        run_id=run_id,
        security_id=1,
        entity_token=verdicts[0].entity_token,
        as_of=NOW,
        horizon_days=verdicts[0].horizon_days,
        pooled_p=0.62,
        dispersion=0.1,
        agent_weights=tuple(m.AgentWeight(agent=v.agent, weight=1.0) for v in verdicts),
        bear_severity=red.bear_severity,
        target_weight=0.04,
    )
    book = m.ProposedBook(
        run_id=run_id,
        as_of=NOW,
        positions=(
            m.ProposedPosition(
                security_id=cd.security_id,
                entity_token=cd.entity_token,
                sector="Semis",
                pooled_p=cd.pooled_p,
                target_weight=cd.target_weight,
            ),
        ),
    )
    cio_out = m.CioDecisionLLM(
        decisions=tuple(
            m.CioNameDecisionLLM(
                entity_token=p.entity_token, action=enums.CioAction.APPROVE, reason=""
            )
            for p in book.positions
        ),
        rationale="Single name, within limits.",
    )
    cio = m.CioDecision.from_llm(
        cio_out, run_id=run_id, as_of=NOW, prompt_version="v1", model_served="x/y"
    )
    intent = m.OrderIntent(
        run_id=run_id,
        security_id=book.positions[0].security_id,
        client_order_id=f"{run_id}:1",
        side=enums.OrderSide.BUY,
        qty=10,
        target_weight=book.positions[0].target_weight,
        decision_price=100.0,
        limit_price=100.1,
        time_in_force_minutes=15,
    )
    fill = m.FillReport(
        run_id=run_id,
        security_id=intent.security_id,
        client_order_id=intent.client_order_id,
        broker_order_id="b-1",
        side=intent.side,
        filled_qty=intent.qty,
        decision_price=intent.decision_price,
        arrival_price=100.02,
        fill_price=100.05,
        slippage_bps=5.0,
        filled_at=NOW,
    )
    outcome = m.OutcomeRecord(
        run_id=run_id,
        security_id=fill.security_id,
        horizon=cd.horizon_days,
        fwd_return=0.05,
        sector_fwd_return=0.02,
        scored_at=NOW,
    )
    assert cio.decisions[0].action is enums.CioAction.APPROVE
    assert outcome.excess_return == pytest.approx(0.03)
