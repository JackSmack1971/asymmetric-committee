"""Fixed-seed simulation of the sequential test (blueprint §12.6)."""

from __future__ import annotations

import math
from itertools import pairwise

import pytest

from evaluation.sequential import C_X
from evaluation.simulate import (
    IU_NAME,
    N_REPS,
    NULLS,
    WEEKLY_SD,
    clopper_pearson_upper,
    fail_a_intersection_union,
    fail_a_null,
    power_curve,
    seed_for,
    skew_shift,
    tost_component_size,
    tost_size,
    type1_intersection_union,
    type1_null,
)


def test_seeds_are_fixed_by_name() -> None:
    assert seed_for("iid_t3") == 3545827819055368654
    assert seed_for("iid_t3") != seed_for("garch11")


def test_clopper_pearson_upper_bound() -> None:
    assert clopper_pearson_upper(0, 100) == pytest.approx(1 - 0.01 ** (1 / 100))
    assert clopper_pearson_upper(100, 100) == 1.0
    assert clopper_pearson_upper(5, 100) > 0.05  # exact bound is above the point estimate
    assert clopper_pearson_upper(2, 100, 0.5) < clopper_pearson_upper(2, 100, 0.99)


def test_skew_shift_zeroes_the_clipped_mean_not_the_raw_mean() -> None:
    s = skew_shift()
    assert 0.0 < s < 1e-4  # the clip barely bites at 3.6 sd, but the shift is not zero
    # a large-sample check with the same construction: clipped mean ~ 0 (within MC error)
    import numpy as np

    g = np.random.Generator(np.random.PCG64(1)).gamma(2.0, size=4_000_000)
    x = s + WEEKLY_SD * (g - 2.0) / math.sqrt(2.0)
    assert abs(float(np.clip(x, -C_X, C_X).mean())) < 3 * WEEKLY_SD / math.sqrt(len(x))


def test_small_runs_are_deterministic() -> None:
    a, b = type1_null("ar1_phi0.3", n=300), type1_null("ar1_phi0.3", n=300)
    assert (a.rejections, a.seed) == (b.rejections, b.seed)
    c, d = tost_component_size("iid_t3", +1, n=200), tost_component_size("iid_t3", +1, n=200)
    assert c.rejections == d.rejections
    assert type1_intersection_union(n=300).rejections == type1_intersection_union(n=300).rejections
    assert (
        fail_a_null("ar1_phi0.3", n=300).rejections == fail_a_null("ar1_phi0.3", n=300).rejections
    )
    assert fail_a_null("iid_t3", n=50).seed == seed_for("iid_t3")
    assert (
        fail_a_intersection_union(n=100).rejections == fail_a_intersection_union(n=100).rejections
    )


@pytest.mark.sim
@pytest.mark.parametrize("name", list(NULLS))
def test_type_i_error_is_below_alpha_for_every_null(name: str) -> None:
    r = type1_null(name)
    assert r.n == N_REPS
    assert r.accepted, f"{name}: {r.rejections}/{r.n} rate={r.rate:.5f} cp99={r.cp_upper:.5f}"


@pytest.mark.sim
def test_type_i_error_for_the_intersection_union_case() -> None:
    r = type1_intersection_union()
    assert r.name == IU_NAME
    assert r.accepted, f"{r.rejections}/{r.n} rate={r.rate:.5f} cp99={r.cp_upper:.5f}"


@pytest.mark.sim
@pytest.mark.parametrize("name", list(NULLS))
def test_fail_a_type_i_error_is_below_alpha_for_every_null(name: str) -> None:
    r = fail_a_null(name)
    assert r.n == N_REPS
    assert r.accepted, f"{r.name}: {r.rejections}/{r.n} rate={r.rate:.5f} cp99={r.cp_upper:.5f}"


@pytest.mark.sim
def test_fail_a_type_i_error_for_the_intersection_union_case() -> None:
    r = fail_a_intersection_union()
    assert r.accepted, f"{r.name}: {r.rejections}/{r.n} rate={r.rate:.5f} cp99={r.cp_upper:.5f}"


@pytest.mark.sim
@pytest.mark.parametrize("name", list(NULLS))
def test_joint_tost_size_on_the_margin_diagnostic(name: str) -> None:
    """Diagnostic only: the joint rate is tiny because the margin is far below the SE."""
    for sign in (+1, -1):
        r = tost_size(name, sign)
        assert r.accepted, f"{r.name}: {r.rejections}/{r.n} cp99={r.cp_upper:.5f}"


@pytest.mark.sim
def test_power_rises_with_the_edge() -> None:
    pts = power_curve()
    powers = [p.power for p in pts]
    assert powers[0] < 0.01  # no edge: essentially never passes
    assert powers[-1] > 0.99  # 1% a week: always passes
    assert all(b >= a - 0.03 for a, b in pairwise(powers))
