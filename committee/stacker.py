"""Walk-forward ridge-logistic stacker (§7.2) and error correlation / N_eff (§7.3).

Pure and deterministic: no DB, no LLM. Callers pass resolved observations only (strictly
resolved before ``as_of``); this module never sees an unresolved outcome. The fit is a damped
Newton solve on a strictly convex 2-parameter objective, so no numeric library is needed.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean

from config.loader import CommitteeConfig
from contracts.enums import Horizon
from contracts.models import CalibrationFit, ErrorCorrelation

log = logging.getLogger(__name__)

N_EFF_ALERT = 2.0  # §7.3
_MAX_ITER = 100
_TOL = 1e-10


@dataclass(frozen=True)
class Observation:
    """One resolved instance: pooled logit ``L``, outcome (1 = beat the sector ETF), weight.

    ``weight`` is the §7.1 overlap weight (1 at 5d, 1/4 at 21d, ~1/13 at 63d).
    """

    logit: float
    outcome: int
    weight: float


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _softplus(x: float) -> float:
    return max(x, 0.0) + math.log1p(math.exp(-abs(x)))


def _objective(
    a: float, b: float, obs: Sequence[Observation], anchor: float, g_a: float, g_b: float
) -> float:
    nll = sum(o.weight * (_softplus(a + b * o.logit) - o.outcome * (a + b * o.logit)) for o in obs)
    return nll / len(obs) + 0.5 * g_a * (a - anchor) ** 2 + 0.5 * g_b * (b - 1.0) ** 2


def _solve(
    obs: Sequence[Observation], anchor: float, g_a: float, g_b: float
) -> tuple[float, float]:
    m = len(obs)
    a, b = anchor, 1.0
    f = _objective(a, b, obs, anchor, g_a, g_b)
    for _ in range(_MAX_ITER):
        ga = g_a * (a - anchor)
        gb = g_b * (b - 1.0)
        haa, hab, hbb = g_a, 0.0, g_b
        for o in obs:
            p = sigmoid(a + b * o.logit)
            r = o.weight * (p - o.outcome) / m
            s = o.weight * p * (1.0 - p) / m
            ga += r
            gb += r * o.logit
            haa += s
            hab += s * o.logit
            hbb += s * o.logit**2
        if math.hypot(ga, gb) < _TOL:
            break
        det = haa * hbb - hab * hab
        if det > 1e-14:
            da, db = -(hbb * ga - hab * gb) / det, -(haa * gb - hab * ga) / det
        else:  # singular Hessian (a zero gamma with no spread): steepest descent
            da, db = -ga, -gb
        step = 1.0
        while step > 1e-12:
            f_new = _objective(a + step * da, b + step * db, obs, anchor, g_a, g_b)
            if f_new <= f + 1e-4 * step * (ga * da + gb * db):
                break
            step *= 0.5
        else:
            break
        a, b, f = a + step * da, b + step * db, f_new
    return a, b


def fit_stacker(
    observations: Sequence[Observation],
    *,
    horizon: Horizon,
    independent_periods: int,
    config: CommitteeConfig,
) -> CalibrationFit:
    """Fit ``p_cal = sigmoid(alpha + beta * L)`` for one horizon (§7.2).

    Pass-through (alpha 0, beta 1) while ``independent_periods < T_s``. Otherwise ridge-logistic
    with alpha pulled to logit(trailing base rate) and beta pulled to 1. ``independent_periods``
    comes from the evaluator; it is not ``len(observations)`` because outcomes overlap.
    """
    if not observations:
        raise ValueError("no resolved observations")
    cfg = config.stacker
    if independent_periods < cfg.activation_periods:
        return CalibrationFit(
            horizon=horizon,
            alpha=0.0,
            beta=1.0,
            active=False,
            independent_periods=independent_periods,
            observations=len(observations),
            base_rate=None,
        )
    total = sum(o.weight for o in observations)
    if total <= 0:
        raise ValueError("observation weights must sum to a positive value")
    lo, hi = config.logit_clip
    base = min(max(sum(o.weight * o.outcome for o in observations) / total, lo), hi)
    a, b = _solve(observations, math.log(base / (1.0 - base)), cfg.gamma_alpha, cfg.gamma_beta)
    return CalibrationFit(
        horizon=horizon,
        alpha=a,
        beta=b,
        active=True,
        independent_periods=independent_periods,
        observations=len(observations),
        base_rate=base,
    )


def compute_effective_agents(
    forecasts: Mapping[str, Sequence[float]],
    outcomes: Sequence[int],
    *,
    horizon: Horizon,
) -> ErrorCorrelation:
    """Pairwise error correlation, mean off-diagonal rho_bar and N_eff = A / (1 + (A-1) rho_bar).

    ``forecasts[agent][k]`` is ``p_a`` on resolved instance ``k``; error is ``p - outcome``.
    Logs ``AlgorithmicMonocultureWarning`` when N_eff < 2 (§7.3).
    """
    agents = sorted(forecasts)
    n_agents, m = len(agents), len(outcomes)
    if n_agents < 2:
        raise ValueError("need at least two agents")
    if m < 2 or any(len(forecasts[a]) != m for a in agents):
        raise ValueError("need >= 2 resolved instances, equal for every agent")
    errors = {a: [p - y for p, y in zip(forecasts[a], outcomes, strict=True)] for a in agents}
    centered: dict[str, list[float]] = {}
    norm: dict[str, float] = {}
    for a in agents:
        mean = fmean(errors[a])
        centered[a] = [e - mean for e in errors[a]]
        norm[a] = math.sqrt(sum(c * c for c in centered[a]))
        if norm[a] <= 1e-12:
            raise ValueError(f"agent {a!r} has constant errors; correlation undefined")
    total = 0.0
    for i, a in enumerate(agents):
        for b in agents[i + 1 :]:
            cov = sum(x * y for x, y in zip(centered[a], centered[b], strict=True))
            total += max(-1.0, min(1.0, cov / (norm[a] * norm[b])))
    rho = total / (n_agents * (n_agents - 1) / 2)
    denom = 1.0 + (n_agents - 1) * rho
    if denom <= 0:  # rho_bar <= -1/(A-1): errors anti-correlated past the identity's range
        raise ValueError("mean correlation below -1/(A-1); N_eff undefined")
    n_eff = n_agents / denom
    alert = n_eff < N_EFF_ALERT
    if alert:
        log.warning(
            "AlgorithmicMonocultureWarning: horizon=%s n_eff=%.3f rho_bar=%.3f agents=%d",
            horizon.value,
            n_eff,
            rho,
            n_agents,
        )
    return ErrorCorrelation(
        horizon=horizon,
        agents=n_agents,
        observations=m,
        mean_correlation=rho,
        n_eff=n_eff,
        monoculture_alert=alert,
    )
