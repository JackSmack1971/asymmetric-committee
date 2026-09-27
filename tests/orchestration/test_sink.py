"""orchestration/sink.py: atomic per-step writes, idempotent replays, append-only commitments."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from contracts.commitment import commitment_hash
from contracts.enums import (
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
    Stance,
)
from contracts.models import (
    AgentVerdict,
    AgentWeight,
    CioDecision,
    CioNameDecisionLLM,
    CommitmentAnchor,
    CommitteeDecision,
    CommitteeDecisionRecord,
    DecisionCommitment,
    DlqRecord,
    EvidenceRef,
    ExecutionRecord,
    KillSwitchEvent,
    PortfolioSnapshot,
    ProposedBook,
    ProposedPosition,
    RedTeamVerdict,
    RunRecord,
    StepArtifacts,
    VerdictRecord,
)
from orchestration.sink import DecisionSink
from store.write import AnchorMismatchError, CommitmentMismatchError
from tests.store import factories as f

AS_OF = datetime(2024, 3, 1, 21, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
TOKEN = "ENTITY_01"


@pytest.fixture
def engine(pg_engine: Engine) -> Iterator[Engine]:
    def wipe() -> None:
        with pg_engine.begin() as c:
            c.execute(text("TRUNCATE runs, securities RESTART IDENTITY CASCADE"))

    wipe()
    yield pg_engine
    wipe()


@pytest.fixture
def sid(engine: Engine) -> int:
    with engine.begin() as c:
        return f.security(c)


def run_record(
    run_id: UUID, status: RunStatus = RunStatus.AGENTS_OK, cost: float = 0.0
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        mode=RunMode.BACKTEST,
        as_of=AS_OF,
        config_hash="c" * 64,
        status=status,
        started_at=AS_OF,
        total_cost_usd=cost,
    )


def verdict(run_id: UUID, agent: AgentName = AgentName.VALUE) -> AgentVerdict:
    return AgentVerdict(
        run_id=run_id,
        agent=agent,
        entity_token=TOKEN,
        as_of=AS_OF,
        prompt_version="v1-abcd1234",
        model_served="provider/served-model-2024-01",
        valid=True,
        stance=Stance.BUY,
        p_outperform_5=0.55,
        p_outperform_21=0.6,
        p_outperform_63=0.62,
        key_evidence=(EvidenceRef(source=FeedName.PRICE_BARS, row_id="r1", note="uptrend"),),
        risks=(),
        data_sufficiency=DataSufficiency.FULL,
    )


def red_team(run_id: UUID) -> RedTeamVerdict:
    return RedTeamVerdict(
        run_id=run_id,
        entity_token=TOKEN,
        as_of=AS_OF,
        prompt_version="v1-abcd1234",
        model_served="provider/strong-model",
        valid=True,
        bear_severity=BearSeverity.HIGH,
        falsifiable_risk="margins compress",
        horizon_days=Horizon.D21,
        key_evidence=(EvidenceRef(source=FeedName.PRICE_BARS, row_id="r1", note="rollover"),),
    )


def step(run_id: UUID, security_id: int, **overrides: object) -> StepArtifacts:
    book = ProposedBook(
        run_id=run_id,
        as_of=AS_OF,
        positions=(
            ProposedPosition(
                security_id=security_id,
                entity_token=TOKEN,
                sector="Tech",
                pooled_p=0.6,
                target_weight=0.05,
            ),
        ),
    )
    fields: dict[str, object] = {
        "run": run_record(run_id, cost=1.25),
        "verdicts": (
            VerdictRecord(
                security_id=security_id,
                verdict=verdict(run_id),
                tokens_in=100,
                tokens_out=20,
                cost_usd=0.01,
                latency_ms=350,
            ),
            VerdictRecord(
                security_id=security_id,
                verdict=red_team(run_id),
                tokens_in=200,
                tokens_out=40,
                cost_usd=0.05,
                latency_ms=900,
            ),
        ),
        "decisions": tuple(
            CommitteeDecisionRecord(
                decision=CommitteeDecision(
                    run_id=run_id,
                    security_id=security_id,
                    entity_token=TOKEN,
                    as_of=AS_OF,
                    horizon_days=h,
                    pooled_p=0.6,
                    dispersion=0.1,
                    agent_weights=(AgentWeight(agent=AgentName.VALUE, weight=1.0),),
                    bear_severity=BearSeverity.HIGH,
                    target_weight=0.05,
                ),
                pooled_logit=0.405,
                sizing_mode=SizingMode.CALIBRATED,
                cio_action=CioAction.APPROVE,
            )
            for h in (Horizon.D5, Horizon.D21)
        ),
        "portfolio": PortfolioSnapshot(
            run_id=run_id,
            as_of=AS_OF,
            book=book,
            cash_weight=book.cash_weight,
            cio=CioDecision(
                run_id=run_id,
                as_of=AS_OF,
                prompt_version="v1-cio",
                model_served="provider/strong-model",
                decisions=(
                    CioNameDecisionLLM(
                        entity_token=TOKEN, action=CioAction.FLAG_FOR_REVIEW, reason="x"
                    ),
                ),
                rationale="ok",
            ),
        ),
        "commitment": DecisionCommitment(run_id=run_id, sha256=SHA_A, committed_at=AS_OF),
        "dlq": (
            DlqRecord(
                run_id=run_id,
                as_of=AS_OF,
                agent="value",
                error_type="budget_exceeded",
                payload={"entity_token": "ENTITY_02"},
            ),
        ),
        "kill_switch": (
            KillSwitchEvent(
                run_id=run_id,
                triggered_at=AS_OF,
                trigger=KillTrigger.DAILY_LOSS,
                daily_loss=-0.031,
                peak_drawdown=0.035,
                cancelled_order_ids=("o1", "o2"),
            ),
        ),
    }
    fields.update(overrides)
    if "commitment" not in overrides:  # a real hash, so the run can be verified from its rows
        sha = commitment_hash(fields["run"], fields["decisions"], fields["portfolio"])  # type: ignore[arg-type]
        fields["commitment"] = DecisionCommitment(run_id=run_id, sha256=sha, committed_at=AS_OF)
    return StepArtifacts.model_validate(fields)


TABLES = (
    "runs",
    "agent_verdicts",
    "committee_decisions",
    "portfolio_snapshots",
    "decision_commitments",
    "dlq_records",
    "kill_switch_events",
)


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as c:
        return {t: int(c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()) for t in TABLES}


def test_flush_writes_the_whole_step(engine: Engine, sid: int) -> None:
    run_id = uuid4()
    DecisionSink(engine).flush_step(step(run_id, sid))
    assert counts(engine) == {
        "runs": 1,
        "agent_verdicts": 2,
        "committee_decisions": 2,
        "portfolio_snapshots": 1,
        "decision_commitments": 1,
        "dlq_records": 1,
        "kill_switch_events": 1,
    }
    with engine.connect() as c:
        served: Any = c.execute(
            text("SELECT verdict->>'model_served' FROM agent_verdicts WHERE agent = 'value'")
        ).scalar_one()
        cash: Any = c.execute(text("SELECT cash_weight FROM portfolio_snapshots")).scalar_one()
        total: Any = c.execute(text("SELECT total_cost_usd FROM runs")).scalar_one()
    assert served == "provider/served-model-2024-01"  # invariant 7: the served slug is stored
    assert cash == pytest.approx(0.95)
    assert total == pytest.approx(1.25)


def test_a_failure_mid_flush_leaves_nothing(engine: Engine, sid: int) -> None:
    """Decisions for an unknown security break the FK after the run and verdicts were written."""
    run_id = uuid4()
    good = step(run_id, sid)
    bad_decision = good.decisions[0].model_copy(
        update={
            "decision": good.decisions[0].decision.model_copy(update={"security_id": sid + 999})
        }
    )
    with pytest.raises(IntegrityError):
        DecisionSink(engine).flush_step(good.model_copy(update={"decisions": (bad_decision,)}))
    assert set(counts(engine).values()) == {0}


def test_replaying_a_step_is_a_noop_and_the_run_moves_forward(engine: Engine, sid: int) -> None:
    run_id = uuid4()
    sink = DecisionSink(engine)
    first = step(run_id, sid)
    sink.flush_step(first)
    before = counts(engine)
    later = first.model_copy(
        update={
            "run": run_record(run_id, RunStatus.COMMITTED, cost=2.5).model_copy(
                update={"ended_at": AS_OF}
            )
        }
    )
    sink.flush_step(later)
    assert counts(engine) == before
    with engine.connect() as c:
        status, cost = c.execute(text("SELECT status, total_cost_usd FROM runs")).one()
    assert (status, cost) == ("COMMITTED", pytest.approx(2.5))


def test_a_different_commitment_for_a_committed_run_is_refused(engine: Engine, sid: int) -> None:
    run_id = uuid4()
    sink = DecisionSink(engine)
    first = step(run_id, sid)
    assert first.commitment is not None
    sink.flush_step(first)
    other = DecisionCommitment(run_id=run_id, sha256=SHA_B, committed_at=AS_OF)
    with pytest.raises(CommitmentMismatchError):
        sink.flush_step(step(run_id, sid, commitment=other))
    with engine.connect() as c:
        stored: Any = c.execute(text("SELECT sha256 FROM decision_commitments")).scalar_one()
    assert stored == first.commitment.sha256


def test_anchor_upgrade_keeps_the_hash(engine: Engine, sid: int) -> None:
    run_id = uuid4()
    sink = DecisionSink(engine)
    first = step(run_id, sid)
    assert first.commitment is not None
    sha = first.commitment.sha256
    sink.flush_step(first)
    sink.record_anchor(CommitmentAnchor(run_id=run_id, sha256=sha, anchored_at=AS_OF))
    sink.record_anchor(
        CommitmentAnchor(run_id=run_id, sha256=sha, anchored_at=AS_OF, ots_proof=b"\x00proof")
    )
    with engine.connect() as c:
        proof: Any = c.execute(text("SELECT ots_proof FROM commitment_anchors")).scalar_one()
    assert bytes(proof) == b"\x00proof"
    with pytest.raises(AnchorMismatchError):
        sink.record_anchor(CommitmentAnchor(run_id=run_id, sha256=SHA_B, anchored_at=AS_OF))


def test_between_step_records_dedupe(engine: Engine, sid: int) -> None:
    run_id = uuid4()
    sink = DecisionSink(engine)
    first = step(run_id, sid)
    sink.flush_step(first)
    sink.record_dlq(first.dlq)
    sink.record_halt(first.kill_switch[0])
    assert counts(engine)["dlq_records"] == 1
    assert counts(engine)["kill_switch_events"] == 1
    manual = KillSwitchEvent(
        run_id=run_id, triggered_at=AS_OF, trigger=KillTrigger.MANUAL, flattened=True
    )
    sink.record_halt(manual)
    assert counts(engine)["kill_switch_events"] == 2


def test_step_artifacts_must_share_one_run(sid: int) -> None:
    with pytest.raises(ValueError, match="different run"):
        step(uuid4(), sid, dlq=(DlqRecord(run_id=uuid4(), as_of=AS_OF, agent="a", error_type="e"),))


def test_only_a_manual_halt_flattens() -> None:
    with pytest.raises(ValueError, match="manual"):
        KillSwitchEvent(
            run_id=uuid4(), triggered_at=AS_OF, trigger=KillTrigger.DAILY_LOSS, flattened=True
        )


def test_execution_upsert_moves_forward_and_keeps_first_reference(engine: Engine, sid: int) -> None:
    run_id = uuid4()
    sink = DecisionSink(engine)
    sink.flush_step(StepArtifacts(run=run_record(run_id)))
    open_row = ExecutionRecord(
        run_id=run_id, security_id=sid, client_order_id=f"{run_id}-{sid}-lim",
        broker_order_id="B1", kind=OrderKind.LIMIT, side=OrderSide.BUY, qty=100.0,
        filled_qty=0.0, limit_price=100.25, decision_price=99.0, reference_price=100.0,
        reference_source=ReferenceSource.IEX_MID, status=BrokerOrderStatus.OPEN,
        submitted_at=AS_OF,
    )  # fmt: skip
    sink.record_executions([open_row])
    done = open_row.model_copy(
        update={
            "filled_qty": 37.0, "fill_price": 99.5, "slippage_bps": -50.0, "filled_at": AS_OF,
            "status": BrokerOrderStatus.CANCELED, "reference_price": 555.0,
            "reference_source": ReferenceSource.SIP_LAST,
        }
    )  # fmt: skip
    sink.record_executions([done])
    sink.record_executions([done])  # replay is a no-op
    stored = sink.find_execution(open_row.client_order_id)
    assert stored is not None
    assert (stored.filled_qty, stored.fill_price, stored.slippage_bps) == (37.0, 99.5, -50.0)
    assert stored.status is BrokerOrderStatus.CANCELED
    assert (stored.reference_price, stored.reference_source) == (100.0, ReferenceSource.IEX_MID)
    assert sink.find_execution("missing") is None
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM orders")).scalar_one() == 1
