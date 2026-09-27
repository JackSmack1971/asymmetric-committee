"""§8.1 dual-mode committee sizing: rank and calibrated modes, constraint order (invariant 6)."""

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from hypothesis import given
from hypothesis import strategies as st

from committee.stacker import sigmoid
from config.loader import RiskConfig, load_config
from contracts.enums import VOTING_AGENTS, BearSeverity, Horizon
from contracts.models import AgentWeight, CalibrationFit, HorizonPool, PooledForecast, ProposedBook
from risk import size_committee_book

CFG: RiskConfig = load_config(allow_placeholders=True, env={}).risk
AGENT = sorted(VOTING_AGENTS, key=lambda a: a.value)[0]
RUN, AS_OF = uuid4(), datetime(2025, 1, 3, tzinfo=UTC)
H = Horizon.D21


def forecast(sid: int, logit: float, dispersion: float = 0.0) -> PooledForecast:
    pools = tuple(
        HorizonPool(
            horizon=h,
            logit=logit,
            lambda_t=0.0,
            dispersion=dispersion,
            weights=(AgentWeight(agent=AGENT, weight=1.0),),
        )
        for h in Horizon
    )
    return PooledForecast(entity_token=f"TICKER_{sid + 10}", pools=pools)


def fit(
    active: bool, alpha: float = 0.0, beta: float = 1.0, base: float | None = None
) -> CalibrationFit:
    return CalibrationFit(
        horizon=H,
        alpha=alpha,
        beta=beta,
        active=active,
        independent_periods=20 if active else 3,
        observations=100,
        base_rate=base,
    )


def size(
    pooled: dict[int, PooledForecast],
    vols: dict[int, float],
    sectors: dict[int, str] | None = None,
    bear: Mapping[int, BearSeverity | None] | None = None,
    cal: CalibrationFit | None = None,
    cfg: RiskConfig = CFG,
) -> ProposedBook:
    return size_committee_book(
        run_id=RUN,
        as_of=AS_OF,
        pooled=pooled,
        bear=bear or {},
        sectors=sectors or {sid: f"S{sid}" for sid in pooled},
        volatilities=vols,
        fit=cal or fit(False),
        config=cfg,
    )


def test_rank_mode_takes_top_m_by_logit_and_is_inverse_vol() -> None:
    pooled = {i: forecast(i, logit=i / 10) for i in range(1, 13)}
    vols = {i: 0.2 for i in pooled}
    vols[12] = 0.4  # top name, half the weight of the others
    book = size(pooled, vols)
    ids = {p.security_id for p in book.positions}
    assert ids <= set(range(5, 13)) and len(ids) <= 8
    w = {p.security_id: p.target_weight for p in book.positions}
    assert w[12] < w[11]
    assert all(x <= CFG.max_position + 1e-12 for x in w.values())
    assert (
        book.gross_exposure + book.cash_weight == 1.0
        or abs(book.gross_exposure + book.cash_weight - 1.0) < 1e-12
    )


def test_rank_mode_applies_bear_multiplier() -> None:
    pooled = {i: forecast(i, logit=1.0 + i / 100) for i in range(1, 9)}
    vols = {i: 0.4 for i in pooled}
    cfg = CFG.model_copy(update={"max_position": 0.08, "vol_target_annual": 10.0})
    base = {p.security_id: p.target_weight for p in size(pooled, vols, cfg=cfg).positions}
    bear = {1: BearSeverity.HIGH, 2: BearSeverity.MED}
    w = {p.security_id: p.target_weight for p in size(pooled, vols, bear=bear, cfg=cfg).positions}
    # The multiplier acts on raw_w (12.5% each) before the cap: high -> 6.25%; med -> 9.4%, capped.
    assert base[1] == 0.08
    assert abs(w[1] - 0.125 * 0.5) < 1e-9
    assert w[2] == 0.08
    assert w[3] == 0.08


def test_calibrated_mode_edge_hurdle_and_vol_scaling() -> None:
    cal = fit(True, base=0.45)
    # p_cal = sigmoid(L); edge = p - 0.45
    logits = {1: 0.0, 2: 0.3, 3: 0.4, 4: 0.4}  # p = .5, .574, .599, .599 -> edge .05, .124, .149
    below = 0.45 + 0.03  # edge 0.03 < hurdle
    logits[5] = math.log(below / (1 - below))
    pooled = {sid: forecast(sid, lg) for sid, lg in logits.items()}
    vols = {1: 0.2, 2: 0.2, 3: 0.2, 4: 0.4, 5: 0.2}
    cfg = CFG.model_copy(update={"vol_target_annual": 10.0, "committee_k": 0.05})
    book = size(pooled, vols, cal=cal, cfg=cfg)
    w = {p.security_id: p.target_weight for p in book.positions}
    assert 5 not in w
    assert w[4] < w[3]  # same edge, double the vol
    assert abs(w[4] * 2 - w[3]) < 1e-9
    for sid in w:
        assert sigmoid(logits[sid]) - 0.45 >= 0.04


def test_pass_through_fit_matches_rank_probability() -> None:
    book = size({1: forecast(1, 0.7)}, {1: 0.2})
    assert abs(book.positions[0].pooled_p - sigmoid(0.7)) < 1e-12


def test_weights_do_not_depend_on_anything_but_inputs() -> None:
    pooled = {i: forecast(i, i / 5) for i in range(1, 7)}
    vols = {i: 0.25 for i in pooled}
    assert size(pooled, vols) == size(pooled, vols)


@given(
    st.lists(
        st.tuples(
            st.floats(-4, 4, allow_nan=False),
            st.floats(0.02, 2, allow_nan=False),
            st.floats(0, 2, allow_nan=False),
            st.sampled_from(list(BearSeverity)),
        ),
        min_size=1,
        max_size=30,
    ),
    st.booleans(),
    st.floats(0.2, 0.7, allow_nan=False),
)
def test_bounds_and_residual_cash(
    data: list[tuple[float, float, float, BearSeverity]], active: bool, base: float
) -> None:
    pooled = {i + 1: forecast(i + 1, lg, disp) for i, (lg, _, disp, _) in enumerate(data)}
    vols = {i + 1: v for i, (_, v, _, _) in enumerate(data)}
    bear = {i + 1: b for i, (_, _, _, b) in enumerate(data)}
    sectors = {i + 1: f"S{i % 3}" for i in range(len(data))}
    cal = fit(True, base=base) if active else fit(False)
    book = size(pooled, vols, sectors=sectors, bear=bear, cal=cal)
    assert all(CFG.min_position <= p.target_weight <= CFG.max_position for p in book.positions)
    if not active:
        assert len(book.positions) <= CFG.rank_top_m
    for s in set(sectors.values()):
        assert (
            sum(p.target_weight for p in book.positions if p.sector == s) <= CFG.max_sector + 1e-9
        )
    vol = sum((p.target_weight * vols[p.security_id]) ** 2 for p in book.positions) ** 0.5
    assert vol <= CFG.vol_target_annual + 1e-9
    assert book.gross_exposure <= 1.0 + 1e-9
    assert abs(book.gross_exposure + book.cash_weight - 1.0) < 1e-9
