"""Hypothesis strategies producing valid instances of every contract model."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from hypothesis import strategies as st

from contracts import models as m
from contracts.enums import (
    VOTING_AGENTS,
    AgentName,
    BearSeverity,
    CioAction,
    DataSufficiency,
    FeedName,
    Horizon,
    OrderSide,
    RunMode,
    RunStatus,
    Stage,
    Stance,
)

text_chars = st.characters(codec="utf-8")


def text(max_size: int) -> st.SearchStrategy[str]:
    return st.text(text_chars, min_size=1, max_size=max_size).filter(lambda s: s.strip() != "")


def floats(lo: float | None = None, hi: float | None = None, **kw: Any) -> st.SearchStrategy[float]:
    return st.floats(lo, hi, allow_nan=False, allow_infinity=False, **kw)


uuids = st.uuids()
aware_dt = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1), timezones=st.just(UTC)
)
dates = st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 1, 1))
prob = floats(0.0, 1.0)
weight = floats(0.0, m.MAX_POSITION)
positive = floats(1e-6, 1e9)
nonneg = floats(0.0, 1e9)
security_id = st.integers(min_value=1, max_value=2**31)
labels = text(128)
short = text(280)
sha = st.text("0123456789abcdef", min_size=64, max_size=64)
entity_token = st.builds(
    lambda p, n: f"{p}_{n:02d}", st.sampled_from(["TICKER", "ENTITY"]), st.integers(0, 9999)
)
voting_agent = st.sampled_from(sorted(VOTING_AGENTS))

evidence = st.builds(m.EvidenceRef, source=st.sampled_from(FeedName), row_id=labels, note=short)
evidence_tuple = st.lists(evidence, min_size=1, max_size=5).map(tuple)

agent_verdict_llm_kwargs: dict[str, st.SearchStrategy[Any]] = dict(
    stance=st.sampled_from(Stance),
    p_outperform=prob,
    horizon_days=st.sampled_from(Horizon),
    key_evidence=evidence_tuple,
    risks=st.lists(short, max_size=3).map(tuple),
    data_sufficiency=st.sampled_from(DataSufficiency),
)
envelope: dict[str, st.SearchStrategy[Any]] = dict(
    run_id=uuids, as_of=aware_dt, prompt_version=labels, model_served=labels
)
verdict_envelope = {**envelope, "valid": st.booleans()}

agent_verdict_llm = st.builds(m.AgentVerdictLLM, **agent_verdict_llm_kwargs)
agent_verdict = st.builds(
    m.AgentVerdict,
    **agent_verdict_llm_kwargs,
    **verdict_envelope,
    agent=voting_agent,
    entity_token=entity_token,
)

red_team_llm_kwargs: dict[str, st.SearchStrategy[Any]] = dict(
    bear_severity=st.sampled_from(BearSeverity),
    falsifiable_risk=short,
    horizon_days=st.sampled_from(Horizon),
    key_evidence=evidence_tuple,
)
red_team_verdict_llm = st.builds(m.RedTeamVerdictLLM, **red_team_llm_kwargs)
red_team_verdict = st.builds(
    m.RedTeamVerdict, **red_team_llm_kwargs, **verdict_envelope, entity_token=entity_token
)

cio_name_decision = st.one_of(
    st.builds(
        m.CioNameDecisionLLM,
        entity_token=entity_token,
        action=st.just(CioAction.APPROVE),
        reason=st.just(""),
    ),
    st.builds(
        m.CioNameDecisionLLM,
        entity_token=entity_token,
        action=st.sampled_from([CioAction.VETO, CioAction.FLAG_FOR_REVIEW]),
        reason=short,
    ),
)
cio_decisions = st.lists(cio_name_decision, max_size=15, unique_by=lambda d: d.entity_token).map(
    tuple
)
cio_decision_llm = st.builds(m.CioDecisionLLM, decisions=cio_decisions, rationale=text(4000))
cio_decision = st.builds(m.CioDecision, decisions=cio_decisions, rationale=text(4000), **envelope)

gate_decision = st.builds(
    m.GateDecision,
    run_id=uuids,
    security_id=security_id,
    as_of=aware_dt,
    feature_set_version=labels,
    gate_model_version=labels,
    score=prob,
    passed=st.booleans(),
    components=st.lists(st.tuples(labels, floats()), max_size=5).map(tuple),
)

agent_weights = st.lists(
    st.builds(m.AgentWeight, agent=voting_agent, weight=nonneg),
    min_size=1,
    max_size=5,
    unique_by=lambda w: w.agent,
).map(tuple)

committee_decision = st.builds(
    m.CommitteeDecision,
    run_id=uuids,
    security_id=security_id,
    entity_token=entity_token,
    as_of=aware_dt,
    horizon_days=st.sampled_from(Horizon),
    pooled_p=prob,
    dispersion=nonneg,
    agent_weights=agent_weights,
    bear_severity=st.none() | st.sampled_from(BearSeverity),
    target_weight=weight,
)

proposed_position = st.builds(
    m.ProposedPosition,
    security_id=security_id,
    entity_token=entity_token,
    sector=labels,
    pooled_p=prob,
    target_weight=weight,
)
# 12 x 0.08 < 1, so gross exposure never exceeds 1.
proposed_book = st.builds(
    m.ProposedBook,
    run_id=uuids,
    as_of=aware_dt,
    positions=st.lists(
        proposed_position,
        max_size=12,
        unique_by=(lambda p: p.security_id, lambda p: p.entity_token),
    ).map(tuple),
)

order_intent = st.builds(
    m.OrderIntent,
    run_id=uuids,
    security_id=security_id,
    client_order_id=labels,
    side=st.sampled_from(OrderSide),
    qty=positive,
    target_weight=weight,
    decision_price=positive,
    limit_price=positive,
    time_in_force_minutes=st.integers(1, 390),
)

fill_report = st.builds(
    m.FillReport,
    run_id=uuids,
    security_id=security_id,
    client_order_id=labels,
    broker_order_id=labels,
    side=st.sampled_from(OrderSide),
    filled_qty=nonneg,
    decision_price=positive,
    arrival_price=positive,
    fill_price=positive,
    slippage_bps=floats(),
    filled_at=aware_dt,
)


@st.composite
def run_record(draw: st.DrawFn) -> m.RunRecord:
    started = draw(aware_dt)
    ended = draw(st.none() | st.timedeltas(timedelta(0), timedelta(days=30)).map(started.__add__))
    return m.RunRecord(
        run_id=draw(uuids),
        mode=draw(st.sampled_from(RunMode)),
        as_of=draw(aware_dt),
        config_hash=draw(sha),
        status=draw(st.sampled_from(RunStatus)),
        started_at=started,
        ended_at=ended,
        status_reason=draw(st.none() | short),
    )


task_key = st.builds(
    m.TaskKey, run_id=uuids, stage=st.sampled_from(Stage), security_id=st.none() | security_id
)
decision_commitment = st.builds(
    m.DecisionCommitment, run_id=uuids, sha256=sha, committed_at=aware_dt
)
outcome_record = st.builds(
    m.OutcomeRecord,
    run_id=uuids,
    security_id=security_id,
    horizon=st.sampled_from(Horizon),
    fwd_return=floats(-1.0, 100.0),
    sector_fwd_return=floats(-1.0, 100.0),
    scored_at=aware_dt,
)


@st.composite
def agent_score(draw: st.DrawFn) -> m.AgentScore:
    start = draw(dates)
    return m.AgentScore(
        agent=draw(st.sampled_from(AgentName)),
        model_served=draw(st.none() | labels),
        prompt_version=draw(st.none() | labels),
        horizon=draw(st.sampled_from(Horizon)),
        window_start=start,
        window_end=start + draw(st.timedeltas(timedelta(0), timedelta(days=3650))),
        n=draw(st.integers(0, 10**6)),
        brier=draw(prob),
        ic=draw(st.none() | floats(-1.0, 1.0)),
        hit_rate=draw(prob),
    )


STRATEGIES: dict[type[m.Contract], st.SearchStrategy[Any]] = {
    m.EvidenceRef: evidence,
    m.AgentVerdictLLM: agent_verdict_llm,
    m.AgentVerdict: agent_verdict,
    m.RedTeamVerdictLLM: red_team_verdict_llm,
    m.RedTeamVerdict: red_team_verdict,
    m.CioNameDecisionLLM: cio_name_decision,
    m.CioDecisionLLM: cio_decision_llm,
    m.CioDecision: cio_decision,
    m.GateDecision: gate_decision,
    m.AgentWeight: agent_weights.map(lambda t: t[0]),
    m.CommitteeDecision: committee_decision,
    m.ProposedPosition: proposed_position,
    m.ProposedBook: proposed_book,
    m.OrderIntent: order_intent,
    m.FillReport: fill_report,
    m.RunRecord: run_record(),
    m.TaskKey: task_key,
    m.DecisionCommitment: decision_commitment,
    m.OutcomeRecord: outcome_record,
    m.AgentScore: agent_score(),
}
