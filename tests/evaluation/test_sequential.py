"""Sequential e-process, aGRAPA bet, terminal rule and TOST diagnostic (blueprint §12.6). Pure."""

from __future__ import annotations

import dataclasses
import math
import random
from itertools import pairwise

import numpy as np
import pytest
import statsmodels.api as sm  # type: ignore[import-untyped]

from evaluation.sequential import (
    C_X,
    E_THRESHOLD,
    KV_TABLE_I_BARTLETT_95,
    LAMBDA_CAP,
    MAX_WEEK,
    MIN_WEEK,
    TOST_MARGIN,
    BettingState,
    SequentialStatus,
    SequentialTest,
    SequentialTestClosed,
    TerminalReason,
    bartlett_lrv,
    bet,
    clipped_y,
    hac_lag,
    kv_bartlett_cv95,
    next_lambda,
    tost,
)


def run(ys: list[float]) -> list[BettingState]:
    states = [BettingState()]
    for y in ys:
        states.append(bet(states[-1], y))
    return states


def test_constants_match_the_preregistered_rule() -> None:
    assert E_THRESHOLD == 1 / 0.05
    assert (MIN_WEEK, MAX_WEEK, C_X, LAMBDA_CAP) == (26, 104, 0.05, 0.5)
    assert TOST_MARGIN == 0.02 / 52


def test_hand_computed_wealth_and_moments() -> None:
    s1, s2, s3 = run([1.0, 1.0, -1.0])[1:]
    assert s1.wealth == 1.0  # λ_1 = 0
    assert (s1.mu, s1.var) == (0.5, 1.25 / 2)
    assert next_lambda(s1) == 0.5  # 0.5 / (0.625 + 0.25) = 0.571, capped
    assert s2.wealth == 1.5
    assert s2.mu == pytest.approx(2 / 3)
    assert s2.var == pytest.approx((1 + 0.25 + (1 / 3) ** 2) / 3)
    assert s3.wealth == pytest.approx(0.75)  # 1.5 * (1 + 0.5 * -1)


def test_lambda_is_zero_at_start_and_always_within_bounds() -> None:
    assert next_lambda(BettingState()) == 0.0
    rng = random.Random(1)
    state = BettingState()
    for _ in range(500):
        state = bet(state, rng.uniform(-1.0, 1.0))
        assert 0.0 <= next_lambda(state) <= LAMBDA_CAP
        assert state.wealth > 0.0


def test_lambda_t_is_predictable() -> None:
    """The bet booked on y_{t} is fixed by y_1..y_{t-1}: changing y_t cannot change it."""
    base = [0.3, -0.2, 0.9, 0.1, -0.7, 0.4]
    for i in range(len(base)):
        before = run(base[:i])[-1]
        lam = next_lambda(before)
        for y in (-1.0, -0.2, 0.0, 0.6, 1.0):
            assert bet(before, y).wealth == pytest.approx(before.wealth * (1 + lam * y))
        changed = [*base[:i], -base[i]]
        assert next_lambda(run(changed[:i])[-1]) == lam


def test_paper_form_and_y_form_agree() -> None:
    """Waudby-Smith & Ramdas eq. 26 on X = (y+1)/2, m = 1/2, c = 1/2, one-sided."""
    rng = random.Random(7)
    ys = [rng.uniform(-1, 1) for _ in range(200)]
    mu, var, wealth_paper = 0.5, 0.25, 1.0
    sum_x, ss = 0.0, 0.0
    state = BettingState()
    for t, y in enumerate(ys, start=1):
        lam_paper = (mu - 0.5) / (var + (mu - 0.5) ** 2)
        lam_paper = max(0.0, min(1.0, lam_paper))  # clip to [0, c/m] with c = 1/2, m = 1/2
        x = (y + 1) / 2
        wealth_paper *= 1 + lam_paper * (x - 0.5)
        sum_x += x
        mu = (0.5 + sum_x) / (t + 1)
        ss += (x - mu) ** 2
        var = (0.25 + ss) / (t + 1)
        assert next_lambda(state) == pytest.approx(lam_paper / 2, abs=1e-12)
        state = bet(state, y)
        assert state.wealth == pytest.approx(wealth_paper, rel=1e-12)


def test_y_is_clipped_and_bad_input_fails_closed() -> None:
    assert clipped_y(0.10) == 1.0
    assert clipped_y(-0.10) == -1.0
    assert clipped_y(0.025) == 0.5
    with pytest.raises(ValueError):
        clipped_y(math.nan)
    with pytest.raises(ValueError):
        bet(BettingState(), 1.5)


def first_pass_week(spy: list[float], quant: list[float]) -> int | None:
    a = b = BettingState()
    for week, (xs, xq) in enumerate(zip(spy, quant, strict=True), start=1):
        a, b = bet(a, clipped_y(xs)), bet(b, clipped_y(xq))
        if week >= MIN_WEEK and a.wealth >= 20 and b.wealth >= 20:
            return week
    return None


def drive(spy: list[float], quant: list[float]) -> SequentialTest:
    test = SequentialTest()
    for xs, xq in zip(spy, quant, strict=True):
        test = test.observe(xs, xq)
        if test.status is not SequentialStatus.RUNNING:
            break
    return test


def test_pass_needs_week_26_and_is_absorbing() -> None:
    win = [0.05] * 60
    t = drive(win, win)
    assert t.status is SequentialStatus.PASS
    assert t.pass_week == MIN_WEEK  # e >= 20 long before week 26, but PASS waits for the floor
    assert t.spy.wealth >= 20 and t.quant.wealth >= 20
    assert t.observe(-0.05, -0.05) is t  # absorbing: later weeks change nothing


def test_pass_requires_both_comparisons_in_the_same_week() -> None:
    t = drive([0.05] * 80, [0.0] * 40 + [0.05] * 40)
    assert t.status is SequentialStatus.PASS
    assert t.pass_week is not None and t.pass_week > 40
    assert t.spy.wealth >= 20 and t.quant.wealth >= 20


def test_pass_week_matches_brute_force_on_random_series() -> None:
    rng = np.random.Generator(np.random.PCG64(5))
    for _ in range(60):
        edge = rng.uniform(0.0, 0.02)
        spy = (edge + 0.0139 * rng.standard_normal(104)).tolist()
        quant = (edge + 0.0139 * rng.standard_normal(104)).tolist()
        t = drive(spy, quant)
        expected = first_pass_week(spy, quant)
        assert t.pass_week == expected
        assert (t.status is SequentialStatus.PASS) == (expected is not None)


def mirror_wealth_path(quant: list[float]) -> list[float]:
    state = BettingState()
    path = []
    for x in quant:
        state = bet(state, -clipped_y(x))
        path.append(state.wealth)
    return path


def test_no_terminal_state_before_week_26_even_when_the_mirror_has_crossed() -> None:
    quant = [-0.05] * 60
    path = mirror_wealth_path(quant)
    assert path[MIN_WEEK - 2] >= E_THRESHOLD  # crossed well before the floor (week 25)
    test = SequentialTest()
    for week in range(1, MIN_WEEK):
        test = test.observe(0.0, quant[week - 1])
        assert test.status is SequentialStatus.RUNNING
        assert test.terminal_week is None and test.terminal_reason is None
    test = test.observe(0.0, quant[MIN_WEEK - 1])  # a process above threshold at 26 terminates
    assert test.status is SequentialStatus.FAIL
    assert (test.week, test.terminal_week) == (MIN_WEEK, MIN_WEEK)
    assert test.terminal_reason is TerminalReason.MIRRORED_E_PROCESS
    assert test.pass_week is None
    assert test.mirror.wealth >= E_THRESHOLD


def test_pre_26_crossing_that_falls_below_threshold_is_not_terminal() -> None:
    quant = [-0.05] * 10 + [0.05] * (MAX_WEEK - 10)
    path = mirror_wealth_path(quant)
    assert max(path[: MIN_WEEK - 1]) >= E_THRESHOLD and path[MIN_WEEK - 1] < E_THRESHOLD
    t = drive([0.0] * MAX_WEEK, quant)
    assert t.status is SequentialStatus.INCONCLUSIVE
    assert t.terminal_reason is TerminalReason.NO_CROSSING and t.week == MAX_WEEK


def test_first_terminal_crossing_is_absorbing_for_fail() -> None:
    t = drive([0.0] * 30, [-0.05] * 30)
    assert t.status is SequentialStatus.FAIL and t.terminal_week == MIN_WEEK
    for x in (0.05, -0.05, 0.0):
        assert t.observe(x, x) is t
    later = drive([0.0] * 104, [-0.05] * 30 + [0.05] * 74)
    assert later.status is SequentialStatus.FAIL and later.terminal_week == MIN_WEEK
    assert later.week == MIN_WEEK  # the driver stops at the first terminal week


def test_same_week_pass_and_fail_is_inconclusive_conflicting_evidence() -> None:
    high = BettingState(t=25, wealth=1000.0)  # all three e-processes far above 20 at the floor
    armed = SequentialTest(25, high, high, high, (0.0,) * 25)
    t = armed.observe(0.0, 0.0)
    assert t.status is SequentialStatus.INCONCLUSIVE
    assert t.terminal_reason is TerminalReason.CONFLICTING_EVIDENCE
    assert t.terminal_week == MIN_WEEK and t.pass_week is None
    assert t.observe(0.05, 0.05) is t  # the conflict is terminal and absorbing
    only_pass = SequentialTest(25, high, high, BettingState(t=25), (0.0,) * 25).observe(0.0, 0.0)
    assert only_pass.status is SequentialStatus.PASS
    only_fail = SequentialTest(25, BettingState(t=25), high, high, (0.0,) * 25).observe(0.0, 0.0)
    assert only_fail.status is SequentialStatus.FAIL


def reference_decision(spy: list[float], quant: list[float]) -> tuple[str, int | None, str]:
    a = b = m = BettingState()
    for week, (xs, xq) in enumerate(zip(spy, quant, strict=True), start=1):
        a, b = bet(a, clipped_y(xs)), bet(b, clipped_y(xq))
        m = bet(m, -clipped_y(xq))
        if week >= MIN_WEEK:
            p = a.wealth >= 20 and b.wealth >= 20
            f = m.wealth >= 20
            if p and f:
                return "INCONCLUSIVE", week, "conflicting_evidence"
            if p:
                return "PASS", week, "both_e_processes"
            if f:
                return "FAIL", week, "mirrored_e_process"
    return "INCONCLUSIVE", MAX_WEEK, "no_crossing_by_max_week"


def test_decision_matches_reference_rule_on_random_series() -> None:
    rng = np.random.Generator(np.random.PCG64(21))
    seen = set()
    for _ in range(150):
        edge_s, edge_q = rng.uniform(-0.02, 0.02, 2)
        spy = (edge_s + 0.0139 * rng.standard_normal(104)).tolist()
        quant = (edge_q + 0.0139 * rng.standard_normal(104)).tolist()
        t = drive(spy, quant)
        status, week, reason = reference_decision(spy, quant)
        assert (t.status.value, t.terminal_week, str(t.terminal_reason)) == (status, week, reason)
        seen.add(status)
    assert {"PASS", "FAIL", "INCONCLUSIVE"} <= seen


def test_week_104_without_a_crossing_is_inconclusive_and_closes_the_window() -> None:
    rng = np.random.Generator(np.random.PCG64(13))
    noisy = (0.0139 * rng.standard_normal(104)).tolist()
    t = drive([0.0] * 104, noisy)
    assert t.status is SequentialStatus.INCONCLUSIVE and t.pass_week is None
    assert (t.terminal_week, t.terminal_reason) == (MAX_WEEK, TerminalReason.NO_CROSSING)
    assert t.tost_result is not None  # diagnostic only
    with pytest.raises(SequentialTestClosed):
        t.observe(0.0, 0.0)


def test_tost_equivalence_never_creates_fail_or_any_status() -> None:
    rng = np.random.Generator(np.random.PCG64(11))
    tiny = (0.0002 * rng.standard_normal(104)).tolist()
    t = drive([0.0] * 104, tiny)
    assert t.tost_result is not None and t.tost_result.equivalent  # diagnostic says "equivalent"
    assert t.status is SequentialStatus.INCONCLUSIVE
    assert t.terminal_reason is TerminalReason.NO_CROSSING
    running = SequentialTest()
    for v in tiny[:103]:
        running = running.observe(0.0, v)
        assert running.status is SequentialStatus.RUNNING and running.tost_result is None


def test_terminal_week_and_reason_are_serializable() -> None:
    win = drive([0.05] * 60, [0.05] * 60)
    fail = drive([0.0] * 60, [-0.05] * 60)
    assert (win.status, win.terminal_week, win.terminal_reason) == (
        SequentialStatus.PASS,
        MIN_WEEK,
        TerminalReason.BOTH_E_PROCESSES,
    )
    assert win.pass_week == MIN_WEEK
    d = dataclasses.asdict(fail)
    assert (d["status"], d["terminal_week"], d["terminal_reason"]) == (
        "FAIL",
        26,
        "mirrored_e_process",
    )
    assert dataclasses.asdict(SequentialTest())["terminal_reason"] is None


def test_hac_lag_and_fixed_b_critical_value() -> None:
    assert hac_lag(104) == 4
    assert len(KV_TABLE_I_BARTLETT_95) == 50
    assert kv_bartlett_cv95(0.02) == 1.690
    assert kv_bartlett_cv95(0.04) == 1.731
    assert kv_bartlett_cv95(1.0) == 3.764
    assert kv_bartlett_cv95(0.03) == pytest.approx((1.690 + 1.731) / 2)
    assert kv_bartlett_cv95(4 / 104) == pytest.approx(1.690 + (4 / 104 / 0.02 - 1) * 0.041)
    assert kv_bartlett_cv95(4 / 104) == pytest.approx(1.7278, abs=5e-5)
    grid = KV_TABLE_I_BARTLETT_95
    assert all(a < b for a, b in pairwise(grid[:-1]))
    for bad in (0.0, 0.01, 1.2):
        with pytest.raises(ValueError):
            kv_bartlett_cv95(bad)


def test_tost_matches_statsmodels_hac() -> None:
    rng = np.random.Generator(np.random.PCG64(3))
    for _ in range(5):
        e = rng.standard_normal(104) * 0.014
        x = np.empty(104)
        x[0] = e[0]
        for i in range(1, 104):
            x[i] = 0.2 * x[i - 1] + e[i]
        r = tost(x.tolist())
        fit = sm.OLS(x, np.ones(104)).fit(
            cov_type="HAC", cov_kwds={"maxlags": r.bandwidth - 1, "use_correction": False}
        )
        assert r.bandwidth == 4 and r.b == pytest.approx(4 / 104)
        assert r.mean == pytest.approx(float(fit.params[0]), rel=1e-12)
        assert r.se == pytest.approx(float(fit.bse[0]), rel=1e-9)
        assert r.t_lower == pytest.approx((r.mean + TOST_MARGIN) / float(fit.bse[0]), rel=1e-9)
        assert r.critical_value == kv_bartlett_cv95(4 / 104)


def test_lrv_bandwidth_one_is_the_plain_variance_and_bounds_are_checked() -> None:
    x = [0.01, -0.02, 0.03, 0.0, -0.01, 0.02]
    m = sum(x) / 6
    assert bartlett_lrv(x, 1) == pytest.approx(sum((v - m) ** 2 for v in x) / 6)
    with pytest.raises(ValueError):
        bartlett_lrv(x, 0)
    with pytest.raises(ValueError):
        bartlett_lrv(x, 7)


def test_tost_declares_equivalence_only_inside_the_margin() -> None:
    rng = np.random.Generator(np.random.PCG64(9))
    noise = rng.standard_normal(104) * 0.0002
    assert tost(noise.tolist()).equivalent
    assert not tost((noise + 0.01).tolist()).equivalent  # mean far above the margin
    assert not tost((noise - 0.01).tolist()).equivalent  # mean far below it
    assert not tost([0.0] * 52 + [0.05] * 52).equivalent  # noisy series: se too large
    assert tost([0.0] * 104).equivalent  # degenerate constant series, exact mean 0
