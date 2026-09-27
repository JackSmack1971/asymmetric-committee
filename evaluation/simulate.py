"""Fixed-seed Type-I, TOST-diagnostic and power simulation, sequential test (blueprint §12.6).

Validation only: the nulls, seeds, sample sizes and the acceptance rule are fixed here before any
result is seen, and a failing null is reported, never repaired by tuning ``λ``, the clip, the
critical value, the lag or the null definition. Run ``python -m evaluation.simulate``.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy import integrate, optimize, stats

from evaluation.sequential import (
    ALPHA,
    C_X,
    MAX_WEEK,
    TOST_MARGIN,
    SequentialStatus,
    SequentialTest,
    tost,
)

CONFIDENCE = 0.99  # one-sided Clopper-Pearson confidence for the Type-I acceptance rule
N_WEEKS = MAX_WEEK  # T = 104
N_REPS = 20_000  # replications per Type-I / TOST null
POWER_REPS = 2_000
WEEKLY_SD = 0.10 / math.sqrt(52)  # 10% annual tracking error
GARCH_ALPHA, GARCH_BETA = 0.10, 0.85
AR_PHIS = (0.1, 0.2, 0.3)
IU_EDGE = 0.005  # weekly mean excess on the comparison that is *not* a true null
IU_CORR = 0.5
POWER_EDGES = (0.0, 0.001, 0.002, 0.003, 0.005, 0.0075, 0.01)
SEED_NAMESPACE = "asymmetric-committee/p6.1/"

Rng = np.random.Generator
Series = NDArray[np.float64]
Generator = Callable[[Rng, int, int], Series]


def seed_for(name: str) -> int:
    """Deterministic per-null seed: first 8 bytes of ``sha256(namespace + name)``."""
    return int.from_bytes(hashlib.sha256((SEED_NAMESPACE + name).encode()).digest()[:8], "big")


def make_rng(name: str) -> Rng:
    return np.random.Generator(np.random.PCG64(seed_for(name)))


def clopper_pearson_upper(k: int, n: int, confidence: float = CONFIDENCE) -> float:
    """One-sided exact upper bound on a binomial rate."""
    if k >= n:
        return 1.0
    return float(stats.beta.ppf(confidence, k + 1, n - k))


# --- null generators: weekly net excess, mean zero (or clipped mean zero), shape (n, T) -------


def gen_student_t3(rng: Rng, n: int, t: int) -> Series:
    return WEEKLY_SD * rng.standard_t(3, size=(n, t)) / math.sqrt(3.0)


def gen_garch(rng: Rng, n: int, t: int) -> Series:
    """GARCH(1,1) martingale differences with unconditional sd ``WEEKLY_SD``."""
    omega = WEEKLY_SD**2 * (1.0 - GARCH_ALPHA - GARCH_BETA)
    burn = 200
    z = rng.standard_normal((n, t + burn))
    x = np.empty((n, t + burn))
    var = np.full(n, WEEKLY_SD**2)
    for i in range(t + burn):
        x[:, i] = np.sqrt(var) * z[:, i]
        var = omega + GARCH_ALPHA * x[:, i] ** 2 + GARCH_BETA * var
    return x[:, burn:]


def make_gen_ar1(phi: float) -> Generator:
    def gen(rng: Rng, n: int, t: int) -> Series:
        eps = WEEKLY_SD * math.sqrt(1.0 - phi * phi) * rng.standard_normal((n, t))
        x = np.empty((n, t))
        prev = WEEKLY_SD * rng.standard_normal(n)  # stationary start
        for i in range(t):
            prev = phi * prev + eps[:, i]
            x[:, i] = prev
        return x

    return gen


def skew_shift(sd: float = WEEKLY_SD, c_x: float = C_X, shape: float = 2.0) -> float:
    """Shift ``s`` so a right-skewed Gamma(shape) excess has *clipped* mean exactly 0.

    ``x = s + sd·(G - shape)/√shape``; solved by quadrature, deterministic.
    """

    def clipped_mean(s: float) -> float:
        def integrand(g: float) -> float:
            x = s + sd * (g - shape) / math.sqrt(shape)
            return max(-c_x, min(c_x, x)) * float(stats.gamma.pdf(g, shape))

        return float(integrate.quad(integrand, 0.0, 200.0, limit=400)[0])

    return float(optimize.brentq(clipped_mean, -sd, sd, xtol=1e-14))


def gen_skewed_clipped_zero(rng: Rng, n: int, t: int) -> Series:
    shape = 2.0
    g = rng.gamma(shape, size=(n, t))
    return skew_shift() + WEEKLY_SD * (g - shape) / math.sqrt(shape)


NULLS: dict[str, Generator] = {
    "iid_t3": gen_student_t3,
    "garch11": gen_garch,
    **{f"ar1_phi{phi}": make_gen_ar1(phi) for phi in AR_PHIS},
    "skewed_clipped_mean0": gen_skewed_clipped_zero,
}
IU_NAME = "intersection_union_one_true_null"


@dataclass(frozen=True)
class RateResult:
    name: str
    n: int
    rejections: int
    seed: int
    cp_upper: float

    @property
    def rate(self) -> float:
        return self.rejections / self.n

    @property
    def accepted(self) -> bool:
        return self.cp_upper <= ALPHA


def _result(name: str, rejections: int, n: int) -> RateResult:
    return RateResult(name, n, rejections, seed_for(name), clopper_pearson_upper(rejections, n))


def type1_null(name: str, n: int = N_REPS) -> RateResult:
    """P(PASS) when the committee has no edge on the one comparison, fed to both e-processes."""
    x = NULLS[name](make_rng(name), n, N_WEEKS)
    hits = 0
    for row in x:
        test = SequentialTest()
        for v in row.tolist():
            test = test.observe(v, v)
            if test.status is not SequentialStatus.RUNNING:
                break
        hits += test.status is SequentialStatus.PASS
    return _result(name, hits, n)


def type1_intersection_union(n: int = N_REPS) -> RateResult:
    """Two correlated comparisons: A is a true null, B has a real edge. PASS is a Type-I error."""
    rng = make_rng(IU_NAME)
    a = rng.standard_t(3, size=(n, N_WEEKS)) / math.sqrt(3.0)
    e = rng.standard_t(3, size=(n, N_WEEKS)) / math.sqrt(3.0)
    b = IU_CORR * a + math.sqrt(1.0 - IU_CORR**2) * e
    xa, xb = WEEKLY_SD * a, IU_EDGE + WEEKLY_SD * b
    hits = 0
    for ra, rb in zip(xa.tolist(), xb.tolist(), strict=True):
        test = SequentialTest()
        for va, vb in zip(ra, rb, strict=True):
            test = test.observe(va, vb)
            if test.status is not SequentialStatus.RUNNING:
                break
        hits += test.status is SequentialStatus.PASS
    return _result(IU_NAME, hits, n)


def fail_a_null(name: str, n: int = N_REPS) -> RateResult:
    """P(terminal FAIL) on the quant leg under the same null path and seed as ``type1_null``.

    The SPY leg is held at zero so PASS cannot pre-empt or conflict; the rate is the sup crossing of
    the mirrored e-process over observed weeks 26-104 (first terminal week, current wealth).
    """
    x = NULLS[name](make_rng(name), n, N_WEEKS)
    hits = 0
    for row in x:
        test = SequentialTest()
        for v in row.tolist():
            test = test.observe(0.0, v)
            if test.status is not SequentialStatus.RUNNING:
                break
        hits += test.status is SequentialStatus.FAIL
    return RateResult(f"fail_a_{name}", n, hits, seed_for(name), clopper_pearson_upper(hits, n))


def fail_a_intersection_union(n: int = N_REPS) -> RateResult:
    """P(FAIL) when the quant leg carries the real edge (mirror null strictly true)."""
    rng = make_rng(IU_NAME)
    a = rng.standard_t(3, size=(n, N_WEEKS)) / math.sqrt(3.0)
    e = rng.standard_t(3, size=(n, N_WEEKS)) / math.sqrt(3.0)
    b = IU_CORR * a + math.sqrt(1.0 - IU_CORR**2) * e
    xa, xb = WEEKLY_SD * a, IU_EDGE + WEEKLY_SD * b
    hits = 0
    for ra, rb in zip(xa.tolist(), xb.tolist(), strict=True):
        test = SequentialTest()
        for va, vb in zip(ra, rb, strict=True):
            test = test.observe(va, vb)
            if test.status is not SequentialStatus.RUNNING:
                break
        hits += test.status is SequentialStatus.FAIL
    return RateResult(
        f"fail_a_{IU_NAME}", n, hits, seed_for(IU_NAME), clopper_pearson_upper(hits, n)
    )


def tost_size(name: str, sign: int, n: int = N_REPS) -> RateResult:
    """DIAGNOSTIC (unaccepted). P(TOST equivalence), true mean on a margin (``sign`` = ±1)."""
    label = f"tost_{name}_{'+' if sign > 0 else '-'}margin"
    x = NULLS[name](make_rng(label), n, N_WEEKS) + sign * TOST_MARGIN
    hits = sum(tost(row.tolist()).equivalent for row in x)
    return _result(label, hits, n)


def tost_component_size(name: str, sign: int, n: int = N_REPS) -> RateResult:
    """DIAGNOSTIC (unaccepted). P(one one-sided test rejects) with the true mean on its boundary.

    ``sign = +1``: mean = +margin, the upper test (``t_upper < -cv``) should reject at most 5%.
    ``sign = -1``: mean = -margin, the lower test (``t_lower > cv``). This is the "one-sided
    boundary size" of the scratch study; the joint TOST size above is bounded by it.
    """
    label = f"tost_component_{name}_{'+' if sign > 0 else '-'}margin"
    x = NULLS[name](make_rng(label), n, N_WEEKS) + sign * TOST_MARGIN
    hits = 0
    for row in x:
        r = tost(row.tolist())
        hits += (r.t_upper < -r.critical_value) if sign > 0 else (r.t_lower > r.critical_value)
    return _result(label, hits, n)


@dataclass(frozen=True)
class PowerPoint:
    weekly_edge: float
    n: int
    passes: int
    median_pass_week: float | None

    @property
    def power(self) -> float:
        return self.passes / self.n


def power_curve(edges: tuple[float, ...] = POWER_EDGES, n: int = POWER_REPS) -> list[PowerPoint]:
    """P(PASS by week 104) for an iid-normal committee with a constant weekly edge on both legs."""
    points: list[PowerPoint] = []
    for edge in edges:
        name = f"power_edge_{edge}"
        x = edge + WEEKLY_SD * make_rng(name).standard_normal((n, N_WEEKS))
        weeks: list[int] = []
        for row in x:
            test = SequentialTest()
            for v in row.tolist():
                test = test.observe(v, v)
                if test.status is not SequentialStatus.RUNNING:
                    break
            if test.status is SequentialStatus.PASS and test.pass_week is not None:
                weeks.append(test.pass_week)
        median = statistics.median(weeks) if weeks else None
        points.append(PowerPoint(edge, n, len(weeks), median))
    return points


def main() -> None:
    print(f"T={N_WEEKS} reps={N_REPS} alpha={ALPHA} CP one-sided {CONFIDENCE:.0%}")
    accepted = [type1_null(name) for name in NULLS] + [type1_intersection_union()]
    accepted += [fail_a_null(name) for name in NULLS] + [fail_a_intersection_union()]
    diagnostic = [tost_size(name, sign) for name in NULLS for sign in (+1, -1)]
    diagnostic += [tost_component_size(name, sign) for name in NULLS for sign in (+1, -1)]
    for r in accepted:
        print(
            f"{r.name:44s} k={r.rejections:6d}/{r.n} rate={r.rate:.5f} "
            f"cp99_upper={r.cp_upper:.5f} seed={r.seed} {'OK' if r.accepted else 'VIOLATION'}"
        )
    print("-- TOST/HAC DIAGNOSTIC: not validated for decision use, never sets PASS/FAIL --")
    for r in diagnostic:
        print(
            f"{r.name:44s} k={r.rejections:6d}/{r.n} rate={r.rate:.5f} "
            f"cp99_upper={r.cp_upper:.5f} seed={r.seed} {'above-alpha' if not r.accepted else 'ok'}"
        )
    for p in power_curve():
        print(
            f"power edge={p.weekly_edge:.4f} power={p.power:.3f} median_week={p.median_pass_week}"
        )


if __name__ == "__main__":
    main()
