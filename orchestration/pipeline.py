"""Backtest orchestrator: one weekly ``as_of`` step from partitions to an atomic sink flush (P5).

Reads are the caller's job (invariant 2): a ``StepLoader`` returns point-in-time ``StepInputs`` and
this module re-checks ``available_at <= as_of`` on everything it is handed. Nothing here talks to
the database; the finished step goes to ``DecisionSink.flush_step`` in one transaction.

Step order: alias guard -> voters -> pool -> red team on the top names -> stacker -> sizing -> CIO
-> commitment -> flush. A budget breach (or any aborting failure) after the voters ends the step
``PARTIAL``: what exists is flushed, no book is committed, and a backtest stops stepping.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid5

from agents.base import BudgetExceededError, ChatClient, RunBudget, VerdictCache
from agents.cio import CioResult, run_cio
from agents.partitions import EntityData, Partitioner
from agents.runner import RunnerResult, run_agents
from committee.pooling import History, NoVotingAgentsError, pool_agent_verdicts
from committee.stacker import Observation, fit_stacker, sigmoid
from config.loader import AppConfig
from contracts.data import Security
from contracts.enums import AgentName, BearSeverity, Horizon, RunMode, RunStatus, SizingMode
from contracts.models import (
    AgentVerdict,
    CalibrationFit,
    CommitteeDecision,
    CommitteeDecisionRecord,
    DecisionCommitment,
    DlqRecord,
    PooledForecast,
    PortfolioSnapshot,
    ProposedBook,
    RedTeamVerdict,
    RunRecord,
    StepArtifacts,
    VerdictRecord,
)
from risk import size_committee_book
from universe.snapshot import assert_alias_coverage

log = logging.getLogger(__name__)

RED_TEAM_TOP_N = 15  # §7.4: the red team reviews the strongest candidates only
VOL_FEATURE = "realized_vol_20d"  # annualized (P2)
_RUN_NAMESPACE = UUID("6f0c1c5e-3b1e-4a55-9a53-0d1f5f0b7a11")


class LookAheadError(RuntimeError):
    """A row with ``available_at > as_of`` reached the orchestrator (invariant 3)."""


@dataclass(frozen=True)
class StepInputs:
    """Point-in-time snapshot of one step, built by a loader that reads only via ``store.as_of``."""

    securities: Sequence[Security]
    brands: Mapping[int, Sequence[str]]
    entities: Sequence[EntityData]
    partitioner: Partitioner
    base_rates: Mapping[Horizon, float]
    observations: Mapping[Horizon, Sequence[Observation]] = field(default_factory=dict)
    independent_periods: Mapping[Horizon, int] = field(default_factory=dict)
    history: History | None = None


class StepLoader(Protocol):
    def load(self, as_of: datetime) -> StepInputs: ...


class StepSink(Protocol):
    def flush_step(self, step: StepArtifacts) -> None: ...


@dataclass(frozen=True)
class StepResult:
    run_id: UUID
    as_of: datetime
    status: RunStatus
    book: ProposedBook | None
    cost_usd: float
    reason: str | None = None


class CollectingDLQ:
    """In-memory DLQ: keeps every record for the sink and forwards to an optional live queue."""

    def __init__(self, forward: Callable[[dict[str, str]], None] | None = None) -> None:
        self.records: list[dict[str, str]] = []
        self._forward = forward

    def push(self, record: dict[str, str]) -> None:
        self.records.append(record)
        if self._forward is not None:
            self._forward(record)


def weekly_steps(start: datetime, end: datetime, *, days: int = 7) -> Iterator[datetime]:
    """``start, start + 7d, ...`` up to and including ``end``."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("step bounds must be timezone-aware")
    t = start
    while t <= end:
        yield t
        t += timedelta(days=days)


def config_fingerprint(cfg: AppConfig) -> str:
    return hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()


def assert_point_in_time(inputs: StepInputs, as_of: datetime) -> None:
    """Invariant 3 at the orchestrator boundary; the partitioner re-checks per row as well."""
    for e in inputs.entities:
        rows: list[tuple[str, datetime]] = [("features", e.features.available_at)]
        rows += [("fundamental", f.available_at) for f in e.facts]
        rows += [("insider", t.available_at) for t in e.insiders]
        rows += [("news", n.available_at) for n in e.news]
        for kind, at in rows:
            if at > as_of:
                raise LookAheadError(f"{kind} row for security {e.security_id} at {at} > {as_of}")


def _commitment_hash(decisions: Sequence[CommitteeDecisionRecord], book: ProposedBook) -> str:
    payload = {
        "decisions": [d.model_dump(mode="json") for d in decisions],
        "book": book.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class BacktestOrchestrator:
    def __init__(
        self,
        *,
        config: AppConfig,
        loader: StepLoader,
        sink: StepSink,
        client_for: Callable[[AgentName], ChatClient],
        cio_client: ChatClient,
        primary_horizon: Horizon = Horizon.D21,
        cache: VerdictCache | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        concurrency: int = 16,
    ) -> None:
        self._cfg = config
        self._loader = loader
        self._sink = sink
        self._client_for = client_for
        self._cio_client = cio_client
        self._primary = primary_horizon
        self._cache = cache
        self._clock = clock
        self._concurrency = concurrency
        self._hash = config_fingerprint(config)

    def run_id_for(self, as_of: datetime) -> UUID:
        """Deterministic, so re-running a step is a replay for the sink."""
        return uuid5(_RUN_NAMESPACE, f"{self._hash}:{RunMode.BACKTEST.value}:{as_of.isoformat()}")

    def run_backtest(self, start: datetime, end: datetime) -> list[StepResult]:
        results: list[StepResult] = []
        for as_of in weekly_steps(start, end):
            result = self.run_step(as_of)
            results.append(result)
            if result.status is RunStatus.PARTIAL:
                break
        return results

    # ------------------------------------------------------------------------------------------

    def run_step(self, as_of: datetime) -> StepResult:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        run_id, started = self.run_id_for(as_of), self._clock()
        inputs = self._loader.load(as_of)
        assert_point_in_time(inputs, as_of)
        assert_alias_coverage(inputs.securities, inputs.brands)  # fail closed before any prompt

        budget = RunBudget(self._cfg.pipeline.budgets.run_budget_usd)
        dlq = CollectingDLQ()
        voters: list[AgentVerdict] = []
        reds: list[RedTeamVerdict] = []

        def collect(v: AgentVerdict | RedTeamVerdict) -> None:
            (reds if isinstance(v, RedTeamVerdict) else voters).append(v)  # type: ignore[arg-type]

        def call(entities: Sequence[EntityData], red_ids: set[int], done: set[str]) -> RunnerResult:
            return asyncio.run(
                run_agents(
                    run_id=run_id,
                    mode=RunMode.BACKTEST,
                    models=self._cfg.models,
                    config_hash=self._hash,
                    partitioner=inputs.partitioner,
                    entities=entities,
                    client_for=self._client_for,
                    sink=collect,
                    red_team_ids=red_ids,
                    completed=done,
                    cache=self._cache,
                    dlq=dlq,
                    budget=budget,
                    concurrency=self._concurrency,
                )
            )

        def partial(reason: str) -> StepResult:
            return self._flush_partial(
                run_id, as_of, started, inputs.entities, voters, reds, dlq, budget, reason
            )

        first = call(inputs.entities, set(), set())
        if first.status is RunStatus.PARTIAL:
            return partial(
                "voting stage aborted: " + ", ".join(sorted(set(first.aborted.values())))
            )

        by_token = {e.entity_token: e for e in inputs.entities}
        pooled: dict[int, PooledForecast] = {}
        for token, group in _group(voters).items():
            try:
                pooled[by_token[token].security_id] = pool_agent_verdicts(
                    group,
                    base_rates=inputs.base_rates,
                    config=self._cfg.pipeline.committee,
                    history=inputs.history,
                )
            except NoVotingAgentsError:
                log.info("no voting agent for %s at %s", token, as_of)

        ranked = sorted(pooled, key=lambda s: (-pooled[s].pool(self._primary).logit, s))
        red_ids = set(ranked[:RED_TEAM_TOP_N])
        if red_ids:
            second = call(
                [e for e in inputs.entities if e.security_id in red_ids],
                red_ids,
                first.completed,
            )
            if second.status is RunStatus.PARTIAL:
                return partial("red team stage aborted")

        fits = self._fits(inputs)
        fit = fits[self._primary]
        bear: dict[int, BearSeverity | None] = {
            by_token[r.entity_token].security_id: r.bear_severity for r in reds
        }
        book = size_committee_book(
            run_id=run_id,
            as_of=as_of,
            pooled=pooled,
            bear=bear,
            sectors={e.security_id: e.sector for e in inputs.entities},
            volatilities=_volatilities(inputs.entities),
            fit=fit,
            config=self._cfg.risk,
        )
        try:
            cio: CioResult = run_cio(
                self._cio_client,
                book,
                models=self._cfg.models,
                run_id=run_id,
                red_team={r.entity_token: r for r in reds},
                max_veto_pct=float(self._cfg.risk.cio_veto_budget),
                dlq=dlq,
                budget=budget,
            )
        except BudgetExceededError:
            return partial("budget exceeded at CIO")

        final = cio.book
        records = self._decision_records(run_id, as_of, pooled, fits, bear, final, cio, fit.active)
        snapshot = PortfolioSnapshot(
            run_id=run_id, as_of=as_of, book=final, cash_weight=final.cash_weight, cio=cio.decision
        )
        now = self._clock()
        self._sink.flush_step(
            StepArtifacts(
                run=RunRecord(
                    run_id=run_id,
                    mode=RunMode.BACKTEST,
                    as_of=as_of,
                    config_hash=self._hash,
                    status=RunStatus.COMMITTED,
                    started_at=started,
                    ended_at=now,
                    total_cost_usd=budget.spent_usd,
                ),
                verdicts=_verdict_records(inputs.entities, voters, reds),
                decisions=records,
                portfolio=snapshot,
                commitment=DecisionCommitment(
                    run_id=run_id, sha256=_commitment_hash(records, final), committed_at=now
                ),
                dlq=_dlq_records(run_id, as_of, dlq),
            )
        )
        return StepResult(run_id, as_of, RunStatus.COMMITTED, final, budget.spent_usd)

    # ------------------------------------------------------------------------------------------

    def _fits(self, inputs: StepInputs) -> dict[Horizon, CalibrationFit]:
        cfg, fits = self._cfg.pipeline.committee, {}
        for h in self._cfg.pipeline.horizons:
            obs = inputs.observations.get(h, ())
            periods = inputs.independent_periods.get(h, 0)
            if obs:
                fits[h] = fit_stacker(obs, horizon=h, independent_periods=periods, config=cfg)
            else:
                fits[h] = CalibrationFit(
                    horizon=h,
                    alpha=0.0,
                    beta=1.0,
                    active=False,
                    independent_periods=periods,
                    observations=0,
                    base_rate=None,
                )
        return fits

    def _decision_records(
        self,
        run_id: UUID,
        as_of: datetime,
        pooled: Mapping[int, PooledForecast],
        fits: Mapping[Horizon, CalibrationFit],
        bear: Mapping[int, BearSeverity | None],
        final: ProposedBook,
        cio: CioResult,
        calibrated: bool,
    ) -> tuple[CommitteeDecisionRecord, ...]:
        weights = {p.security_id: p.target_weight for p in final.positions}
        actions = (
            {d.entity_token: d.action for d in cio.decision.decisions}
            if cio.decision is not None and cio.alert is None
            else {}
        )
        mode = SizingMode.CALIBRATED if calibrated else SizingMode.RANK
        out = []
        for sid, forecast in sorted(pooled.items()):
            for pool in forecast.pools:
                fit = fits[pool.horizon]
                out.append(
                    CommitteeDecisionRecord(
                        decision=CommitteeDecision(
                            run_id=run_id,
                            security_id=sid,
                            entity_token=forecast.entity_token,
                            as_of=as_of,
                            horizon_days=pool.horizon,
                            pooled_p=sigmoid(fit.alpha + fit.beta * pool.logit),
                            dispersion=pool.dispersion,
                            agent_weights=pool.weights,
                            bear_severity=bear.get(sid),
                            target_weight=weights.get(sid, 0.0),
                        ),
                        pooled_logit=pool.logit,
                        sizing_mode=mode,
                        cio_action=actions.get(forecast.entity_token),
                        rationale=cio.decision.rationale if cio.decision else None,
                    )
                )
        return tuple(out)

    def _flush_partial(
        self,
        run_id: UUID,
        as_of: datetime,
        started: datetime,
        entities: Sequence[EntityData],
        voters: Sequence[AgentVerdict],
        reds: Sequence[RedTeamVerdict],
        dlq: CollectingDLQ,
        budget: RunBudget,
        reason: str,
    ) -> StepResult:
        """Flush what exists; no decisions or commitment, because no complete book was formed."""
        self._sink.flush_step(
            StepArtifacts(
                run=RunRecord(
                    run_id=run_id,
                    mode=RunMode.BACKTEST,
                    as_of=as_of,
                    config_hash=self._hash,
                    status=RunStatus.PARTIAL,
                    started_at=started,
                    ended_at=self._clock(),
                    status_reason=reason[:1000],
                    total_cost_usd=budget.spent_usd,
                ),
                verdicts=_verdict_records(entities, voters, reds),
                dlq=_dlq_records(run_id, as_of, dlq),
            )
        )
        return StepResult(run_id, as_of, RunStatus.PARTIAL, None, budget.spent_usd, reason)


def _group(verdicts: Sequence[AgentVerdict]) -> dict[str, list[AgentVerdict]]:
    groups: dict[str, list[AgentVerdict]] = {}
    for v in verdicts:
        groups.setdefault(v.entity_token, []).append(v)
    return groups


def _volatilities(entities: Sequence[EntityData]) -> dict[int, float]:
    out: dict[int, float] = {}
    for e in entities:
        v = e.features.values.get(VOL_FEATURE)
        if isinstance(v, int | float) and v > 0:
            out[e.security_id] = float(v)
    return out


def _verdict_records(
    entities: Sequence[EntityData],
    voters: Sequence[AgentVerdict],
    reds: Sequence[RedTeamVerdict],
) -> tuple[VerdictRecord, ...]:
    """The runner does not surface per-call tokens/latency/cost; the run row carries total cost."""
    sid = {e.entity_token: e.security_id for e in entities}
    every: list[AgentVerdict | RedTeamVerdict] = [*voters, *reds]
    return tuple(
        VerdictRecord(
            security_id=sid[v.entity_token],
            verdict=v,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_ms=0,
        )
        for v in every
    )


def _dlq_records(run_id: UUID, as_of: datetime, dlq: CollectingDLQ) -> tuple[DlqRecord, ...]:
    return tuple(
        DlqRecord(
            run_id=run_id,
            as_of=as_of,
            agent=r.get("agent", "unknown"),
            error_type=r.get("error", "unknown"),
            payload={k: v for k, v in r.items() if k not in ("agent", "error")},
        )
        for r in dlq.records
    )
