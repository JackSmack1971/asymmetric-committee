"""§7.2 walk-forward stacker and §7.3 error correlation / N_eff."""

import logging
import math
import random

import pytest

from committee.stacker import (
    Observation,
    compute_effective_agents,
    fit_stacker,
    sigmoid,
)
from config.loader import CommitteeConfig, load_config
from contracts.enums import Horizon

CFG: CommitteeConfig = load_config(allow_placeholders=True, env={}).pipeline.committee
H = Horizon.D21


def synthetic(n: int, slope: float, seed: int = 1) -> list[Observation]:
    """Outcomes drawn from sigmoid(slope * L) with L ~ N(0, 1)."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        logit = rng.gauss(0.0, 1.0)
        y = 1 if rng.random() < sigmoid(slope * logit) else 0
        out.append(Observation(logit=logit, outcome=y, weight=1.0))
    return out


def test_pass_through_below_activation() -> None:
    obs = synthetic(200, slope=3.0)
    fit = fit_stacker(obs, horizon=H, independent_periods=7, config=CFG)
    assert (fit.alpha, fit.beta, fit.active) == (0.0, 1.0, False)
    assert fit.independent_periods == 7
    for logit in (-2.0, 0.0, 1.3):
        assert sigmoid(fit.alpha + fit.beta * logit) == pytest.approx(1 / (1 + math.exp(-logit)))


def test_activates_at_threshold_and_can_extremize() -> None:
    obs = synthetic(3000, slope=2.0)
    fit = fit_stacker(obs, horizon=H, independent_periods=8, config=CFG)
    assert fit.active
    assert fit.beta > 1.0


def test_beta_stays_at_one_under_high_gamma_beta() -> None:
    cfg = CFG.model_copy(update={"stacker": CFG.stacker.model_copy(update={"gamma_beta": 1e6})})
    fit = fit_stacker(synthetic(500, slope=3.0), horizon=H, independent_periods=20, config=cfg)
    assert fit.beta == pytest.approx(1.0, abs=1e-3)


def test_alpha_anchors_to_base_rate_with_no_signal() -> None:
    rng = random.Random(7)
    obs = [Observation(rng.gauss(0, 1), 1 if rng.random() < 0.3 else 0, 1.0) for _ in range(2000)]
    fit = fit_stacker(obs, horizon=H, independent_periods=20, config=CFG)
    assert fit.base_rate == pytest.approx(0.3, abs=0.03)
    assert fit.alpha == pytest.approx(math.log(0.3 / 0.7), abs=0.15)


def test_gradient_is_zero_at_solution() -> None:
    obs = synthetic(400, slope=1.5, seed=3)
    fit = fit_stacker(obs, horizon=H, independent_periods=20, config=CFG)
    assert fit.base_rate is not None
    a, b, m = fit.alpha, fit.beta, len(obs)
    ga = sum(o.weight * (sigmoid(a + b * o.logit) - o.outcome) for o in obs) / m
    gb = sum(o.weight * (sigmoid(a + b * o.logit) - o.outcome) * o.logit for o in obs) / m
    ga += CFG.stacker.gamma_alpha * (a - math.log(fit.base_rate / (1 - fit.base_rate)))
    gb += CFG.stacker.gamma_beta * (b - 1.0)
    assert abs(ga) < 1e-6
    assert abs(gb) < 1e-6


def test_empty_or_single_class_history_is_stable() -> None:
    obs = [Observation(logit=0.5, outcome=1, weight=1.0)] * 50
    fit = fit_stacker(obs, horizon=H, independent_periods=20, config=CFG)
    assert math.isfinite(fit.alpha) and math.isfinite(fit.beta)
    with pytest.raises(ValueError):
        fit_stacker([], horizon=H, independent_periods=20, config=CFG)


def test_n_eff_collapses_to_one_when_errors_perfectly_correlated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcomes = [1, 0, 1, 1, 0, 0, 1, 0, 1, 0]
    base = [0.7, 0.4, 0.6, 0.8, 0.3, 0.2, 0.9, 0.5, 0.55, 0.35]
    forecasts = {
        "a": base,
        "b": [p - 0.1 for p in base],  # constant shift of a, so errors are perfectly correlated
        "c": [p + 0.05 for p in base],
    }
    with caplog.at_level(logging.WARNING):
        res = compute_effective_agents(forecasts, outcomes, horizon=H)
    assert res.mean_correlation == pytest.approx(1.0)
    assert res.n_eff == pytest.approx(1.0)
    assert res.monoculture_alert
    assert any("AlgorithmicMonocultureWarning" in r.message for r in caplog.records)


def test_n_eff_equals_agent_count_for_uncorrelated_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rng = random.Random(11)
    n = 4000
    outcomes = [rng.randint(0, 1) for _ in range(n)]
    # error = p - y, so p = y + independent noise gives independent errors
    forecasts = {k: [y + rng.gauss(0, 0.2) for y in outcomes] for k in "abcd"}
    with caplog.at_level(logging.WARNING):
        res = compute_effective_agents(forecasts, outcomes, horizon=H)
    assert res.n_eff == pytest.approx(4.0, rel=0.05)
    assert not res.monoculture_alert
    assert not caplog.records


def test_effective_agents_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        compute_effective_agents({"a": [0.5, 0.6]}, [1, 0], horizon=H)
    with pytest.raises(ValueError):
        compute_effective_agents({"a": [0.5], "b": [0.6, 0.7]}, [1, 0], horizon=H)
    with pytest.raises(ValueError):  # zero-variance agent has no defined correlation
        compute_effective_agents({"a": [1, 0, 1], "b": [0.1, 0.9, 0.4]}, [1, 0, 1], horizon=H)
