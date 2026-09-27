"""Backtest orchestrator: one weekly ``as_of`` step from partitions to an atomic sink flush (P5).

Reads are the caller's job (invariant 2): a ``StepLoader`` returns point-in-time ``StepInputs`` and
this module re-checks ``available_at <= as_of`` on everything it is handed. Nothing here talks to
the database; the finished step goes to ``DecisionSink.flush_step`` in one transaction.

Step order: alias guard -> voters -> pool -> red team on the top names -> stacker -> sizing -> CIO
-> commitment -> flush. A budget breach, any aborting failure, or a missing valid CIO reply ends the
step ``PARTIAL``: what exists is flushed, no book is committed, and a backtest stops stepping.

Run identity (invariant 8): every genuinely new attempt gets a fresh ``run_id``; only an explicit
``run_id`` resumes a run. Resume reads that run's stored verdicts (a row exists only for COMPLETED
work), so those tasks short-circuit while PARTIAL/FAILED work runs again. A run that already
reached ``COMMITTED`` is returned as is. Nothing is keyed on (config, as_of), so a failed run can
never block a fresh one.

Transactions: ``flush_step`` is the only persistence call and is atomic. ``COMMITTED`` is returned
only after the flush returned. If the flush (or any later stage) raises, the step tries to record
``FAILED`` with the completed verdicts, then re-raises; the store never demotes a committed run.

Anchoring and execution (P5): a ``COMMITTED`` run is handed to the ``AnchorStage`` (every mode:
backtests stop at ``ANCHORED``). Only a LIVE orchestrator with an ``ExecutionStage`` goes on, and
only for an ``ANCHORED`` run (fresh, or resumed by explicit ``run_id``). PARTIAL and FAILED runs
never reach either stage, and a run a kill-switch halt moved to PARTIAL is never resumed: recovery
is a new run. The execution stage reconciles against the broker, so a resume finishes existing
orders rather than sending new ones, and it alone moves the run to ``EXECUTED``. Stage failures
leave the run where it was and propagate; they are never routed through the ``FAILED`` recorder.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from agents.base import (
    BudgetExceededError,
    CallTelemetry,
    ChatClient,
    RunBudget,
    UnknownServedModelError,
    VerdictCache,
)
from agents.cio import CioResult, CioTierError, run_cio
from agents.partitions import EntityData, Partitioner
from agents.runner import RunnerResult, run_agents, task_key
from committee.pooling import History, NoVotingAgentsError, pool_agent_verdicts
from committee.stacker import Observation, fit_stacker, sigmoid
from config.loader import AppConfig, UsageUnavailableError
from contracts.commitment import commitment_hash
from contracts.data import Security
from contracts.enums import (
    HALT_REASON_PREFIX,
    AgentName,
    BearSeverity,
    Horizon,
    RunMode,
    RunStatus,
    SizingMode,
)
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
from orchestration.anchor_stage import AnchorResult
from orchestration.execution_stage import ExecutionResult
from risk import size_committee_book
from universe.snapshot import assert_alias_coverage

log = logging.getLogger(__name__)

RED_TEAM_TOP_N = 15  # §7.4: the red team reviews the strongest candidates only
VOL_FEATURE = "realized_vol_20d"  # annualized (P2)
# Failures that cannot yield a valid CIO reply or complete accounting: the run ends PARTIAL.
_CIO_ABORTS = (BudgetExceededError, UsageUnavailableError, UnknownServedModelError, CioTierError)
_ADVANCED = (RunStatus.COMMITTED, RunStatus.ANCHORED, RunStatus.EXECUTED, RunStatus.SCORED)
# Stopping points of a healthy step: backtests end at ANCHORED, live runs go on to EXECUTED.
_STEP_DONE = (RunStatus.COMMITTED, RunStatus.ANCHORED, RunStatus.EXECUTED)


class LookAheadError(RuntimeError):
    """A row with ``available_at > as_of`` reached the orchestrator (invariant 3)."""


class RunMismatchError(RuntimeError):
    """An explicit ``run_id`` names a stored run that does not match this as_of, mode or config."""


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


class Anchors(Protocol):
    def anchor_run(self, run_id: UUID) -> AnchorResult: ...


class Executes(Protocol):
    def execute_run(self, run_id: UUID) -> ExecutionResult: ...


class StepSink(Protocol):
    def flush_step(self, step: StepArtifacts) -> None: ...

    def load_run(self, run_id: UUID) -> RunRecord | None: ...

    def load_verdicts(self, run_id: UUID) -> list[VerdictRecord]: ...

    def load_book(self, run_id: UUID) -> ProposedBook | None: ...


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


@dataclass
class _Attempt:
    """Mutable state of one execution of a run (a fresh run or a resume of an existing one)."""

    run_id: UUID
    as_of: datetime
    started: datetime
    inputs: StepInputs
    budget: RunBudget
    dlq: CollectingDLQ
    voters: list[AgentVerdict] = field(default_factory=list)
    reds: list[RedTeamVerdict] = field(default_factory=list)
    stored: set[str] = field(default_factory=set)  # task keys whose verdict is already persisted
    telemetry: dict[str, CallTelemetry] = field(default_factory=dict)


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
        new_run_id: Callable[[], UUID] = uuid4,
        concurrency: int = 16,
        mode: RunMode = RunMode.BACKTEST,
        anchoring: Anchors | None = None,
        execution: Executes | None = None,
    ) -> None:
        if execution is not None and mode is not RunMode.LIVE:
            raise ValueError("only a LIVE orchestrator may execute; backtests stop at ANCHORED")
        if execution is not None and anchoring is None:
            raise ValueError("execution needs anchoring: only an ANCHORED run may execute")
        self._mode = mode
        self._anchoring = anchoring
        self._execution = execution
        self._cfg = config
        self._loader = loader
        self._sink = sink
        self._client_for = client_for
        self._cio_client = cio_client
        self._primary = primary_horizon
        self._cache = cache
        self._clock = clock
        self._new_run_id = new_run_id
        self._concurrency = concurrency
        self._hash = config_fingerprint(config)

    def run_backtest(
        self, start: datetime, end: datetime, *, resume: Mapping[datetime, UUID] | None = None
    ) -> list[StepResult]:
        """Weekly steps; each gets a fresh run id unless ``resume`` names one for that ``as_of``."""
        if self._mode is not RunMode.BACKTEST:
            raise ValueError("run_backtest requires a BACKTEST orchestrator")
        results: list[StepResult] = []
        for as_of in weekly_steps(start, end):
            result = self.run_step(as_of, run_id=(resume or {}).get(as_of))
            results.append(result)
            if result.status not in _STEP_DONE:
                break
        return results

    # ------------------------------------------------------------------------------------------

    def run_step(self, as_of: datetime, *, run_id: UUID | None = None) -> StepResult:
        """One step. ``run_id=None`` starts a new run; an explicit id resumes that run."""
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        prior = self._sink.load_run(run_id) if run_id is not None else None
        if run_id is None:
            run_id = self._new_run_id()
        if prior is not None:
            self._check_same_run(prior, as_of)
            if _halted(prior):  # terminal: recovery from a halt is a new run, never a resume
                book = self._sink.load_book(run_id)
                return StepResult(
                    run_id, as_of, prior.status, book, prior.total_cost_usd, prior.status_reason
                )
            if prior.status in _ADVANCED:  # COMPLETED work short-circuits
                book = self._sink.load_book(run_id)
                done = StepResult(run_id, as_of, prior.status, book, prior.total_cost_usd)
                return self._maybe_execute(done)  # a resumed run anchors/reconciles, not resends

        inputs = self._loader.load(as_of)
        assert_point_in_time(inputs, as_of)
        assert_alias_coverage(inputs.securities, inputs.brands)  # fail closed before any prompt

        attempt = _Attempt(
            run_id=run_id,
            as_of=as_of,
            started=prior.started_at if prior else self._clock(),
            inputs=inputs,
            budget=RunBudget(
                self._cfg.pipeline.budgets.run_budget_usd,
                prior.total_cost_usd if prior else 0.0,
            ),
            dlq=CollectingDLQ(),
        )
        if prior is not None:
            self._seed_from_store(attempt)
        try:
            result = self._execute(attempt)
        except Exception as exc:
            self._record_failure(attempt, exc)
            raise
        return self._maybe_execute(result)

    def _maybe_execute(self, result: StepResult) -> StepResult:
        """``COMMITTED -> ANCHORED`` (every mode), then ``ANCHORED -> EXECUTED`` (live only).

        Stage errors propagate and leave the run where it was; nothing here demotes it.
        """
        if result.status is RunStatus.COMMITTED and self._anchoring is not None:
            anchored = self._anchoring.anchor_run(result.run_id)
            result = StepResult(
                result.run_id, result.as_of, anchored.status, result.book, result.cost_usd
            )
        if (
            self._execution is None
            or self._mode is not RunMode.LIVE
            or result.status is not RunStatus.ANCHORED
        ):
            return result
        out = self._execution.execute_run(result.run_id)  # raises leave the run ANCHORED
        return StepResult(
            result.run_id,
            result.as_of,
            out.status,
            result.book,
            result.cost_usd,
            out.reason,
        )

    def _check_same_run(self, prior: RunRecord, as_of: datetime) -> None:
        if (prior.mode, prior.as_of, prior.config_hash) != (self._mode, as_of, self._hash):
            raise RunMismatchError(
                f"run {prior.run_id} was {prior.mode.value} at {prior.as_of} under config "
                f"{prior.config_hash[:8]}; cannot resume it as {self._mode.value} at {as_of} under "
                f"{self._hash[:8]}"
            )

    def _seed_from_store(self, a: _Attempt) -> None:
        """Stored verdicts are COMPLETED tasks: reuse them, do not call the model again."""
        sid_by_token = {e.entity_token: e.security_id for e in a.inputs.entities}
        for rec in self._sink.load_verdicts(a.run_id):
            v = rec.verdict
            if sid_by_token.get(v.entity_token) != rec.security_id:
                raise RunMismatchError(
                    f"stored verdict for {v.entity_token} is not in this universe"
                )
            (a.reds if isinstance(v, RedTeamVerdict) else a.voters).append(v)  # type: ignore[arg-type]
            a.stored.add(_verdict_key(a.run_id, v, rec.security_id))

    def _execute(self, a: _Attempt) -> StepResult:
        run_id, as_of, inputs = a.run_id, a.as_of, a.inputs
        by_token = {e.entity_token: e for e in inputs.entities}

        def collect(v: AgentVerdict | RedTeamVerdict) -> None:
            (a.reds if isinstance(v, RedTeamVerdict) else a.voters).append(v)  # type: ignore[arg-type]

        def call(entities: Sequence[EntityData], red_ids: set[int], done: set[str]) -> RunnerResult:
            result = asyncio.run(
                run_agents(
                    run_id=run_id,
                    mode=self._mode,
                    models=self._cfg.models,
                    config_hash=self._hash,
                    partitioner=inputs.partitioner,
                    entities=entities,
                    client_for=self._client_for,
                    sink=collect,
                    red_team_ids=red_ids,
                    completed=done,
                    cache=self._cache,
                    dlq=a.dlq,
                    budget=a.budget,
                    concurrency=self._concurrency,
                )
            )
            a.telemetry.update(result.telemetry)
            return result

        done = set(a.stored)
        first = call(inputs.entities, set(), done)
        if first.status is RunStatus.PARTIAL:
            return self._flush_partial(
                a, "voting stage aborted: " + ", ".join(sorted(set(first.aborted.values())))
            )
        done |= first.completed

        pooled: dict[int, PooledForecast] = {}
        for token, group in _group(a.voters).items():
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
            second = call([e for e in inputs.entities if e.security_id in red_ids], red_ids, done)
            if second.status is RunStatus.PARTIAL:
                return self._flush_partial(a, "red team stage aborted")

        fits = self._fits(inputs)
        fit = fits[self._primary]
        bear: dict[int, BearSeverity | None] = {
            by_token[r.entity_token].security_id: r.bear_severity for r in a.reds
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
                red_team={r.entity_token: r for r in a.reds},
                max_veto_pct=float(self._cfg.risk.cio_veto_budget),
                dlq=a.dlq,
                budget=a.budget,
            )
        except _CIO_ABORTS as exc:
            return self._flush_partial(a, f"CIO aborted: {type(exc).__name__}")
        if book.positions and cio.decision is None:
            # No valid CIO reply is not an approval ("no CIO" is an ablation, not the fallback).
            return self._flush_partial(a, "CIO produced no valid decision")

        final = cio.book
        records = self._decision_records(run_id, as_of, pooled, fits, bear, final, cio, fit.active)
        snapshot = PortfolioSnapshot(
            run_id=run_id, as_of=as_of, book=final, cash_weight=final.cash_weight, cio=cio.decision
        )
        now = self._clock()
        committed_run = self._run_record(a, RunStatus.COMMITTED, ended_at=now)
        self._sink.flush_step(
            StepArtifacts(
                run=committed_run,
                verdicts=self._new_verdict_records(a, complete=True),
                decisions=records,
                portfolio=snapshot,
                commitment=DecisionCommitment(
                    run_id=run_id,
                    sha256=commitment_hash(committed_run, records, snapshot),
                    committed_at=now,
                ),
                dlq=_dlq_records(run_id, as_of, a.dlq),
            )
        )
        return StepResult(run_id, as_of, RunStatus.COMMITTED, final, a.budget.spent_usd)

    # ------------------------------------------------------------------------------------------

    def _run_record(
        self, a: _Attempt, status: RunStatus, *, ended_at: datetime, reason: str | None = None
    ) -> RunRecord:
        return RunRecord(
            run_id=a.run_id,
            mode=self._mode,
            as_of=a.as_of,
            config_hash=self._hash,
            status=status,
            started_at=a.started,
            ended_at=ended_at,
            status_reason=None if reason is None else reason[:1000],
            total_cost_usd=a.budget.spent_usd,
        )

    def _new_verdict_records(self, a: _Attempt, *, complete: bool) -> tuple[VerdictRecord, ...]:
        """Records for verdicts not yet stored, with the accounting the runner measured.

        A verdict without telemetry is never given made-up numbers: it is an error, except while
        recording a failure, where it is left out (the task is not COMPLETED and runs again).
        """
        sid = {e.entity_token: e.security_id for e in a.inputs.entities}
        out: list[VerdictRecord] = []
        every: list[AgentVerdict | RedTeamVerdict] = [*a.voters, *a.reds]
        for v in every:
            key = _verdict_key(a.run_id, v, sid[v.entity_token])
            if key in a.stored:
                continue
            tel = a.telemetry.get(key)
            if tel is None:
                if complete:
                    raise RuntimeError(f"no call telemetry for completed task {key}")
                continue
            out.append(
                VerdictRecord(
                    security_id=sid[v.entity_token],
                    verdict=v,
                    tokens_in=tel.tokens_in,
                    tokens_out=tel.tokens_out,
                    cost_usd=tel.cost_usd,
                    latency_ms=tel.latency_ms,
                )
            )
        return tuple(out)

    def _flush_partial(self, a: _Attempt, reason: str) -> StepResult:
        """Flush what exists; no decisions or commitment, because no complete book was formed."""
        self._sink.flush_step(
            StepArtifacts(
                run=self._run_record(a, RunStatus.PARTIAL, ended_at=self._clock(), reason=reason),
                verdicts=self._new_verdict_records(a, complete=True),
                dlq=_dlq_records(a.run_id, a.as_of, a.dlq),
            )
        )
        return StepResult(a.run_id, a.as_of, RunStatus.PARTIAL, None, a.budget.spent_usd, reason)

    def _record_failure(self, a: _Attempt, exc: Exception) -> None:
        """Best effort: mark the run FAILED and keep completed verdicts so a resume can reuse them.

        Runs after the failed flush rolled back, so it cannot claim more than what persisted. If
        this write fails too, the original error is what the caller sees.
        """
        try:
            self._sink.flush_step(
                StepArtifacts(
                    run=self._run_record(
                        a,
                        RunStatus.FAILED,
                        ended_at=self._clock(),
                        reason=f"{type(exc).__name__}: {exc}",
                    ),
                    verdicts=self._new_verdict_records(a, complete=False),
                    dlq=_dlq_records(a.run_id, a.as_of, a.dlq),
                )
            )
        except Exception:
            log.exception("could not record FAILED for run %s", a.run_id)

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


def _verdict_key(run_id: UUID, v: AgentVerdict | RedTeamVerdict, security_id: int) -> str:
    agent = AgentName.RED_TEAM if isinstance(v, RedTeamVerdict) else v.agent
    return task_key(run_id, agent, security_id)


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


def _halted(run: RunRecord) -> bool:
    return run.status is RunStatus.PARTIAL and (run.status_reason or "").startswith(
        HALT_REASON_PREFIX
    )
