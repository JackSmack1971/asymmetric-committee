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
    BrokerOrderStatus,
    CioAction,
    DataSufficiency,
    FeedName,
    Horizon,
    KillTrigger,
    OrderKind,
    OrderSide,
    ReferenceSource,
    RunMode,
    RunStatus,
    SizingMode,
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
    p_outperform_5=prob,
    p_outperform_21=prob,
    p_outperform_63=prob,
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

horizon_pool = st.builds(
    m.HorizonPool,
    horizon=st.sampled_from(Horizon),
    logit=floats(),
    lambda_t=prob,
    dispersion=nonneg,
    weights=agent_weights,
)

pooled_forecast = st.builds(
    m.PooledForecast,
    entity_token=entity_token,
    pools=st.lists(horizon_pool, min_size=1, max_size=3).map(tuple),
)

calibration_fit = st.builds(
    m.CalibrationFit,
    horizon=st.sampled_from(Horizon),
    alpha=floats(),
    beta=floats(),
    active=st.booleans(),
    independent_periods=st.integers(min_value=0, max_value=10_000),
    observations=st.integers(min_value=0, max_value=100_000),
    base_rate=st.none() | prob,
)

error_correlation = st.builds(
    m.ErrorCorrelation,
    horizon=st.sampled_from(Horizon),
    agents=st.integers(min_value=2, max_value=5),
    observations=st.integers(min_value=2, max_value=100_000),
    mean_correlation=st.floats(min_value=-1.0, max_value=1.0, allow_nan=False),
    n_eff=st.floats(min_value=0.01, max_value=5.0, allow_nan=False),
    monoculture_alert=st.booleans(),
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

broker_order = st.builds(
    m.BrokerOrder,
    broker_order_id=labels,
    client_order_id=labels,
    symbol=labels,
    side=st.sampled_from(OrderSide),
    kind=st.sampled_from(OrderKind),
    qty=positive,
    filled_qty=nonneg,
    limit_price=st.none() | positive,
    filled_avg_price=st.none() | positive,
    status=st.sampled_from(BrokerOrderStatus),
    submitted_at=aware_dt,
    filled_at=st.none() | aware_dt,
)

execution_record = st.builds(
    m.ExecutionRecord,
    run_id=uuids,
    security_id=security_id,
    client_order_id=labels,
    broker_order_id=labels,
    kind=st.sampled_from(OrderKind),
    side=st.sampled_from(OrderSide),
    qty=positive,
    filled_qty=nonneg,
    limit_price=st.none() | positive,
    decision_price=positive,
    reference_price=positive,
    reference_source=st.sampled_from(ReferenceSource),
    fill_price=st.none() | positive,
    slippage_bps=st.none() | floats(),
    status=st.sampled_from(BrokerOrderStatus),
    submitted_at=aware_dt,
    filled_at=st.none() | aware_dt,
)


@st.composite
def market_session(draw: st.DrawFn) -> m.MarketSession:
    opens = draw(aware_dt)
    return m.MarketSession(
        session_date=draw(dates),
        opens_at=opens,
        closes_at=opens + draw(st.timedeltas(timedelta(minutes=1), timedelta(hours=12))),
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
        total_cost_usd=draw(nonneg),
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


verdict_record = st.builds(
    m.VerdictRecord,
    security_id=security_id,
    verdict=agent_verdict | red_team_verdict,
    tokens_in=st.none() | st.integers(0, 10**6),
    tokens_out=st.none() | st.integers(0, 10**6),
    cost_usd=nonneg,
    latency_ms=st.none() | st.integers(0, 10**7),
)
committee_decision_record = st.builds(
    m.CommitteeDecisionRecord,
    decision=committee_decision,
    pooled_logit=st.none() | floats(),
    sizing_mode=st.none() | st.sampled_from(SizingMode),
    cio_action=st.none() | st.sampled_from(CioAction),
    rationale=st.none() | text(4000),
)


@st.composite
def portfolio_snapshot(draw: st.DrawFn) -> m.PortfolioSnapshot:
    book = draw(proposed_book)
    return m.PortfolioSnapshot(
        run_id=book.run_id,
        as_of=book.as_of,
        book=book,
        cash_weight=book.cash_weight,
        cio=draw(
            st.none() | cio_decision.map(lambda c: c.model_copy(update={"run_id": book.run_id}))
        ),
    )


dlq_record = st.builds(
    m.DlqRecord,
    run_id=uuids,
    as_of=aware_dt,
    agent=labels,
    error_type=labels,
    payload=st.dictionaries(labels, short, max_size=4),
)


@st.composite
def kill_switch_event(draw: st.DrawFn) -> m.KillSwitchEvent:
    trigger = draw(st.sampled_from(KillTrigger))
    return m.KillSwitchEvent(
        run_id=draw(uuids),
        triggered_at=draw(aware_dt),
        trigger=trigger,
        daily_loss=draw(st.none() | floats(-1.0, 1.0)),
        peak_drawdown=draw(st.none() | floats(0.0, 1.0)),
        cancelled_order_ids=tuple(draw(st.lists(labels, max_size=5))),
        flattened=trigger is KillTrigger.MANUAL and draw(st.booleans()),
    )


commitment_anchor = st.builds(
    m.CommitmentAnchor,
    run_id=uuids,
    sha256=sha,
    ots_proof=st.none() | st.binary(max_size=64),
    git_commit=st.none() | labels,
    anchored_at=aware_dt,
    verified_at=st.none() | aware_dt,
)


@st.composite
def step_artifacts(draw: st.DrawFn) -> m.StepArtifacts:
    run = draw(run_record())
    rid = run.run_id
    snap = draw(st.none() | portfolio_snapshot())
    if snap is not None:
        snap = snap.model_copy(
            update={
                "run_id": rid,
                "book": snap.book.model_copy(update={"run_id": rid}),
                "cio": snap.cio.model_copy(update={"run_id": rid}) if snap.cio else None,
            }
        )
    return m.StepArtifacts(
        run=run,
        verdicts=tuple(
            v.model_copy(update={"verdict": v.verdict.model_copy(update={"run_id": rid})})
            for v in draw(st.lists(verdict_record, max_size=3))
        ),
        decisions=tuple(
            d.model_copy(update={"decision": d.decision.model_copy(update={"run_id": rid})})
            for d in draw(st.lists(committee_decision_record, max_size=3))
        ),
        portfolio=snap,
        commitment=draw(
            st.none() | decision_commitment.map(lambda c: c.model_copy(update={"run_id": rid}))
        ),
        dlq=tuple(
            d.model_copy(update={"run_id": rid}) for d in draw(st.lists(dlq_record, max_size=3))
        ),
        kill_switch=tuple(
            k.model_copy(update={"run_id": rid})
            for k in draw(st.lists(kill_switch_event(), max_size=2))
        ),
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
    m.HorizonPool: horizon_pool,
    m.PooledForecast: pooled_forecast,
    m.CalibrationFit: calibration_fit,
    m.ErrorCorrelation: error_correlation,
    m.ProposedPosition: proposed_position,
    m.ProposedBook: proposed_book,
    m.OrderIntent: order_intent,
    m.FillReport: fill_report,
    m.BrokerOrder: broker_order,
    m.ExecutionRecord: execution_record,
    m.MarketSession: market_session(),
    m.RunRecord: run_record(),
    m.TaskKey: task_key,
    m.DecisionCommitment: decision_commitment,
    m.OutcomeRecord: outcome_record,
    m.AgentScore: agent_score(),
    m.VerdictRecord: verdict_record,
    m.CommitteeDecisionRecord: committee_decision_record,
    m.PortfolioSnapshot: portfolio_snapshot(),
    m.DlqRecord: dlq_record,
    m.KillSwitchEvent: kill_switch_event(),
    m.CommitmentAnchor: commitment_anchor,
    m.StepArtifacts: step_artifacts(),
}
