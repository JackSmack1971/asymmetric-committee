"""Anytime-valid sequential test (blueprint §12.6). Pure: no I/O, clock or database.

Primary evidence is a pair of betting e-processes on the clipped weekly net excess
``y = clip(x, ±c_x) / c_x``. The bet ``λ_t`` is the aGRAPA rule of Waudby-Smith & Ramdas
(2024, JRSS-B; arXiv 2010.09686, App. B.3 eq. 26) in ``y`` units, so it is a function of
``y_1 … y_{t-1}`` only. E-processes accumulate from observed week 1, but no terminal decision
is taken before observed week ``MIN_WEEK`` (26). At each observed week ``t`` in [26, 104] the
*current* wealth decides: PASS iff both primary e-values >= 20; FAIL iff the mirrored quant
e-value >= 20; both on the first such week is INCONCLUSIVE (``conflicting_evidence``). The first
terminal result is absorbing; no crossing by week 104 is INCONCLUSIVE.

The fixed-b TOST at the bottom is a DIAGNOSTIC. Its one-sided components exceed their nominal
level under the preregistered dependent nulls, so it is not validated for decision use and never
sets PASS, FAIL or INCONCLUSIVE.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

ALPHA = 0.05
E_THRESHOLD = 20.0  # 1 / ALPHA
C_X = 0.05  # clip on weekly net excess (pre-registered)
LAMBDA_CAP = 0.5  # aGRAPA truncation c = 1/2 in y units
MIN_WEEK = 26
MAX_WEEK = 104
TOST_MARGIN = 0.02 / 52  # arithmetic weekly-mean margin, no geometric annualization

# Kiefer & Vogelsang (2005), Econometric Theory 21, Table I: asymptotic right-tail critical
# values of t_b, Bartlett kernel, 95% row, b = M/T = 0.02, 0.04, ..., 1.00 (50,000 replications).
KV_TABLE_I_BARTLETT_95: tuple[float, ...] = (
    1.690, 1.731, 1.772, 1.813, 1.861, 1.902, 1.944, 1.988, 2.030, 2.081,
    2.124, 2.179, 2.222, 2.274, 2.324, 2.367, 2.412, 2.459, 2.505, 2.556,
    2.601, 2.651, 2.696, 2.739, 2.781, 2.828, 2.872, 2.913, 2.956, 3.007,
    3.048, 3.082, 3.124, 3.162, 3.198, 3.245, 3.291, 3.330, 3.367, 3.408,
    3.444, 3.494, 3.537, 3.579, 3.616, 3.654, 3.692, 3.727, 3.764, 3.764,
)  # fmt: skip
_KV_STEP = 0.02


class SequentialTestClosed(RuntimeError):
    """An observation arrived after the window closed at week 104 without a terminal crossing."""


class SequentialStatus(StrEnum):
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class TerminalReason(StrEnum):
    BOTH_E_PROCESSES = "both_e_processes"  # PASS
    MIRRORED_E_PROCESS = "mirrored_e_process"  # FAIL
    CONFLICTING_EVIDENCE = "conflicting_evidence"  # INCONCLUSIVE: PASS and FAIL true together
    NO_CROSSING = "no_crossing_by_max_week"  # INCONCLUSIVE: window exhausted


def clipped_y(x: float, c_x: float = C_X) -> float:
    """Weekly net excess to the bounded betting variable ``y = clip(x, ±c_x) / c_x``."""
    if not math.isfinite(x):
        raise ValueError(f"non-finite excess return: {x!r}")
    return max(-c_x, min(c_x, x)) / c_x


@dataclass(frozen=True)
class BettingState:
    """One e-process. ``mu``/``var`` are the running mean and variance (priors 0 and 1 at t = 0)."""

    t: int = 0
    wealth: float = 1.0
    sum_y: float = 0.0
    sum_sq_dev: float = 0.0
    mu: float = 0.0
    var: float = 1.0


def next_lambda(state: BettingState) -> float:
    """``lambda = clip(mu / (var + mu^2), 0, 1/2)``, past data only; 0 at t = 0 (``mu_0 = 0``)."""
    lam = state.mu / (state.var + state.mu * state.mu)
    return max(0.0, min(LAMBDA_CAP, lam))


def bet(state: BettingState, y: float) -> BettingState:
    """Book ``y`` with the bet fixed by the past, then update the aGRAPA moments."""
    if not -1.0 <= y <= 1.0:
        raise ValueError(f"y must lie in [-1, 1], got {y!r}")
    wealth = state.wealth * (1.0 + next_lambda(state) * y)
    t = state.t + 1
    sum_y = state.sum_y + y
    mu = sum_y / (t + 1)
    sum_sq_dev = state.sum_sq_dev + (y - mu) ** 2  # deviation from the *current* μ̂_t
    var = (1.0 + sum_sq_dev) / (t + 1)
    return BettingState(t, wealth, sum_y, sum_sq_dev, mu, var)


def hac_lag(n: int) -> int:
    """Pre-registered Bartlett lag rule ``L = ⌊4 (T/100)^{2/9}⌋`` (4 at T = 104)."""
    bandwidth: float = 4.0 * math.pow(n / 100.0, 2.0 / 9.0)
    return math.floor(bandwidth)


def kv_bartlett_cv95(b: float) -> float:
    """95% right-tail fixed-b critical value, linear interpolation of KV (2005) Table I.

    Outside the tabulated range ``0.02 <= b <= 1`` there is no pinned value and this fails.
    """
    if not _KV_STEP - 1e-12 <= b <= 1.0 + 1e-12:
        raise ValueError(f"b = {b!r} outside the Kiefer-Vogelsang Table I range [0.02, 1]")
    pos = min(max(b / _KV_STEP - 1.0, 0.0), len(KV_TABLE_I_BARTLETT_95) - 1.0)
    i = min(int(pos), len(KV_TABLE_I_BARTLETT_95) - 2)
    frac = pos - i
    lo, hi = KV_TABLE_I_BARTLETT_95[i], KV_TABLE_I_BARTLETT_95[i + 1]
    return lo + frac * (hi - lo)


def bartlett_lrv(x: Sequence[float], bandwidth: int) -> float:
    """Long-run variance with Bartlett weights ``1 - j/M`` for ``j < M`` (KV's ``M``).

    ``gamma_j = (1/T) sum d_t d_{t-j}`` on demeaned data; no small-sample correction.
    """
    n = len(x)
    if bandwidth < 1 or bandwidth > n:
        raise ValueError("bandwidth must satisfy 1 <= M <= T")
    m = math.fsum(x) / n
    d = [v - m for v in x]
    lrv = math.fsum(v * v for v in d) / n
    for j in range(1, bandwidth):
        gamma = math.fsum(d[t] * d[t - j] for t in range(j, n)) / n
        lrv += 2.0 * (1.0 - j / bandwidth) * gamma
    return lrv


@dataclass(frozen=True)
class TostResult:
    """DIAGNOSTIC ONLY: not validated for decision use (boundary size exceeds alpha)."""

    n: int
    bandwidth: int
    b: float
    mean: float
    se: float
    t_lower: float  # (mean + δ) / se, tests H0: mean <= -δ
    t_upper: float  # (mean - δ) / se, tests H0: mean >= +δ
    critical_value: float
    equivalent: bool


def tost(x: Sequence[float], margin: float = TOST_MARGIN) -> TostResult:
    """DIAGNOSTIC ONLY. Two one-sided tests of |mean| < margin, fixed-b Bartlett HAC.

    Not validated for decision use: under the preregistered AR/skewed boundary nulls the one-sided
    components reject more than 5%, and at T = 104 the margin is far below the standard error at
    realistic tracking errors. Its result never sets PASS/FAIL/INCONCLUSIVE.
    ``bandwidth = L`` is Kiefer-Vogelsang's ``M`` and ``b = L / T``.
    """
    n = len(x)
    bandwidth = hac_lag(n)
    b = bandwidth / n
    cv = kv_bartlett_cv95(b)
    mean = math.fsum(x) / n
    se = math.sqrt(bartlett_lrv(x, bandwidth) / n)
    if se <= 0.0:
        # degenerate (constant) series: the mean is known exactly
        t_lo = math.inf if mean + margin > 0 else -math.inf
        t_hi = -math.inf if mean - margin < 0 else math.inf
    else:
        t_lo = (mean + margin) / se
        t_hi = (mean - margin) / se
    return TostResult(n, bandwidth, b, mean, se, t_lo, t_hi, cv, t_lo > cv and t_hi < -cv)


@dataclass(frozen=True)
class SequentialTest:
    """Immutable state of the two-comparison test (vs exposure-matched SPY, vs quant baseline).

    ``observe`` takes one observed week's net excess vs each comparator. ``mirror`` bets on
    ``-y`` of the quant comparison (committee worse than the quant baseline) for FAIL.
    ``terminal_week``/``terminal_reason`` record the decision; ``tost_result`` is a diagnostic
    computed only when the window is exhausted and never influences ``status``.
    """

    week: int = 0
    spy: BettingState = BettingState()
    quant: BettingState = BettingState()
    mirror: BettingState = BettingState()
    quant_excess: tuple[float, ...] = ()
    status: SequentialStatus = SequentialStatus.RUNNING
    terminal_week: int | None = None
    terminal_reason: TerminalReason | None = None
    tost_result: TostResult | None = None

    @property
    def pass_week(self) -> int | None:
        return self.terminal_week if self.status is SequentialStatus.PASS else None

    def observe(self, excess_vs_spy: float, excess_vs_quant: float) -> SequentialTest:
        if self.status is not SequentialStatus.RUNNING:
            if self.terminal_reason is TerminalReason.NO_CROSSING:
                raise SequentialTestClosed(f"window closed at week {self.week} without a decision")
            return self  # absorbing: the first terminal result is frozen
        y_spy = clipped_y(excess_vs_spy)
        y_quant = clipped_y(excess_vs_quant)
        week = self.week + 1
        spy = bet(self.spy, y_spy)
        quant = bet(self.quant, y_quant)
        mirror = bet(self.mirror, -y_quant)
        raw = (*self.quant_excess, excess_vs_quant)

        def state(
            status: SequentialStatus = SequentialStatus.RUNNING,
            reason: TerminalReason | None = None,
            diagnostic: TostResult | None = None,
        ) -> SequentialTest:
            terminal = None if status is SequentialStatus.RUNNING else week
            return SequentialTest(
                week, spy, quant, mirror, raw, status, terminal, reason, diagnostic
            )

        if week >= MIN_WEEK:
            passed = spy.wealth >= E_THRESHOLD and quant.wealth >= E_THRESHOLD
            failed = mirror.wealth >= E_THRESHOLD
            if passed and failed:
                return state(SequentialStatus.INCONCLUSIVE, TerminalReason.CONFLICTING_EVIDENCE)
            if passed:
                return state(SequentialStatus.PASS, TerminalReason.BOTH_E_PROCESSES)
            if failed:
                return state(SequentialStatus.FAIL, TerminalReason.MIRRORED_E_PROCESS)
        if week < MAX_WEEK:
            return state()
        return state(SequentialStatus.INCONCLUSIVE, TerminalReason.NO_CROSSING, tost(raw))
