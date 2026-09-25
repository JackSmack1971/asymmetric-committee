"""Inter-stage messages. Every message shape in the system is defined here and nowhere else.

Models ending in ``LLM`` are what a model is asked to emit (their strict JSON Schema is exported by
``contracts.schema_export``). The full model adds the system-filled envelope, so the LLM can never
write ``model_served``, ``run_id`` or any weight (invariants 6 and 7).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

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
    p_outperform: Probability = Field(
        description="Probability (0 to 1) the entity beats its sector ETF over the horizon."
    )
    horizon_days: Horizon = Field(description="Scoring horizon in trading days.")
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


class RunRecord(Contract):
    run_id: UUID
    mode: RunMode
    as_of: AwareDatetime
    config_hash: Sha256Hex
    status: RunStatus
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    status_reason: str | None = Field(default=None, max_length=1000)

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

    @property
    def excess_return(self) -> float:
        return self.fwd_return - self.sector_fwd_return


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
