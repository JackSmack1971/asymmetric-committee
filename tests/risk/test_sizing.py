from datetime import UTC, datetime
from uuid import uuid4

from hypothesis import given
from hypothesis import strategies as st

from config.loader import RiskConfig
from contracts.enums import AgentName, BearSeverity, Horizon
from contracts.models import AgentWeight, CommitteeDecision
from risk import size_book

CFG = RiskConfig(
    long_only=True,
    max_position=0.08,
    max_sector=0.30,
    vol_target_annual=0.12,
    min_position=0.01,
    entry_threshold=0.56,
    k=0.02,
    dispersion_lambda=0.5,
    bear_multiplier={
        BearSeverity.LOW: 1.0,
        BearSeverity.MED: 1.0,
        BearSeverity.HIGH: 0.5,
    },
    cio_veto_budget=0.2,
    kill_switch_daily_loss=0.03,
    modeled_cost_bps=5,
)


@given(
    st.lists(
        st.tuples(st.floats(0, 1, allow_nan=False), st.floats(0.02, 2, allow_nan=False)),
        min_size=1,
        max_size=30,
    )
)
def test_constraints_hold(data: list[tuple[float, float]]) -> None:
    run_id, as_of = uuid4(), datetime(2025, 1, 3, tzinfo=UTC)
    decisions = tuple(
        CommitteeDecision(
            run_id=run_id,
            security_id=i + 1,
            entity_token=f"TICKER_{i + 10}",
            as_of=as_of,
            horizon_days=Horizon.D21,
            pooled_p=p,
            dispersion=0,
            agent_weights=(AgentWeight(agent=AgentName.QUANT_BASELINE, weight=1),),
            bear_severity=None,
            target_weight=0,
        )
        for i, (p, _) in enumerate(data)
    )
    vols = {i + 1: vol for i, (_, vol) in enumerate(data)}
    sectors = {i + 1: f"S{i % 3}" for i in range(len(data))}
    book = size_book(decisions=decisions, sectors=sectors, volatilities=vols, config=CFG)
    assert all(CFG.min_position <= p.target_weight <= CFG.max_position for p in book.positions)
    assert all(
        sum(p.target_weight for p in book.positions if p.sector == sector) <= CFG.max_sector + 1e-12
        for sector in set(sectors.values())
    )
    assert (
        sum((p.target_weight * vols[p.security_id]) ** 2 for p in book.positions) ** 0.5
        <= CFG.vol_target_annual + 1e-12
    )
