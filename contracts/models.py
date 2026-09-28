"""Inter-stage messages. Every message shape in the system is defined here and nowhere else.

Models ending in ``LLM`` are what a model is asked to emit (their strict JSON Schema is exported by
``contracts.schema_export``). The full model adds the system-filled envelope, so the LLM can never
write ``model_served``, ``run_id`` or any weight (invariants 6 and 7).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

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
    OutcomeCompleteness,
    ReferenceSource,
    RunMode,
    RunStatus,
    SizingMode,
    Stage,
    Stance,
)

# Hard cap on any single position (§8.1). config/risk.yaml may be tighter, never looser.
MAX_POSITION = 0.08

Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
Weight = Annotated[float, Field(ge=0.0, le=MAX_POSITION, allow_inf_nan=False)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
NonNegative = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
SecurityId = Annotated[int, Field(ge=1)]
EntityToken = Annotated[str, Field(pattern=r"^[A-Z]+_[0-9]{2,}$", max_length=32)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Label = Annotated[str, Field(min_length=1, max_length=128)]
ShortText = Annotated[str, Field(min_length=1, max_length=280)]
LongText = Annotated[str, Field(min_length=1, max_length=4000)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- LLM-facing ------------------------------------------------------------------------------


class EvidenceRef(Contract):
    """A citation of one row in the agent's input partition."""

    source: FeedName = Field(description="Input partition the cited row belongs to.")
    row_id: Label = Field(description="Row ID exactly as shown in the input table.")
    note: ShortText = Field(description="What this row shows, in one sentence.")


class AgentVerdictLLM(Contract):
    stance: Stance
    p_outperform_5: Probability = Field(
        description="Probability (0 to 1) the entity beats its sector ETF over 5 trading days."
    )
    p_outperform_21: Probability = Field(
        description="Probability (0 to 1) the entity beats its sector ETF over 21 trading days."
    )
    p_outperform_63: Probability = Field(
        description="Probability (0 to 1) the entity beats its sector ETF over 63 trading days."
    )
    key_evidence: tuple[EvidenceRef, ...] = Field(
        min_length=1, max_length=5, description="1 to 5 citations of input rows."
    )
    risks: tuple[ShortText, ...] = Field(max_length=3, description="At most 3 key risks.")
    data_sufficiency: DataSufficiency


class RedTeamVerdictLLM(Contract):
    bear_severity: BearSeverity
    falsifiable_risk: ShortText = Field(
        description="One specific risk that can be checked against later data."
    )
    horizon_days: Horizon = Field(description="Scoring horizon in trading days.")
    key_evidence: tuple[EvidenceRef, ...] = Field(
        min_length=1, max_length=5, description="1 to 5 citations of input rows."
    )


class CioNameDecisionLLM(Contract):
    entity_token: EntityToken
    action: CioAction
    reason: str = Field(
        max_length=1000, description="Required for veto and flag_for_review; empty for approve."
    )

    @model_validator(mode="after")
    def _reason_when_not_approved(self) -> Self:
        if self.action is not CioAction.APPROVE and not self.reason.strip():
            raise ValueError(f"{self.action.value} requires a reason")
        return self


class CioDecisionLLM(Contract):
    decisions: tuple[CioNameDecisionLLM, ...] = Field(
        description="Exactly one decision per proposed name."
    )
    rationale: LongText = Field(description="Portfolio rationale for the dashboard.")

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        tokens = [d.entity_token for d in self.decisions]
        if len(tokens) != len(set(tokens)):
            raise ValueError("duplicate entity_token in CIO decisions")
        return self


LLM_OUTPUT_MODELS: tuple[type[Contract], ...] = (AgentVerdictLLM, RedTeamVerdictLLM, CioDecisionLLM)


# --- System-filled envelopes -----------------------------------------------------------------


class AgentVerdict(AgentVerdictLLM):
    """Exactly §3.1: the LLM output plus the envelope filled by agents/base.py."""

    run_id: UUID
    agent: AgentName
    entity_token: EntityToken
    as_of: AwareDatetime
    prompt_version: Label
    model_served: Label  # from response.model, never the requested model
    valid: bool  # system-owned; cutoff validity is evaluated only for backtests

    @classmethod
    def from_llm(
        cls,
        out: AgentVerdictLLM,
        *,
        run_id: UUID,
        agent: AgentName,
        entity_token: str,
        as_of: datetime,
        prompt_version: str,
        model_served: str,
        valid: bool,
    ) -> Self:
        return cls.model_validate(
            {
                **out.model_dump(),
                "run_id": run_id,
                "agent": agent,
                "entity_token": entity_token,
                "as_of": as_of,
                "prompt_version": prompt_version,
                "model_served": model_served,
                "valid": valid,
            }
        )

    @model_validator(mode="after")
    def _voting_agent(self) -> Self:
        if self.agent not in VOTING_AGENTS:
            raise ValueError(f"{self.agent.value} does not emit AgentVerdict")
        return self


class RedTeamVerdict(RedTeamVerdictLLM):
    run_id: UUID
    agent: Literal[AgentName.RED_TEAM] = AgentName.RED_TEAM
    entity_token: EntityToken
    as_of: AwareDatetime
    prompt_version: Label
    model_served: Label
    valid: bool

    @classmethod
    def from_llm(
        cls,
        out: RedTeamVerdictLLM,
        *,
        run_id: UUID,
        entity_token: str,
        as_of: datetime,
        prompt_version: str,
        model_served: str,
        valid: bool,
    ) -> Self:
        return cls.model_validate(
            {
                **out.model_dump(),
                "run_id": run_id,
                "entity_token": entity_token,
                "as_of": as_of,
                "prompt_version": prompt_version,
                "model_served": model_served,
                "valid": valid,
            }
        )


class CioDecision(CioDecisionLLM):
    run_id: UUID
    agent: Literal[AgentName.CIO] = AgentName.CIO
    as_of: AwareDatetime
    prompt_version: Label
    model_served: Label

    @classmethod
    def from_llm(
        cls,
        out: CioDecisionLLM,
        *,
        run_id: UUID,
        as_of: datetime,
        prompt_version: str,
        model_served: str,
    ) -> Self:
        return cls.model_validate(
            {
                **out.model_dump(),
                "run_id": run_id,
                "as_of": as_of,
                "prompt_version": prompt_version,
                "model_served": model_served,
            }
        )


# --- Deterministic stages --------------------------------------------------------------------


class GateDecision(Contract):
    run_id: UUID
    security_id: SecurityId
    as_of: AwareDatetime
    feature_set_version: Label
    gate_model_version: Label
    score: Probability
    passed: bool
    components: tuple[tuple[Label, Finite], ...] = ()


class AgentWeight(Contract):
    agent: AgentName
    weight: NonNegative  # pooling weight w_a (§7), not a portfolio weight


class CommitteeDecision(Contract):
    run_id: UUID
    security_id: SecurityId
    entity_token: EntityToken
    as_of: AwareDatetime
    horizon_days: Horizon
    pooled_p: Probability
    dispersion: NonNegative  # std of agent logits
    agent_weights: tuple[AgentWeight, ...] = Field(min_length=1)
    bear_severity: BearSeverity | None
    target_weight: Weight

    @model_validator(mode="after")
    def _voting_weights(self) -> Self:
        agents = [w.agent for w in self.agent_weights]
        if len(agents) != len(set(agents)):
            raise ValueError("duplicate agent in agent_weights")
        baseline = agents == [AgentName.QUANT_BASELINE]
        if not baseline and not set(agents) <= VOTING_AGENTS:
            raise ValueError("only voting agents carry pooling weights")
        return self


class HorizonPool(Contract):
    """Pooled logit for one horizon (§7.1). ``weights`` are voting agents only."""

    horizon: Horizon
    logit: Finite  # L = sum_a w_a * z_a, before the stacker
    lambda_t: Probability  # shrinkage toward equal weights actually applied
    dispersion: NonNegative  # std of the agent logits
    weights: tuple[AgentWeight, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _voting_weights(self) -> Self:
        agents = [w.agent for w in self.weights]
        if len(agents) != len(set(agents)) or not set(agents) <= VOTING_AGENTS:
            raise ValueError("weights must be unique voting agents")
        return self


class PooledForecast(Contract):
    """Committee pooling output for one entity, one pool per horizon."""

    entity_token: EntityToken
    pools: tuple[HorizonPool, ...] = Field(min_length=1)

    def pool(self, horizon: Horizon) -> HorizonPool:
        for p in self.pools:
            if p.horizon == horizon:
                return p
        raise KeyError(horizon)

    def weights(self, horizon: Horizon) -> dict[AgentName, float]:
        return {w.agent: w.weight for w in self.pool(horizon).weights}

    @property
    def weights_d5(self) -> dict[AgentName, float]:
        return self.weights(Horizon.D5)

    @property
    def weights_d21(self) -> dict[AgentName, float]:
        return self.weights(Horizon.D21)

    @property
    def weights_d63(self) -> dict[AgentName, float]:
        return self.weights(Horizon.D63)

    @property
    def logit_d5(self) -> float:
        return self.pool(Horizon.D5).logit

    @property
    def logit_d21(self) -> float:
        return self.pool(Horizon.D21).logit

    @property
    def logit_d63(self) -> float:
        return self.pool(Horizon.D63).logit


class CalibrationFit(Contract):
    """Walk-forward stacker fit for one horizon (§7.2): ``p_cal = sigmoid(alpha + beta * L)``."""

    horizon: Horizon
    alpha: Finite
    beta: Finite
    active: bool  # False = pass-through (alpha 0, beta 1) before T_s independent periods
    independent_periods: int = Field(ge=0)
    observations: int = Field(ge=0)
    base_rate: Probability | None  # trailing base rate anchoring alpha; None when not fitted


class BenchmarkForecastBundle(Contract):
    """Immutable whole-name inputs used by P6.6 random-committee permutations."""

    security_id: SecurityId
    entity_token: EntityToken
    agent_verdicts: tuple[AgentVerdict, ...] = Field(min_length=1)
    pooled_forecast: PooledForecast
    bear_severity: BearSeverity | None

    @model_validator(mode="after")
    def _same_entity(self) -> Self:
        if self.pooled_forecast.entity_token != self.entity_token:
            raise ValueError("pooled forecast token does not match benchmark bundle")
        if any(verdict.entity_token != self.entity_token for verdict in self.agent_verdicts):
            raise ValueError("agent verdict token does not match benchmark bundle")
        if any(verdict.run_id != self.agent_verdicts[0].run_id for verdict in self.agent_verdicts):
            raise ValueError("benchmark bundle spans runs")
        agents = [verdict.agent for verdict in self.agent_verdicts]
        if len(agents) != len(set(agents)):
            raise ValueError("benchmark bundle has duplicate agent verdicts")
        return self


class BenchmarkReplayContext(Contract):
    """Durable run-time sizing and version evidence, outside P6.5 commitment material."""

    run_id: UUID
    random_bundles: tuple[BenchmarkForecastBundle, ...] = Field(min_length=1)
    calibration_fits: tuple[CalibrationFit, ...] = Field(min_length=1)
    sizing_horizon: Horizon
    risk_config_snapshot: dict[str, Any]
    feature_set_versions: tuple[Label, ...] = Field(min_length=1)
    # Empty means the producer did not supply verifiable gate evidence; scoring fails closed.
    gate_model_versions: tuple[Label, ...]
    evaluation_parameters_sha256: Sha256Hex

    @model_validator(mode="after")
    def _canonical_evidence(self) -> Self:
        horizons = [fit.horizon for fit in self.calibration_fits]
        if len(horizons) != len(set(horizons)):
            raise ValueError("benchmark replay context has duplicate calibration horizons")
        if self.sizing_horizon not in horizons:
            raise ValueError("replay context lacks the configured sizing horizon fit")
        if len({bundle.security_id for bundle in self.random_bundles}) != len(self.random_bundles):
            raise ValueError("benchmark replay context has duplicate random bundles")
        if any(
            verdict.run_id != self.run_id
            for bundle in self.random_bundles
            for verdict in bundle.agent_verdicts
        ):
            raise ValueError("benchmark replay bundle belongs to another run")
        if tuple(sorted(self.feature_set_versions)) != self.feature_set_versions:
            raise ValueError("feature-set versions must be sorted")
        if tuple(sorted(self.gate_model_versions)) != self.gate_model_versions:
            raise ValueError("gate-model versions must be sorted")
        if len(set(self.feature_set_versions)) != len(self.feature_set_versions):
            raise ValueError("feature-set versions must be unique")
        if len(set(self.gate_model_versions)) != len(self.gate_model_versions):
            raise ValueError("gate-model versions must be unique")
        return self

    @property
    def evidence_sha256(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(canonical).hexdigest()


class ErrorCorrelation(Contract):
    """Agent error correlation and effective agent count for one horizon (§7.3)."""

    horizon: Horizon
    agents: int = Field(ge=2)
    observations: int = Field(ge=2)
    mean_correlation: Annotated[float, Field(ge=-1.0, le=1.0, allow_inf_nan=False)]
    n_eff: Positive
    monoculture_alert: bool  # n_eff < threshold


class ProposedPosition(Contract):
    security_id: SecurityId
    entity_token: EntityToken
    sector: Label
    pooled_p: Probability
    target_weight: Weight


class ProposedBook(Contract):
    run_id: UUID
    as_of: AwareDatetime
    positions: tuple[ProposedPosition, ...]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [p.security_id for p in self.positions]
        tokens = [p.entity_token for p in self.positions]
        if len(ids) != len(set(ids)) or len(tokens) != len(set(tokens)):
            raise ValueError("duplicate position")
        if self.gross_exposure > 1.0 + 1e-9:
            raise ValueError("gross exposure exceeds 1")
        return self

    @property
    def gross_exposure(self) -> float:
        return sum(p.target_weight for p in self.positions)

    @property
    def cash_weight(self) -> float:
        """Cash is the residual, never a choice (§8.1)."""
        return max(0.0, 1.0 - self.gross_exposure)


class OrderIntent(Contract):
    run_id: UUID
    security_id: SecurityId
    client_order_id: Label
    side: OrderSide
    qty: Positive
    target_weight: Weight
    decision_price: Positive
    limit_price: Positive
    time_in_force_minutes: int = Field(ge=1, le=390)


class FillReport(Contract):
    run_id: UUID
    security_id: SecurityId
    client_order_id: Label
    broker_order_id: Label
    side: OrderSide
    filled_qty: NonNegative
    decision_price: Positive
    arrival_price: Positive
    fill_price: Positive
    slippage_bps: Finite
    filled_at: AwareDatetime


class BrokerOrder(Contract):
    """The broker's authoritative view of one order (§9). Never synthesised locally."""

    broker_order_id: Label
    client_order_id: Label
    symbol: Label
    side: OrderSide
    kind: OrderKind
    qty: Positive
    filled_qty: NonNegative
    limit_price: Positive | None = None
    filled_avg_price: Positive | None = None
    status: BrokerOrderStatus
    submitted_at: AwareDatetime
    filled_at: AwareDatetime | None = None


class MarketSession(Contract):
    """One exchange trading session from the broker's official calendar (§9 scheduling)."""

    session_date: date
    opens_at: AwareDatetime
    closes_at: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.closes_at <= self.opens_at:
            raise ValueError("a session closes after it opens")
        return self


class ExecutionRecord(Contract):
    """Persisted execution evidence for one order (§4.3 orders, §9 logging).

    ``fill_price`` and ``slippage_bps`` are the broker's fill against the recorded reference; they
    are None until something fills. Paper slippage is not evidence of real costs (§9).
    """

    run_id: UUID
    security_id: SecurityId
    client_order_id: Label
    broker_order_id: Label
    kind: OrderKind
    side: OrderSide
    qty: Positive
    filled_qty: NonNegative
    limit_price: Positive | None = None
    decision_price: Positive
    reference_price: Positive
    reference_source: ReferenceSource
    fill_price: Positive | None = None
    slippage_bps: Finite | None = None
    status: BrokerOrderStatus
    submitted_at: AwareDatetime
    filled_at: AwareDatetime | None = None


class RunRecord(Contract):
    run_id: UUID
    mode: RunMode
    as_of: AwareDatetime
    config_hash: Sha256Hex
    status: RunStatus
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    status_reason: str | None = Field(default=None, max_length=1000)
    total_cost_usd: NonNegative = 0.0

    @model_validator(mode="after")
    def _times(self) -> Self:
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ended_at before started_at")
        return self


class TaskKey(Contract):
    """Idempotency key (invariant 8). security_id is None for run-level stages."""

    run_id: UUID
    stage: Stage
    security_id: SecurityId | None


class DecisionCommitment(Contract):
    run_id: UUID
    sha256: Sha256Hex
    committed_at: AwareDatetime


class OutcomeRecord(Contract):
    run_id: UUID
    security_id: SecurityId
    horizon: Horizon
    fwd_return: Annotated[float, Field(ge=-1.0, allow_inf_nan=False)]
    sector_fwd_return: Annotated[float, Field(ge=-1.0, allow_inf_nan=False)]
    scored_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    completeness: OutcomeCompleteness = OutcomeCompleteness.COMPLETE
    revision_id: int = Field(default=1, ge=1)
    evidence_revision_sha256: Sha256Hex | None = None

    @model_validator(mode="after")
    def _resolved(self) -> Self:
        if self.resolved_at is None:
            object.__setattr__(self, "resolved_at", self.scored_at)
        return self

    @property
    def excess_return(self) -> float:
        return self.fwd_return - self.sector_fwd_return


class ResolvedForecastOutcome(Contract):
    """One admitted, cutoff-resolved forecast/label pair for the stacker history feed."""

    run_id: UUID
    security_id: SecurityId
    agent: AgentName
    horizon: Horizon
    forecast: Probability
    outperformed: bool
    committed_at: AwareDatetime
    resolved_at: AwareDatetime


class AgentScore(Contract):
    """Rolling score per agent, served model and prompt version (§7, §10.3, §12.4)."""

    agent: AgentName
    model_served: Label | None  # None for the deterministic quant baseline
    prompt_version: Label | None
    horizon: Horizon
    window_start: date
    window_end: date
    n: int = Field(ge=0)
    brier: Probability
    ic: Annotated[float, Field(ge=-1.0, le=1.0, allow_inf_nan=False)] | None
    hit_rate: Probability

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.window_end < self.window_start:
            raise ValueError("window_end before window_start")
        return self


# --- Persistence records (written only by orchestration/sink.py) -------------------------------


class VerdictRecord(Contract):
    """One stored agent or red-team verdict plus its call accounting."""

    security_id: SecurityId
    verdict: AgentVerdict | RedTeamVerdict
    tokens_in: int | None = Field(ge=0)  # None = not reported / no call was made, never zero
    tokens_out: int | None = Field(ge=0)
    cost_usd: NonNegative
    latency_ms: int | None = Field(ge=0)


class CommitteeDecisionRecord(Contract):
    """A committee decision with the pooling detail and CIO outcome the table also keeps."""

    decision: CommitteeDecision
    pooled_logit: Finite | None = None
    sizing_mode: SizingMode | None = None
    cio_action: CioAction | None = None
    rationale: str | None = Field(default=None, max_length=4000)


class PortfolioSnapshot(Contract):
    """The final book after the CIO. Cash is the residual (§8.1), stored, never chosen."""

    run_id: UUID
    as_of: AwareDatetime
    book: ProposedBook
    cash_weight: Probability
    cio: CioDecision | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.book.run_id != self.run_id or self.book.as_of != self.as_of:
            raise ValueError("book does not belong to this snapshot")
        if abs(self.cash_weight - self.book.cash_weight) > 1e-9:
            raise ValueError("cash_weight is not the residual of the book")
        return self


class DlqRecord(Contract):
    run_id: UUID
    as_of: AwareDatetime
    agent: Label
    error_type: Label
    payload: dict[str, str] = Field(default_factory=dict)


class KillSwitchEvent(Contract):
    run_id: UUID
    triggered_at: AwareDatetime
    trigger: KillTrigger
    daily_loss: Finite | None = None  # fraction vs prior close equity
    peak_drawdown: Finite | None = None  # logged alongside, not a trigger
    cancelled_order_ids: tuple[Label, ...] = ()
    flattened: bool = False

    @model_validator(mode="after")
    def _only_manual_flattens(self) -> Self:
        if self.flattened and self.trigger is not KillTrigger.MANUAL:
            raise ValueError("only a manual halt flattens positions (§9)")
        return self


class CommitmentAnchor(Contract):
    model_config = ConfigDict(
        extra="forbid", frozen=True, ser_json_bytes="base64", val_json_bytes="base64"
    )

    run_id: UUID
    sha256: Sha256Hex
    ots_proof: bytes | None = None
    git_commit: Label | None = None
    anchored_at: AwareDatetime
    verified_at: AwareDatetime | None = None


class StepArtifacts(Contract):
    """Everything one ``as_of`` step produced; the sink writes it in a single transaction."""

    run: RunRecord
    verdicts: tuple[VerdictRecord, ...] = ()
    decisions: tuple[CommitteeDecisionRecord, ...] = ()
    portfolio: PortfolioSnapshot | None = None
    commitment: DecisionCommitment | None = None
    benchmark_replay_context: BenchmarkReplayContext | None = None
    dlq: tuple[DlqRecord, ...] = ()
    kill_switch: tuple[KillSwitchEvent, ...] = ()

    @model_validator(mode="after")
    def _one_run(self) -> Self:
        rid = self.run.run_id
        ids = [v.verdict.run_id for v in self.verdicts]
        ids += [d.decision.run_id for d in self.decisions] + [d.run_id for d in self.dlq]
        ids += [k.run_id for k in self.kill_switch]
        if self.portfolio:
            ids.append(self.portfolio.run_id)
        if self.commitment:
            ids.append(self.commitment.run_id)
        if self.benchmark_replay_context is not None:
            ids.append(self.benchmark_replay_context.run_id)
        if any(i != rid for i in ids):
            raise ValueError("artifact belongs to a different run")
        if self.benchmark_replay_context is not None and self.commitment is None:
            raise ValueError("benchmark replay context must be committed atomically")
        return self
