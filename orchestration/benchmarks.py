"""Admission-gated orchestration boundary for weekly benchmark evaluation."""

from __future__ import annotations

from collections.abc import Sequence

from contracts.benchmarks import (
    BenchmarkResult,
    BenchmarkTrialIdentity,
    assert_benchmark_completeness,
    assert_benchmark_variant_completeness,
)
from contracts.enums import VOTING_AGENTS, AgentName, OutcomeCompleteness, RunMode
from contracts.models import (
    BenchmarkReplayContext,
    CioDecision,
    CommitmentAnchor,
    GateDecision,
    PortfolioSnapshot,
    RunRecord,
    VerdictRecord,
)
from evaluation.benchmarks import BenchmarkWeekInputs, evaluate_benchmark_week
from evaluation.scorable import Completeness, ScoringTicket
from orchestration.sink import DecisionSink


def _stored_gate_versions(
    run: RunRecord,
    replay_context: BenchmarkReplayContext,
    gate_decisions: Sequence[GateDecision],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Require actual run-scoped gate rows to support the persisted identity versions."""
    if not gate_decisions:
        raise ValueError("trial identity lacks actual stored gate-decision evidence")
    if any(
        decision.run_id != run.run_id or decision.as_of != run.as_of for decision in gate_decisions
    ):
        raise ValueError("trial gate-decision evidence is inconsistent with the run")
    gate_versions = tuple(sorted({decision.gate_model_version for decision in gate_decisions}))
    feature_versions = tuple(sorted({decision.feature_set_version for decision in gate_decisions}))
    if not gate_versions or not feature_versions:
        raise ValueError("trial identity lacks gate or feature versions")
    if replay_context.gate_model_versions != gate_versions:
        raise ValueError("replay context gate versions do not match stored gate decisions")
    if replay_context.feature_set_versions != feature_versions:
        raise ValueError("replay context feature versions do not match stored gate decisions")
    return gate_versions, feature_versions


def build_trial_identity(
    *,
    run: RunRecord,
    replay_context: BenchmarkReplayContext,
    verdict_records: Sequence[VerdictRecord],
    portfolio: PortfolioSnapshot,
    anchor: CommitmentAnchor,
    gate_decisions: Sequence[GateDecision],
    ablation_id: str | None = None,
) -> BenchmarkTrialIdentity:
    """Derive the exact series identity only from durable evidence; missing evidence is fatal."""
    if run.run_id != replay_context.run_id or run.run_id != portfolio.run_id:
        raise ValueError("trial identity evidence belongs to different runs")
    if anchor.run_id != run.run_id:
        raise ValueError("trial identity anchor belongs to a different run")
    if anchor.git_commit is None:
        raise ValueError("trial identity needs the anchored code revision")
    if run.mode is RunMode.ABLATION and not ablation_id:
        raise ValueError("ablation trial identity is missing its ablation id")
    if run.mode is not RunMode.ABLATION and ablation_id is not None:
        raise ValueError("non-ablation trial identity cannot carry an ablation id")
    prompts: dict[AgentName, set[str]] = {}
    models: dict[AgentName, set[str]] = {}
    for record in verdict_records:
        verdict = record.verdict
        if verdict.run_id != run.run_id or verdict.agent is AgentName.CIO:
            raise ValueError("trial verdict identity is invalid or belongs to another run")
        prompts.setdefault(verdict.agent, set()).add(verdict.prompt_version)
        models.setdefault(verdict.agent, set()).add(verdict.model_served)
    cio: CioDecision | None = portfolio.cio
    if cio is None or cio.run_id != run.run_id:
        raise ValueError("trial identity lacks the durable served CIO decision")
    prompts.setdefault(AgentName.CIO, set()).add(cio.prompt_version)
    models.setdefault(AgentName.CIO, set()).add(cio.model_served)
    required_agents = set(VOTING_AGENTS) | {AgentName.RED_TEAM, AgentName.CIO}
    if set(prompts) != required_agents:
        missing = sorted(agent.value for agent in required_agents - set(prompts))
        raise ValueError(f"trial identity lacks prompt/model evidence for agents: {missing}")
    gate_versions, feature_versions = _stored_gate_versions(run, replay_context, gate_decisions)
    return BenchmarkTrialIdentity(
        config_sha256=run.config_hash,
        prompt_versions={agent: tuple(sorted(values)) for agent, values in prompts.items()},
        served_model_slugs={agent: tuple(sorted(values)) for agent, values in models.items()},
        feature_set_versions=feature_versions,
        gate_model_versions=gate_versions,
        mode=run.mode,
        ablation_id=ablation_id,
        evaluation_parameters_sha256=replay_context.evaluation_parameters_sha256,
        code_revision=anchor.git_commit,
    )


def evaluate_admitted_week(
    ticket: ScoringTicket, inputs: BenchmarkWeekInputs
) -> tuple[BenchmarkResult, ...]:
    """Require the P6.5 ticket identity, cutoff, and terminal class before pure evaluation."""
    if inputs.run_id != ticket.run_id:
        raise ValueError("benchmark inputs do not belong to admitted run")
    if inputs.run_as_of != ticket.run_as_of:
        raise ValueError("benchmark inputs do not match admitted run as-of")
    if inputs.run_as_of > inputs.outcome_cutoff:
        raise ValueError("benchmark run as-of follows its outcome cutoff")
    if inputs.commitment_sha256 != ticket.commitment_sha256:
        raise ValueError("benchmark inputs do not match admitted commitment")
    if inputs.outcome_cutoff != ticket.requested_at:
        raise ValueError("benchmark cutoff must equal the admitted outcome cutoff")
    expected = (
        OutcomeCompleteness.COMPLETE
        if ticket.completeness is Completeness.COMPLETE
        else OutcomeCompleteness.HALTED
    )
    if inputs.completeness is not expected:
        raise ValueError("benchmark completeness does not match admitted run class")
    rows = evaluate_benchmark_week(inputs)
    keys = ((row.week_start, row.benchmark, row.variant) for row in rows)
    if expected is OutcomeCompleteness.HALTED:
        assert_benchmark_variant_completeness(keys, (inputs.week_start,))
    else:
        assert_benchmark_completeness(keys, (inputs.week_start,))
    return rows


def evaluate_and_persist_admitted_week(
    ticket: ScoringTicket, inputs: BenchmarkWeekInputs, sink: DecisionSink
) -> tuple[BenchmarkResult, ...]:
    """Evaluate after admission; HALTED variants persist independently, COMPLETE stays atomic."""
    rows = evaluate_admitted_week(ticket, inputs)
    sink.record_benchmarks(rows, expected_weeks=(inputs.week_start,), ticket=ticket)
    return rows
