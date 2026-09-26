"""§7.1 pooling and §7.5 weight propagation (invariants 1, 6)."""

import math
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from committee.pooling import AgentHistory, NoVotingAgentsError, pool_agent_verdicts
from config.loader import CommitteeConfig, load_config
from contracts.enums import (
    VOTING_AGENTS,
    AgentName,
    DataSufficiency,
    FeedName,
    Horizon,
    Stance,
)
from contracts.models import AgentVerdict, EvidenceRef, PooledForecast

CFG: CommitteeConfig = load_config(allow_placeholders=True, env={}).pipeline.committee
AGENTS = sorted(VOTING_AGENTS, key=lambda a: a.value)
BASE = {Horizon.D5: 0.45, Horizon.D21: 0.44, Horizon.D63: 0.43}
RUN = uuid4()


def verdict(
    agent: AgentName,
    p5: float = 0.55,
    p21: float = 0.6,
    p63: float = 0.58,
    *,
    valid: bool = True,
    sufficiency: DataSufficiency = DataSufficiency.FULL,
) -> AgentVerdict:
    return AgentVerdict(
        stance=Stance.BUY,
        p_outperform_5=p5,
        p_outperform_21=p21,
        p_outperform_63=p63,
        key_evidence=(EvidenceRef(source=FeedName.FEATURES, row_id="f1", note="x"),),
        risks=(),
        data_sufficiency=sufficiency,
        run_id=RUN,
        agent=agent,
        entity_token="ENTITY_01",
        as_of=datetime(2026, 1, 5, tzinfo=UTC),
        prompt_version="v1-test",
        model_served="test/model",
        valid=valid,
    )


@pytest.fixture
def sample_verdicts() -> list[AgentVerdict]:
    # Distinct probabilities so every agent's logit differs.
    return [
        verdict(a, 0.50 + 0.03 * i, 0.55 + 0.03 * i, 0.52 + 0.03 * i) for i, a in enumerate(AGENTS)
    ]


def perturbed(verdicts: list[AgentVerdict], index: int, horizon: Horizon) -> list[AgentVerdict]:
    field = f"p_outperform_{horizon.value}"
    out = list(verdicts)
    out[index] = verdicts[index].model_copy(
        update={field: min(0.95, getattr(verdicts[index], field) + 0.15)}
    )
    return out


def skewed_history(good: AgentName) -> dict[tuple[AgentName, Horizon], AgentHistory]:
    """``good`` forecasts the outcomes; everyone else is worse than the base rate."""
    outcomes = tuple(i % 2 for i in range(40))
    hist: dict[tuple[AgentName, Horizon], AgentHistory] = {}
    for a in AGENTS:
        for h in Horizon:
            p = (
                tuple(0.9 if y else 0.1 for y in outcomes)
                if a is good
                else tuple(0.1 if y else 0.9 for y in outcomes)
            )
            hist[a, h] = AgentHistory(forecasts=p, outcomes=outcomes, independent_periods=120)
    return hist


def test_pooling_propagation_and_weight_positivity(sample_verdicts: list[AgentVerdict]) -> None:
    base = pool_agent_verdicts(sample_verdicts, base_rates=BASE, config=CFG)
    assert isinstance(base, PooledForecast)
    for horizon in Horizon:
        w = base.weights(horizon)
        assert set(w) == set(VOTING_AGENTS)
        assert all(x >= CFG.weight_floor for x in w.values())
        assert math.isclose(sum(w.values()), 1.0)
        assert AgentName.RED_TEAM not in w

    # Changing any single agent's p changes the pooled logit, at every horizon.
    for i in range(len(sample_verdicts)):
        for horizon in Horizon:
            alt = pool_agent_verdicts(
                perturbed(sample_verdicts, i, horizon), base_rates=BASE, config=CFG
            )
            assert alt.pool(horizon).logit > base.pool(horizon).logit, (i, horizon)


def test_propagation_holds_when_shrinkage_is_fully_on(
    sample_verdicts: list[AgentVerdict],
) -> None:
    hist = skewed_history(AGENTS[0])
    base = pool_agent_verdicts(sample_verdicts, base_rates=BASE, config=CFG, history=hist)
    assert base.pool(Horizon.D21).lambda_t > 0.5
    w = base.weights_d21
    assert all(x >= CFG.weight_floor - 1e-12 for x in w.values())
    assert math.isclose(sum(w.values()), 1.0)
    for i in range(len(sample_verdicts)):
        alt = pool_agent_verdicts(
            perturbed(sample_verdicts, i, Horizon.D21),
            base_rates=BASE,
            config=CFG,
            history=hist,
        )
        assert alt.logit_d21 > base.logit_d21


def test_skilled_agent_gets_most_weight_once_shrinkage_is_on(
    sample_verdicts: list[AgentVerdict],
) -> None:
    res = pool_agent_verdicts(
        sample_verdicts, base_rates=BASE, config=CFG, history=skewed_history(AGENTS[2])
    )
    w = res.weights_d21
    assert max(w, key=lambda a: w[a]) is AGENTS[2]
    # The unskilled ones sit exactly on the floor.
    assert all(math.isclose(w[a], CFG.weight_floor) for a in AGENTS if a is not AGENTS[2])


def test_equal_weights_before_t_w(sample_verdicts: list[AgentVerdict]) -> None:
    hist = skewed_history(AGENTS[0])
    short = {
        k: AgentHistory(v.forecasts, v.outcomes, CFG.shrinkage.min_periods) for k, v in hist.items()
    }
    res = pool_agent_verdicts(sample_verdicts, base_rates=BASE, config=CFG, history=short)
    assert res.pool(Horizon.D21).lambda_t == 0.0
    assert all(math.isclose(x, 1 / len(AGENTS)) for x in res.weights_d21.values())


def test_lambda_schedule_uses_the_weakest_agent(sample_verdicts: list[AgentVerdict]) -> None:
    hist = skewed_history(AGENTS[0])
    t = CFG.shrinkage.min_periods + 13
    hist[AGENTS[1], Horizon.D21] = AgentHistory(
        hist[AGENTS[1], Horizon.D21].forecasts, hist[AGENTS[1], Horizon.D21].outcomes, t
    )
    res = pool_agent_verdicts(sample_verdicts, base_rates=BASE, config=CFG, history=hist)
    assert math.isclose(res.pool(Horizon.D21).lambda_t, 1 - math.exp(-13 / CFG.shrinkage.tau))


def test_missing_history_means_equal_weights(sample_verdicts: list[AgentVerdict]) -> None:
    hist = skewed_history(AGENTS[0])
    del hist[AGENTS[3], Horizon.D21]
    res = pool_agent_verdicts(sample_verdicts, base_rates=BASE, config=CFG, history=hist)
    assert res.pool(Horizon.D21).lambda_t == 0.0


def test_invalid_and_insufficient_agents_do_not_vote(sample_verdicts: list[AgentVerdict]) -> None:
    vs = list(sample_verdicts)
    vs[0] = vs[0].model_copy(update={"valid": False})
    vs[1] = vs[1].model_copy(update={"data_sufficiency": DataSufficiency.INSUFFICIENT})
    res = pool_agent_verdicts(vs, base_rates=BASE, config=CFG)
    w = res.weights_d21
    assert set(w) == {v.agent for v in vs[2:]}
    assert math.isclose(sum(w.values()), 1.0)
    # Excluded agents cannot move the pool.
    moved = vs.copy()
    moved[0] = moved[0].model_copy(update={"p_outperform_21": 0.9})
    assert pool_agent_verdicts(moved, base_rates=BASE, config=CFG).logit_d21 == res.logit_d21


def test_no_voting_agents_raises(sample_verdicts: list[AgentVerdict]) -> None:
    vs = [v.model_copy(update={"valid": False}) for v in sample_verdicts]
    with pytest.raises(NoVotingAgentsError):
        pool_agent_verdicts(vs, base_rates=BASE, config=CFG)


def test_extreme_probabilities_are_clipped() -> None:
    lo, hi = CFG.logit_clip
    vs = [verdict(a, 0.0, 0.0, 0.0) for a in AGENTS]
    assert math.isclose(
        pool_agent_verdicts(vs, base_rates=BASE, config=CFG).logit_d21, math.log(lo / (1 - lo))
    )
    vs = [verdict(a, 1.0, 1.0, 1.0) for a in AGENTS]
    assert math.isclose(
        pool_agent_verdicts(vs, base_rates=BASE, config=CFG).logit_d21, math.log(hi / (1 - hi))
    )


def test_duplicate_agent_rejected(sample_verdicts: list[AgentVerdict]) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        pool_agent_verdicts(sample_verdicts + sample_verdicts[:1], base_rates=BASE, config=CFG)


def test_red_team_cannot_enter_pool() -> None:
    with pytest.raises(ValueError, match="does not emit AgentVerdict"):
        verdict(AgentName.RED_TEAM)


def test_dispersion_is_logit_std() -> None:
    vs = [verdict(a, 0.5, 0.5, 0.5) for a in AGENTS]
    pool = pool_agent_verdicts(vs, base_rates=BASE, config=CFG).pool(Horizon.D21)
    assert pool.dispersion == 0.0
    assert pool.logit == 0.0
