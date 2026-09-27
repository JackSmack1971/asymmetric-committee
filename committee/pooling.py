"""Per-horizon logit pooling of the voting agents (§7.1).

Pure and deterministic: no DB, no LLM, no weights outside the pooling weights. The stacker
(``logit p_pool = alpha + beta * L``) is applied downstream; this module returns ``L``.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import pstdev

from config.loader import CommitteeConfig
from contracts.enums import AgentName, DataSufficiency, Horizon
from contracts.models import AgentVerdict, AgentWeight, HorizonPool, PooledForecast


class NoVotingAgentsError(ValueError):
    """Every verdict was excluded (insufficient data or ``valid=False``); the entity has no vote."""


@dataclass(frozen=True)
class AgentHistory:
    """Resolved forecasts of one agent at one horizon, with the count of independent periods.

    ``independent_periods`` is supplied by the evaluator; overlapping observations are not
    independent (§7.1), so it is not ``len(forecasts)``.
    """

    forecasts: tuple[float, ...]
    outcomes: tuple[int, ...]  # 1 = beat the sector ETF
    independent_periods: int

    def __post_init__(self) -> None:
        if len(self.forecasts) != len(self.outcomes):
            raise ValueError("forecasts and outcomes differ in length")


History = Mapping[tuple[AgentName, Horizon], AgentHistory]


def _logit(p: float, lo: float, hi: float) -> float:
    q = min(max(p, lo), hi)
    return math.log(q / (1.0 - q))


def _probability(v: AgentVerdict, horizon: Horizon) -> float:
    return float(getattr(v, f"p_outperform_{horizon.value}"))


def _brier(forecasts: Sequence[float], outcomes: Sequence[int], omega: float) -> float:
    return sum(omega * (p - y) ** 2 for p, y in zip(forecasts, outcomes, strict=True)) / len(
        outcomes
    )


def _lambda_t(
    agents: Sequence[AgentName], horizon: Horizon, history: History | None, cfg: CommitteeConfig
) -> float:
    """0 until every voting agent has T_w independent periods, then 1 - exp(-(T - T_w) / tau)."""
    if history is None:
        return 0.0
    periods = []
    for a in agents:
        h = history.get((a, horizon))
        if h is None:
            return 0.0
        periods.append(h.independent_periods)
    t, t_w = min(periods), cfg.shrinkage.min_periods
    if t <= t_w:
        return 0.0
    return 1.0 - math.exp(-(t - t_w) / cfg.shrinkage.tau)


def _skill_weights(
    agents: Sequence[AgentName],
    horizon: Horizon,
    history: History,
    base_rate: float,
    omega: float,
) -> dict[AgentName, float]:
    """w_hat_a proportional to max(0, BSS_a) against the trailing base rate; equal if no skill."""
    skill: dict[AgentName, float] = {}
    for a in agents:
        h = history[a, horizon]
        base = _brier([base_rate] * len(h.outcomes), h.outcomes, omega)
        bss = 1.0 - _brier(h.forecasts, h.outcomes, omega) / base if base > 0 else 0.0
        skill[a] = max(0.0, bss)
    total = sum(skill.values())
    if total <= 0:
        return {a: 1.0 / len(agents) for a in agents}
    return {a: s / total for a, s in skill.items()}


def _apply_floor(raw: Mapping[AgentName, float], floor: float) -> dict[AgentName, float]:
    """Raise weights to ``floor`` and rescale the rest so the sum stays 1 and none drops below it.

    A single "floor then renormalize" pass can push a floored weight back under the floor, so
    floored agents are pinned and only the others are rescaled, until stable.
    """
    pinned = {a for a, w in raw.items() if w < floor}
    while True:
        free = [a for a in raw if a not in pinned]
        remaining = 1.0 - floor * len(pinned)
        free_sum = sum(raw[a] for a in free)
        if not free or free_sum <= 0:
            return {a: 1.0 / len(raw) for a in raw}
        scaled = {a: raw[a] * remaining / free_sum for a in free}
        newly = {a for a, w in scaled.items() if w < floor}
        if not newly:
            return {a: (floor if a in pinned else scaled[a]) for a in raw}
        pinned |= newly


def pool_agent_verdicts(
    verdicts: Sequence[AgentVerdict],
    *,
    base_rates: Mapping[Horizon, float],
    config: CommitteeConfig,
    history: History | None = None,
) -> PooledForecast:
    """Pool one entity's voting-agent verdicts at every horizon.

    ``verdicts`` holds at most one verdict per agent, all for the same entity. Agents that are
    ``insufficient`` or ``valid=False`` do not vote and weights are renormalized over the rest.
    ``base_rates[h]`` is the trailing empirical base rate of beating the sector ETF at ``h``.
    """
    dupes = [a for a, n in Counter(v.agent for v in verdicts).items() if n > 1]
    if dupes:
        raise ValueError(f"duplicate verdicts for {sorted(a.value for a in dupes)}")
    if len({v.entity_token for v in verdicts}) > 1:
        raise ValueError("verdicts span more than one entity")
    voters = sorted(
        (v for v in verdicts if v.valid and v.data_sufficiency is not DataSufficiency.INSUFFICIENT),
        key=lambda v: v.agent.value,
    )
    if not voters:
        raise NoVotingAgentsError("no valid, sufficient voting agent")
    agents = [v.agent for v in voters]
    lo, hi = config.logit_clip

    pools = []
    for horizon in Horizon:
        z = {v.agent: _logit(_probability(v, horizon), lo, hi) for v in voters}
        lam = _lambda_t(agents, horizon, history, config)
        equal = 1.0 / len(agents)
        if lam > 0 and history is not None:
            omega = config.overlap_weights.for_horizon(horizon)
            skill = _skill_weights(agents, horizon, history, base_rates[horizon], omega)
        else:
            skill = {a: equal for a in agents}
        raw = {a: (1.0 - lam) * equal + lam * skill[a] for a in agents}
        w = _apply_floor(raw, config.weight_floor)
        pools.append(
            HorizonPool(
                horizon=horizon,
                logit=sum(w[a] * z[a] for a in agents),
                lambda_t=lam,
                dispersion=pstdev(z.values()) if len(z) > 1 else 0.0,
                weights=tuple(AgentWeight(agent=a, weight=w[a]) for a in agents),
            )
        )
    return PooledForecast(entity_token=voters[0].entity_token, pools=tuple(pools))
