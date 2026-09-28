"""Pure book cost evidence calculations for §5, §9 and §12.3."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import pairwise
from uuid import UUID
from zoneinfo import ZoneInfo

from config.loader import RiskConfig
from contracts.benchmarks import (
    BenchmarkResult,
    BenchmarkTrialIdentity,
    RandomCommitteeBundle,
    RandomCommitteeSummary,
    assert_benchmark_completeness,
    assert_benchmark_variant_completeness,
)
from contracts.data import PriceBar
from contracts.enums import Benchmark, BenchmarkVariant, OutcomeCompleteness, PriceFeed, RefStatus
from contracts.market_data import BenchmarkPeriodReference, ExecutionReference
from contracts.models import CalibrationFit
from evaluation.tbill_rules import TBillAccrualStep, TBillRate
from risk.sizing import size_committee_book


class BenchmarkEvidenceError(ValueError):
    """Required point-in-time benchmark or cost evidence is absent or invalid."""


_ET = ZoneInfo("America/New_York")
_P6_6_EVALUATION_PARAMETERS = {
    "version": "p6.6-benchmark-evaluation-v1",
    "benchmark_variants": ("adjusted", "unadjusted"),
    "random_committee_draws": 1000,
    "spread_sessions": 21,
    "spread_cost_cutoff": "run_as_of",
    "spread_source_provenance": "22_ordered_SIP_bars_included",
    "spread_estimators": ("abdi_ranaldo", "corwin_schultz"),
    "spread_floor": "daily_nonnegative_before_aggregation",
    "spread_aggregation": "equal_weight_arithmetic_mean",
    "per_side_cost_bps": "0.5*spread_est+5",
    "turnover": "sum_abs_target_minus_drifted_previous",
}


def evaluation_parameters_sha256() -> str:
    """Digest the P6.6 calculation constants used in durable trial identity."""
    canonical = json.dumps(
        _P6_6_EVALUATION_PARAMETERS, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class SpreadEstimate:
    """Two separately averaged, daily-floored spread estimates and their combined cost."""

    spread_ar_21d_bps: float
    spread_cs_21d_bps: float
    spread_est_bps: float
    per_side_cost_bps: float
    ar_daily_bps: tuple[float, ...]
    cs_daily_bps: tuple[float, ...]
    input_sha256: str
    input_bars: tuple[PriceBar, ...] = ()


def estimate_spreads_from_daily(
    *, ar_daily: Sequence[float], cs_daily: Sequence[float]
) -> SpreadEstimate:
    """Floor daily values first, average each estimator, then equally average components."""
    if not ar_daily or len(ar_daily) != len(cs_daily):
        raise BenchmarkEvidenceError("both estimator daily series must be present and aligned")
    if any(not math.isfinite(v) for v in (*ar_daily, *cs_daily)):
        raise BenchmarkEvidenceError("spread estimates must be finite")
    ar = tuple(max(0.0, float(v)) for v in ar_daily)
    cs = tuple(max(0.0, float(v)) for v in cs_daily)
    ar_mean = sum(ar) / len(ar)
    cs_mean = sum(cs) / len(cs)
    spread = (ar_mean + cs_mean) / 2
    return SpreadEstimate(
        spread_ar_21d_bps=ar_mean,
        spread_cs_21d_bps=cs_mean,
        spread_est_bps=spread,
        per_side_cost_bps=0.5 * spread + 5.0,
        ar_daily_bps=ar,
        cs_daily_bps=cs,
        input_sha256="",
    )


def _corwin_schultz(bar0: PriceBar, bar1: PriceBar) -> float:
    """Return the CS two-session spread fraction with its overnight-gap adjustment."""
    h0, l0, h1, l1 = bar0.high, bar0.low, bar1.high, bar1.low
    if bar1.low > bar0.close:
        shift = bar1.low - bar0.close
        h1 -= shift
        l1 -= shift
    elif bar1.high < bar0.close:
        shift = bar0.close - bar1.high
        h1 += shift
        l1 += shift
    if min(h0, l0, h1, l1) <= 0:
        raise BenchmarkEvidenceError("invalid Corwin-Schultz adjusted high/low")
    beta = math.log(h0 / l0) ** 2 + math.log(h1 / l1) ** 2
    gamma = math.log(max(h0, h1) / min(l0, l1)) ** 2
    denominator = 3.0 - 2.0 * math.sqrt(2.0)
    alpha = (math.sqrt(2.0 * beta) - math.sqrt(beta)) / denominator - math.sqrt(gamma / denominator)
    return 2.0 * (math.exp(alpha) - 1.0) / (1.0 + math.exp(alpha))


def estimate_effective_spread(
    *,
    bars: Sequence[PriceBar],
    sessions: Sequence[date],
    security_id: int,
    cutoff: datetime,
) -> SpreadEstimate:
    """Estimate each component from exactly 21 adjacent SIP-bar pairs (22 sessions)."""
    if len(sessions) != 22 or tuple(sorted(set(sessions))) != tuple(sessions):
        raise BenchmarkEvidenceError("a 21-session spread window requires 22 ordered sessions")
    by_date: dict[date, PriceBar] = {}
    for bar in bars:
        if bar.security_id != security_id or bar.feed is not PriceFeed.SIP:
            continue
        day = bar.event_time.astimezone(_ET).date()
        if day not in sessions:
            continue
        if bar.available_at > cutoff:
            continue
        if day in by_date:
            raise BenchmarkEvidenceError("ambiguous SIP bar for required session")
        by_date[day] = bar
    if any(day not in by_date for day in sessions):
        raise BenchmarkEvidenceError("missing eligible SIP bar for required spread session")
    daily_ar: list[float] = []
    daily_cs: list[float] = []
    for first, second in pairwise(sessions):
        left, right = by_date[first], by_date[second]
        eta_left = (math.log(left.high) + math.log(left.low)) / 2.0
        eta_right = (math.log(right.high) + math.log(right.low)) / 2.0
        close = math.log(left.close)
        ar_fraction = math.sqrt(max(4.0 * (close - eta_left) * (close - eta_right), 0.0))
        cs_fraction = _corwin_schultz(left, right)
        daily_ar.append(ar_fraction * 10_000.0)
        daily_cs.append(cs_fraction * 10_000.0)
    estimate = estimate_spreads_from_daily(ar_daily=daily_ar, cs_daily=daily_cs)
    payload = "\n".join(
        f"{day.isoformat()}|{by_date[day].source_version}|{by_date[day].available_at.isoformat()}|"
        f"{by_date[day].open}|{by_date[day].high}|{by_date[day].low}|{by_date[day].close}"
        for day in sessions
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return SpreadEstimate(
        **{
            **estimate.__dict__,
            "input_sha256": digest,
            "input_bars": tuple(by_date[day] for day in sessions),
        }
    )


def random_committee_permutations[Bundle](
    *,
    bundles: dict[int, Bundle],
    eligible_security_ids: Sequence[int],
    commitment_sha256: str,
    k: int = 1000,
) -> tuple[tuple[Bundle, ...], ...]:
    """Jointly permute verdict bundles across the eligible point-in-time names.

    The output order follows sorted target security IDs; every bundle remains intact. Production
    callers use the pinned K=1000 default and the ticket's verified commitment digest.
    """
    if k != 1000:
        raise ValueError("random committee requires exactly K=1000 draws")
    if len(commitment_sha256) != 64 or any(c not in "0123456789abcdef" for c in commitment_sha256):
        raise ValueError("commitment identity must be a lowercase SHA-256 digest")
    targets = tuple(sorted(set(eligible_security_ids)))
    if not targets or len(targets) != len(eligible_security_ids):
        raise BenchmarkEvidenceError("eligible universe must be nonempty and unique")
    if set(bundles) != set(targets):
        raise BenchmarkEvidenceError("random committee bundles must exactly match eligible scope")
    seed_bytes = hashlib.sha256(
        bytes.fromhex(commitment_sha256) + b"|random-committee|1000"
    ).digest()
    rng = random.Random(int.from_bytes(seed_bytes, "big"))
    source = [bundles[sid] for sid in targets]
    draws: list[tuple[Bundle, ...]] = []
    for _ in range(1000):
        permutation = source.copy()
        rng.shuffle(permutation)
        draws.append(tuple(permutation))
    return tuple(draws)


@dataclass(frozen=True)
class InstrumentCost:
    """Replayable per-security cost attribution for one rebalanced period."""

    security_id: int
    previous_weight: float
    target_weight: float
    turnover: float
    per_side_cost_bps: float
    cost_return: float
    spread_input_sha256: str


@dataclass(frozen=True)
class BenchmarkWeekInputs:
    """Complete immutable calculation inputs after scorer admission and store reads."""

    run_id: UUID
    run_as_of: datetime
    week_start: date
    entry_references: dict[int, ExecutionReference]
    period_references: dict[int, BenchmarkPeriodReference]
    cost_cutoff: datetime
    outcome_cutoff: datetime
    commitment_sha256: str
    trial_identity: BenchmarkTrialIdentity
    completeness: OutcomeCompleteness
    spy_security_id: int
    universe_security_ids: tuple[int, ...]
    committee_weights: dict[int, float]
    committed_scope_ids: tuple[int, ...]
    sector_by_security_id: dict[int, str]
    sector_etf_by_sector: dict[str, int]
    quant_weights: dict[int, float]
    random_bundles: dict[int, RandomCommitteeBundle]
    random_entity_tokens: dict[int, str]
    random_calibration_fit: CalibrationFit
    random_risk_config: RiskConfig
    volatilities: dict[int, float]
    period_returns: dict[int, float]
    spreads: dict[int, SpreadEstimate]
    tbill_return: float
    tbill_accrual_steps: tuple[TBillAccrualStep, ...]
    previous_weights: dict[BenchmarkVariant, dict[Benchmark, dict[int, float]]]
    committee_previous_weights: dict[BenchmarkVariant, dict[int, float]]
    first_trial_week: bool
    random_previous_books: dict[BenchmarkVariant, tuple[dict[int, float], ...]] = field(
        default_factory=dict
    )
    previous_run_id: UUID | None = None
    previous_trial_identity_sha256: str | None = None
    previous_week_start: date | None = None
    expected_previous_week_start: date | None = None
    previous_period_end_session: date | None = None
    entry_session: date | None = None
    previous_completeness: OutcomeCompleteness | None = None
    halted: bool = False
    pre_halt_returns: dict[int, float] | None = None
    post_halt_tbill_return: float | None = None
    post_halt_tbill_returns: dict[int, float] | None = None
    halt_tau: datetime | None = None
    halt_trigger: str | None = None
    halt_event_provenance: dict[str, object] | None = None
    halt_symbol_set_provenance: dict[str, object] | None = None
    halt_reference_provenance: dict[int, dict[str, object]] | None = None
    post_halt_tbill_steps: dict[int, tuple[TBillAccrualStep, ...]] | None = None


@dataclass(frozen=True)
class PredecessorBookState:
    """Verified, drifted holdings and identity evidence for the immediately prior trial week."""

    run_id: UUID
    trial_identity_sha256: str
    week_start: date
    period_end_session: date
    completeness: OutcomeCompleteness
    previous_weights: dict[BenchmarkVariant, dict[Benchmark, dict[int, float]]]
    committee_previous_weights: dict[BenchmarkVariant, dict[int, float]]
    random_previous_books: dict[BenchmarkVariant, tuple[dict[int, float], ...]]


def _portfolio_return(
    weights: dict[int, float], returns: dict[int, float], tbill_return: float
) -> float:
    if any(weight < 0 for weight in weights.values()) or sum(weights.values()) > 1.0 + 1e-9:
        raise BenchmarkEvidenceError("benchmark book violates long-only gross exposure constraints")
    missing = set(weights) - set(returns)
    if missing:
        raise BenchmarkEvidenceError(f"missing point-in-time benchmark returns: {sorted(missing)}")
    return (
        sum(weight * returns[sid] for sid, weight in weights.items())
        + (1.0 - sum(weights.values())) * tbill_return
    )


def drift_book_weights(
    *, target_weights: dict[int, float], period_returns: dict[int, float], tbill_return: float
) -> dict[int, float]:
    """Drift a prior target book to its endpoint, including residual cash in T-bills."""
    if any(not math.isfinite(weight) or weight < 0 for weight in target_weights.values()):
        raise BenchmarkEvidenceError("prior target weights must be finite and non-negative")
    gross = sum(target_weights.values())
    if gross > 1.0 + 1e-9:
        raise BenchmarkEvidenceError("prior target book exceeds gross exposure one")
    missing = set(target_weights) - set(period_returns)
    if missing:
        raise BenchmarkEvidenceError(
            f"missing prior-period return for instruments {sorted(missing)}"
        )
    if not math.isfinite(tbill_return) or tbill_return < -1.0:
        raise BenchmarkEvidenceError("prior-period T-bill return is invalid")
    if any(
        not math.isfinite(period_returns[sid]) or period_returns[sid] < -1.0
        for sid in target_weights
    ):
        raise BenchmarkEvidenceError("prior-period instrument return is invalid")
    risky_values = {
        sid: weight * (1.0 + period_returns[sid]) for sid, weight in target_weights.items()
    }
    total_value = sum(risky_values.values()) + (1.0 - gross) * (1.0 + tbill_return)
    if not math.isfinite(total_value) or total_value <= 0:
        raise BenchmarkEvidenceError("prior book has no positive total value at the endpoint")
    return {sid: value / total_value for sid, value in risky_values.items() if value > 0}


def _weighted_books(inputs: BenchmarkWeekInputs) -> dict[Benchmark, dict[int, float]]:
    eligible = tuple(sorted(set(inputs.universe_security_ids)))
    if not eligible or len(eligible) != len(inputs.universe_security_ids):
        raise BenchmarkEvidenceError("point-in-time included universe is empty or duplicated")
    if set(inputs.committee_weights) - set(eligible):
        raise BenchmarkEvidenceError("committee book contains a name outside committed universe")
    spy = {inputs.spy_security_id: 1.0}
    gross = sum(inputs.committee_weights.values())
    if gross > 1.0 + 1e-9:
        raise BenchmarkEvidenceError("committee book exceeds gross exposure one")
    sectors: dict[int, float] = {}
    for sid, weight in inputs.committee_weights.items():
        sector = inputs.sector_by_security_id.get(sid)
        if sector is None or sector not in inputs.sector_etf_by_sector:
            raise BenchmarkEvidenceError(f"missing sector-to-ETF mapping for security {sid}")
        etf_id = inputs.sector_etf_by_sector[sector]
        sectors[etf_id] = sectors.get(etf_id, 0.0) + weight
    equal_weight = {sid: 1.0 / len(eligible) for sid in eligible}
    if set(inputs.quant_weights) - set(eligible):
        raise BenchmarkEvidenceError(
            "quant baseline contains a name outside point-in-time universe"
        )
    return {
        Benchmark.SPY: spy,
        Benchmark.EXPOSURE_MATCHED_SPY: ({inputs.spy_security_id: gross} if gross else {}),
        Benchmark.EQUAL_WEIGHT_UNIVERSE: equal_weight,
        Benchmark.SECTOR_ETF_MATCHED: sectors,
        Benchmark.QUANT_BASELINE_BOOK: dict(inputs.quant_weights),
    }


def _random_committee_books(inputs: BenchmarkWeekInputs) -> tuple[dict[int, float], ...]:
    """Jointly shuffle whole pooled forecast/bear bundles and run the pinned risk sizer."""
    scope = tuple(sorted(set(inputs.committed_scope_ids)))
    if not scope or len(scope) != len(inputs.committed_scope_ids):
        raise BenchmarkEvidenceError("committed random-control scope is empty or duplicated")
    if set(scope) - set(inputs.universe_security_ids):
        raise BenchmarkEvidenceError("committed random-control scope exceeds eligible universe")
    if set(inputs.random_bundles) != set(scope) or set(inputs.random_entity_tokens) != set(scope):
        raise BenchmarkEvidenceError("random verdict bundles do not match committed scope")
    if any(inputs.random_bundles[sid].security_id != sid for sid in scope):
        raise BenchmarkEvidenceError("random verdict bundle is stored under a different security")
    if any(
        verdict.run_id != inputs.run_id
        for bundle in inputs.random_bundles.values()
        for verdict in bundle.agent_verdicts
    ):
        raise BenchmarkEvidenceError("random verdict bundle belongs to a different run")
    if set(scope) - set(inputs.sector_by_security_id) or set(scope) - set(inputs.volatilities):
        raise BenchmarkEvidenceError("random-control scope lacks point-in-time sector/volatility")
    permutations = random_committee_permutations(
        bundles=inputs.random_bundles,
        eligible_security_ids=scope,
        commitment_sha256=inputs.commitment_sha256,
    )
    books: list[dict[int, float]] = []
    for permutation in permutations:
        pooled = {}
        bear = {}
        for target_sid, bundle in zip(scope, permutation, strict=True):
            token = inputs.random_entity_tokens[target_sid]
            pooled[target_sid] = bundle.pooled_forecast.model_copy(update={"entity_token": token})
            bear[target_sid] = bundle.bear_severity
        book = size_committee_book(
            run_id=inputs.run_id,
            as_of=inputs.run_as_of,
            pooled=pooled,
            bear=bear,
            sectors={sid: inputs.sector_by_security_id[sid] for sid in scope},
            volatilities={sid: inputs.volatilities[sid] for sid in scope},
            fit=inputs.random_calibration_fit,
            config=inputs.random_risk_config,
        )
        books.append({position.security_id: position.target_weight for position in book.positions})
    return tuple(books)


def _validate_week_continuity(inputs: BenchmarkWeekInputs) -> None:
    """Never interpret missing predecessor evidence as a new cash-started series."""
    prior_state_present = any(
        (
            inputs.previous_run_id is not None,
            inputs.previous_trial_identity_sha256 is not None,
            inputs.previous_week_start is not None,
            inputs.expected_previous_week_start is not None,
            inputs.previous_period_end_session is not None,
            inputs.entry_session is not None,
            inputs.previous_completeness is not None,
            bool(inputs.previous_weights),
            bool(inputs.committee_previous_weights),
            bool(inputs.random_previous_books),
        )
    )
    if inputs.first_trial_week:
        if prior_state_present:
            raise BenchmarkEvidenceError("first trial week cannot carry predecessor evidence")
        return

    if (
        inputs.previous_run_id is None
        or inputs.previous_trial_identity_sha256 is None
        or inputs.previous_week_start is None
        or inputs.expected_previous_week_start is None
        or inputs.previous_period_end_session is None
        or inputs.entry_session is None
        or inputs.previous_completeness is None
    ):
        raise BenchmarkEvidenceError("later trial week is missing predecessor evidence")
    if inputs.previous_run_id == inputs.run_id:
        raise BenchmarkEvidenceError("trial predecessor cannot be the current run")
    if inputs.previous_trial_identity_sha256 != inputs.trial_identity.identity_sha256:
        raise BenchmarkEvidenceError(
            "predecessor trial identity does not match current trial identity"
        )
    if inputs.previous_week_start != inputs.expected_previous_week_start:
        raise BenchmarkEvidenceError("predecessor is not the immediately preceding scheduled week")
    if inputs.previous_period_end_session != inputs.entry_session:
        raise BenchmarkEvidenceError("predecessor endpoint does not match current entry session")

    variants = set(BenchmarkVariant)
    if (
        set(inputs.previous_weights) != variants
        or set(inputs.committee_previous_weights) != variants
    ):
        raise BenchmarkEvidenceError("predecessor is missing per-variant portfolio books")
    if any(set(inputs.previous_weights[variant]) != set(Benchmark) for variant in variants):
        raise BenchmarkEvidenceError("predecessor is missing a benchmark portfolio book")
    if set(inputs.random_previous_books) != variants or any(
        len(inputs.random_previous_books[variant]) != 1000 for variant in variants
    ):
        raise BenchmarkEvidenceError("predecessor is missing paired K=1000 random books")
    if inputs.previous_completeness is OutcomeCompleteness.HALTED:
        adjusted = BenchmarkVariant.ADJUSTED
        if (
            any(inputs.previous_weights[adjusted][benchmark] for benchmark in Benchmark)
            or inputs.committee_previous_weights[adjusted]
            or any(inputs.random_previous_books[adjusted])
        ):
            raise BenchmarkEvidenceError("halted adjusted book must restart from cash")


def _validate_tbill_trace(inputs: BenchmarkWeekInputs) -> None:
    if not inputs.tbill_accrual_steps:
        raise BenchmarkEvidenceError("missing replayable T-bill vintage evidence")
    growth = 1.0
    previous: date | None = None
    for step in inputs.tbill_accrual_steps:
        if step.accrual_days < 1 or step.rate.vintage_date >= step.accrual_session:
            raise BenchmarkEvidenceError("invalid T-bill accrual step")
        if previous is not None and step.accrual_session <= previous:
            raise BenchmarkEvidenceError("T-bill accrual sessions are not strictly increasing")
        factor = 1.0 + step.rate.yield_pct / 100.0
        if factor <= 0:
            raise BenchmarkEvidenceError("T-bill yield produces a non-positive accrual factor")
        growth *= factor ** (step.accrual_days / 365.0)
        previous = step.accrual_session
    if not math.isclose(growth - 1.0, inputs.tbill_return, rel_tol=1e-10, abs_tol=1e-12):
        raise BenchmarkEvidenceError("T-bill return does not match its replayable vintage steps")


def _validate_spread_trace(inputs: BenchmarkWeekInputs) -> None:
    """Bind every cost estimate to replayable instrument-specific bars known at decision time."""
    if inputs.cost_cutoff > inputs.run_as_of:
        raise BenchmarkEvidenceError("spread evidence cutoff follows the benchmark decision time")
    required: set[int] = set()
    for book in _weighted_books(inputs).values():
        required.update(book)
    required.update(inputs.committee_weights)
    required.update(inputs.committed_scope_ids)
    for variant_books in inputs.previous_weights.values():
        for book in variant_books.values():
            required.update(book)
    for book in inputs.committee_previous_weights.values():
        required.update(book)
    for books in inputs.random_previous_books.values():
        for book in books:
            required.update(book)
    if not required or required - set(inputs.spreads):
        raise BenchmarkEvidenceError("missing per-instrument spread estimates")
    for security_id in sorted(required):
        spread = inputs.spreads[security_id]
        bars = spread.input_bars
        sessions = tuple(bar.event_time.astimezone(_ET).date() for bar in bars)
        if len(bars) != 22 or any(
            bar.available_at > inputs.cost_cutoff
            or bar.event_time > inputs.cost_cutoff
            or bar.event_time.astimezone(_ET).date() >= inputs.week_start
            for bar in bars
        ):
            raise BenchmarkEvidenceError("spread source bars are missing or after the cost cutoff")
        if any(bar.security_id != security_id or bar.feed is not PriceFeed.SIP for bar in bars):
            raise BenchmarkEvidenceError(
                "spread source bars are not instrument-specific SIP evidence"
            )
        if tuple(sorted(set(sessions))) != sessions:
            raise BenchmarkEvidenceError("spread source bars are not 22 unique ordered sessions")
        replayed = estimate_effective_spread(
            bars=bars,
            sessions=sessions,
            security_id=security_id,
            cutoff=inputs.cost_cutoff,
        )
        if replayed != spread:
            raise BenchmarkEvidenceError("stored spread estimate does not replay from its SIP bars")


def _validate_reference_evidence(inputs: BenchmarkWeekInputs) -> None:
    expected_ids = set(inputs.period_returns)
    if (
        not expected_ids
        or set(inputs.entry_references) != expected_ids
        or set(inputs.period_references) != expected_ids
    ):
        raise BenchmarkEvidenceError(
            "weekly return scope lacks complete entry/endpoint reference evidence"
        )
    entry_sessions: set[date] = set()
    endpoint_sessions: set[date] = set()
    for security_id in sorted(expected_ids):
        entry = inputs.entry_references[security_id]
        endpoint = inputs.period_references[security_id]
        if (
            entry.run_id != inputs.run_id
            or endpoint.run_id != inputs.run_id
            or entry.status is not RefStatus.RESOLVED
            or endpoint.status is not RefStatus.RESOLVED
            or entry.price is None
            or endpoint.price is None
            or entry.ref_time is None
            or endpoint.ref_time is None
            or entry.session_date is None
            or endpoint.session_date is None
        ):
            raise BenchmarkEvidenceError(
                "weekly reference evidence is unresolved or belongs to another run"
            )
        if (
            entry.available_at > inputs.outcome_cutoff
            or endpoint.available_at > inputs.outcome_cutoff
        ):
            raise BenchmarkEvidenceError("weekly reference evidence is after the admitted cutoff")
        if entry.ref_time > inputs.outcome_cutoff or endpoint.ref_time > inputs.outcome_cutoff:
            raise BenchmarkEvidenceError("weekly reference time is after the admitted cutoff")
        if (
            entry.symbol_ref != endpoint.symbol_ref
            or entry.mode is not endpoint.mode
            or endpoint.period_start != entry.session_date
            or endpoint.session_date <= entry.session_date
        ):
            raise BenchmarkEvidenceError("weekly entry and endpoint references are mismatched")
        entry_sessions.add(entry.session_date)
        endpoint_sessions.add(endpoint.session_date)
    if entry_sessions != {inputs.week_start} or len(endpoint_sessions) != 1:
        raise BenchmarkEvidenceError("weekly references disagree on entry or endpoint sessions")


def evaluate_benchmark_week(inputs: BenchmarkWeekInputs) -> tuple[BenchmarkResult, ...]:
    """Compute the six weekly books under both variants from one admitted evidence snapshot.

    Random draws jointly permute durable whole-name forecast bundles and use the existing risk
    engine. Only source bundles/configuration and the seed are persisted, never the 1000 books.
    """
    if not inputs.universe_security_ids:
        raise BenchmarkEvidenceError("missing point-in-time included universe")
    if inputs.halted != (inputs.completeness is OutcomeCompleteness.HALTED):
        raise BenchmarkEvidenceError("halt state does not match admitted completeness")
    _validate_reference_evidence(inputs)
    _validate_week_continuity(inputs)
    _validate_tbill_trace(inputs)
    _validate_spread_trace(inputs)
    books = _weighted_books(inputs)
    random_books = _random_committee_books(inputs)
    if len(random_books) != 1000:
        raise BenchmarkEvidenceError("random committee requires exactly K=1000 sized books")
    if inputs.random_previous_books and (
        set(inputs.random_previous_books) != set(BenchmarkVariant)
        or any(len(books) != 1000 for books in inputs.random_previous_books.values())
    ):
        raise BenchmarkEvidenceError("random prior book set must contain K=1000 draws per variant")
    eligible = set(inputs.universe_security_ids)
    for draw in random_books:
        if set(draw) - eligible:
            raise BenchmarkEvidenceError("random draw contains a name outside eligible universe")
        if any(weight < 0 for weight in draw.values()) or sum(draw.values()) > 1.0 + 1e-9:
            raise BenchmarkEvidenceError(
                "random draw violates long-only gross exposure constraints"
            )

    adjusted_returns = dict(inputs.period_returns)
    adjusted_resolved = not inputs.halted
    if inputs.halted:
        post_returns = inputs.post_halt_tbill_returns
        if post_returns is None and inputs.post_halt_tbill_return is not None:
            post_returns = {sid: inputs.post_halt_tbill_return for sid in inputs.period_returns}
        needed: set[int] = set(inputs.period_returns)
        adjusted_resolved = (
            inputs.pre_halt_returns is not None
            and post_returns is not None
            and needed <= set(inputs.pre_halt_returns)
            and needed <= set(post_returns)
            and inputs.halt_tau is not None
            and inputs.halt_trigger is not None
            and inputs.halt_event_provenance is not None
            and inputs.halt_symbol_set_provenance is not None
            and inputs.halt_reference_provenance is not None
            and inputs.post_halt_tbill_steps is not None
            and needed <= set(inputs.post_halt_tbill_steps)
            and needed <= set(inputs.halt_reference_provenance)
        )
        if adjusted_resolved:
            assert inputs.pre_halt_returns is not None and post_returns is not None
            if any(
                not math.isfinite(value)
                for value in (*inputs.pre_halt_returns.values(), *post_returns.values())
            ):
                adjusted_resolved = False
            else:
                adjusted_returns = {
                    sid: (1.0 + inputs.pre_halt_returns[sid]) * (1.0 + post_returns[sid]) - 1.0
                    for sid in needed
                }

    payload = {
        "run_id": str(inputs.run_id),
        "run_as_of": inputs.run_as_of.isoformat(),
        "trial_identity": inputs.trial_identity.model_dump(mode="json"),
        "trial_identity_sha256": inputs.trial_identity.identity_sha256,
        "week_start": inputs.week_start.isoformat(),
        "entry_references": {
            str(sid): reference.model_dump(mode="json")
            for sid, reference in sorted(inputs.entry_references.items())
        },
        "period_references": {
            str(sid): reference.model_dump(mode="json")
            for sid, reference in sorted(inputs.period_references.items())
        },
        "outcome_cutoff": inputs.outcome_cutoff.isoformat(),
        "cost_cutoff": inputs.cost_cutoff.isoformat(),
        "scope": sorted(inputs.universe_security_ids),
        "committed_scope": sorted(inputs.committed_scope_ids),
        "spy_security_id": inputs.spy_security_id,
        "committee_weights": {
            str(security_id): weight
            for security_id, weight in sorted(inputs.committee_weights.items())
        },
        "sector_by_security_id": inputs.sector_by_security_id,
        "sector_etf_by_sector": inputs.sector_etf_by_sector,
        "quant_weights": {
            str(security_id): weight for security_id, weight in sorted(inputs.quant_weights.items())
        },
        "trial_continuity": {
            "first_trial_week": inputs.first_trial_week,
            "previous_run_id": (
                str(inputs.previous_run_id) if inputs.previous_run_id is not None else None
            ),
            "previous_trial_identity_sha256": inputs.previous_trial_identity_sha256,
            "previous_week_start": (
                inputs.previous_week_start.isoformat()
                if inputs.previous_week_start is not None
                else None
            ),
            "expected_previous_week_start": (
                inputs.expected_previous_week_start.isoformat()
                if inputs.expected_previous_week_start is not None
                else None
            ),
            "previous_period_end_session": (
                inputs.previous_period_end_session.isoformat()
                if inputs.previous_period_end_session is not None
                else None
            ),
            "entry_session": inputs.entry_session.isoformat() if inputs.entry_session else None,
            "previous_completeness": (
                inputs.previous_completeness.value
                if inputs.previous_completeness is not None
                else None
            ),
        },
        # These weights are the drifted weights immediately before this rebalance. They are
        # indispensable for reconstructing per-instrument turnover and may not be inferred from
        # a later portfolio snapshot during replay.
        "previous_weights": {
            variant.value: {
                benchmark.value: {
                    str(security_id): weight for security_id, weight in sorted(weights.items())
                }
                for benchmark, weights in sorted(books.items(), key=lambda item: item[0].value)
            }
            for variant, books in sorted(
                inputs.previous_weights.items(), key=lambda item: item[0].value
            )
        },
        "committee_previous_weights": {
            variant.value: {
                str(security_id): weight for security_id, weight in sorted(weights.items())
            }
            for variant, weights in sorted(
                inputs.committee_previous_weights.items(), key=lambda item: item[0].value
            )
        },
        "random_previous_books": {
            variant.value: [
                {str(sid): weight for sid, weight in sorted(book.items())} for book in books
            ]
            for variant, books in sorted(
                inputs.random_previous_books.items(), key=lambda item: item[0].value
            )
        },
        "books": {benchmark.value: book for benchmark, book in books.items()},
        "random_bundles": {
            str(sid): bundle.model_dump(mode="json")
            for sid, bundle in sorted(inputs.random_bundles.items())
        },
        "random_entity_tokens": inputs.random_entity_tokens,
        "random_fit": inputs.random_calibration_fit.model_dump(mode="json"),
        "random_risk_config": inputs.random_risk_config.model_dump(mode="json"),
        "random_sectors": inputs.sector_by_security_id,
        "random_volatilities": inputs.volatilities,
        "period_returns": inputs.period_returns,
        "pre_halt_returns": inputs.pre_halt_returns,
        "post_halt_tbill_return": inputs.post_halt_tbill_return,
        "post_halt_tbill_returns": inputs.post_halt_tbill_returns,
        "halt_tau": inputs.halt_tau.isoformat() if inputs.halt_tau else None,
        "halt_trigger": inputs.halt_trigger,
        "halt_event_provenance": inputs.halt_event_provenance,
        "halt_symbol_set_provenance": inputs.halt_symbol_set_provenance,
        "halt_reference_provenance": inputs.halt_reference_provenance,
        "post_halt_tbill_steps": (
            {
                str(sid): [
                    {
                        "accrual_session": step.accrual_session.isoformat(),
                        "accrual_days": step.accrual_days,
                        "observation_date": step.rate.observation_date.isoformat(),
                        "vintage_date": step.rate.vintage_date.isoformat(),
                        "yield_pct": step.rate.yield_pct,
                        "source_version": step.rate.source_version,
                    }
                    for step in steps
                ]
                for sid, steps in sorted(inputs.post_halt_tbill_steps.items())
            }
            if inputs.post_halt_tbill_steps is not None
            else None
        ),
        "tbill_return": inputs.tbill_return,
        "tbill_accrual_steps": [
            {
                "accrual_session": step.accrual_session.isoformat(),
                "accrual_days": step.accrual_days,
                "observation_date": step.rate.observation_date.isoformat(),
                "vintage_date": step.rate.vintage_date.isoformat(),
                "yield_pct": step.rate.yield_pct,
                "source_version": step.rate.source_version,
            }
            for step in inputs.tbill_accrual_steps
        ],
        "spreads": {
            str(sid): {
                "spread_ar_21d_bps": spread.spread_ar_21d_bps,
                "spread_cs_21d_bps": spread.spread_cs_21d_bps,
                "spread_est_bps": spread.spread_est_bps,
                "per_side_cost_bps": spread.per_side_cost_bps,
                "ar_daily_bps": spread.ar_daily_bps,
                "cs_daily_bps": spread.cs_daily_bps,
                "input_sha256": spread.input_sha256,
                "input_bars": [bar.model_dump(mode="json") for bar in spread.input_bars],
            }
            for sid, spread in sorted(inputs.spreads.items())
        },
    }
    input_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    results: list[BenchmarkResult] = []

    def quantile(values: Sequence[float], probability: float) -> float:
        ordered = sorted(values)
        position = (len(ordered) - 1) * probability
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    variants = tuple(
        variant
        for variant in BenchmarkVariant
        if variant is not BenchmarkVariant.ADJUSTED or adjusted_resolved
    )
    for variant in variants:
        returns = (
            adjusted_returns if variant is BenchmarkVariant.ADJUSTED else inputs.period_returns
        )
        if not inputs.halted:
            returns = inputs.period_returns
        for benchmark in Benchmark:
            candidate_books = (
                random_books if benchmark is Benchmark.RANDOM_COMMITTEE else (books[benchmark],)
            )
            previous_books = (
                inputs.random_previous_books.get(variant, ())
                if benchmark is Benchmark.RANDOM_COMMITTEE
                and inputs.random_previous_books.get(variant)
                else tuple({} for _ in candidate_books)
                if benchmark is Benchmark.RANDOM_COMMITTEE
                else (inputs.previous_weights.get(variant, {}).get(benchmark, {}),)
            )
            gross_values: list[float] = []
            cost_values: list[float] = []
            turnover_values: list[float] = []
            cost_totals: dict[int, dict[str, float | str]] = {}
            for book, previous in zip(candidate_books, previous_books, strict=True):
                gross_value = _portfolio_return(book, returns, inputs.tbill_return)
                charges = instrument_costs(
                    previous_weights=previous, target_weights=book, spreads=inputs.spreads
                )
                if variant is BenchmarkVariant.ADJUSTED and inputs.halted:
                    # The opening rebalance cost is already charged above. Charge the modeled
                    # exit at τ once, using the instrument exposure at its halt mark.
                    assert inputs.pre_halt_returns is not None
                    exit_charges = tuple(
                        InstrumentCost(
                            security_id=sid,
                            previous_weight=0.0,
                            target_weight=0.0,
                            turnover=weight * (1.0 + inputs.pre_halt_returns[sid]),
                            per_side_cost_bps=inputs.spreads[sid].per_side_cost_bps,
                            cost_return=(
                                weight
                                * (1.0 + inputs.pre_halt_returns[sid])
                                * inputs.spreads[sid].per_side_cost_bps
                                / 10_000.0
                            ),
                            spread_input_sha256=inputs.spreads[sid].input_sha256,
                        )
                        for sid, weight in book.items()
                        if weight > 0
                    )
                    charges = (*charges, *exit_charges)
                gross_values.append(gross_value)
                cost_values.append(sum(item.cost_return for item in charges))
                turnover_values.append(sum(item.turnover for item in charges))
                for item in charges:
                    total = cost_totals.setdefault(
                        item.security_id,
                        {
                            "turnover": 0.0,
                            "cost_return": 0.0,
                            "per_side_cost_bps": item.per_side_cost_bps,
                            "spread_input_sha256": item.spread_input_sha256,
                        },
                    )
                    total["turnover"] = float(total["turnover"]) + item.turnover
                    total["cost_return"] = float(total["cost_return"]) + item.cost_return
            cost_trace = [
                {
                    "security_id": sid,
                    "mean_turnover": float(total["turnover"]) / len(candidate_books),
                    "per_side_cost_bps": total["per_side_cost_bps"],
                    "mean_cost_return": float(total["cost_return"]) / len(candidate_books),
                    "spread_input_sha256": total["spread_input_sha256"],
                }
                for sid, total in sorted(cost_totals.items())
            ]
            gross_mean = sum(gross_values) / len(gross_values)
            cost_mean = sum(cost_values) / len(cost_values)
            turnover_mean = sum(turnover_values) / len(turnover_values)
            result_payload = {**payload, "cost_attribution": cost_trace}
            provenance = hashlib.sha256(
                json.dumps(result_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            seed = hashlib.sha256(
                bytes.fromhex(inputs.commitment_sha256) + b"|random-committee|1000"
            ).hexdigest()
            random_summary = None
            if benchmark is Benchmark.RANDOM_COMMITTEE:
                committee_costs = instrument_costs(
                    previous_weights=inputs.committee_previous_weights.get(variant, {}),
                    target_weights=inputs.committee_weights,
                    spreads=inputs.spreads,
                )
                committee_net = _portfolio_return(
                    inputs.committee_weights, returns, inputs.tbill_return
                ) - sum(item.cost_return for item in committee_costs)
                net_values = [g - c for g, c in zip(gross_values, cost_values, strict=True)]
                less = sum(value < committee_net for value in net_values)
                ties = sum(value == committee_net for value in net_values)
                random_summary = RandomCommitteeSummary(
                    mean=gross_mean - cost_mean,
                    p05=quantile(net_values, 0.05),
                    p25=quantile(net_values, 0.25),
                    p50=quantile(net_values, 0.50),
                    p75=quantile(net_values, 0.75),
                    p95=quantile(net_values, 0.95),
                    committee_percentile=(less + 0.5 * ties) / len(net_values),
                    seed_sha256=seed,
                )
            results.append(
                BenchmarkResult(
                    run_id=inputs.run_id,
                    week_start=inputs.week_start,
                    benchmark=benchmark,
                    variant=variant,
                    gross_return=gross_mean,
                    cost_return=cost_mean,
                    net_return=gross_mean - cost_mean,
                    turnover=turnover_mean,
                    outcome_cutoff=inputs.outcome_cutoff,
                    commitment_sha256=inputs.commitment_sha256,
                    trial_identity_sha256=inputs.trial_identity.identity_sha256,
                    input_sha256=input_hash,
                    provenance_sha256=provenance,
                    replay_inputs=result_payload,
                    k=1000 if benchmark is Benchmark.RANDOM_COMMITTEE else None,
                    seed_sha256=seed if benchmark is Benchmark.RANDOM_COMMITTEE else None,
                    completeness=inputs.completeness,
                    random_summary=random_summary,
                )
            )
    result_keys = ((r.week_start, r.benchmark, r.variant) for r in results)
    if inputs.halted:
        assert_benchmark_variant_completeness(result_keys, (inputs.week_start,))
    else:
        assert_benchmark_completeness(result_keys, (inputs.week_start,))
    return tuple(results)


def reconstruct_predecessor_books(
    *,
    previous_inputs: BenchmarkWeekInputs,
    previous_results: Sequence[BenchmarkResult],
    current_trial_identity_sha256: str,
    expected_previous_week_start: date,
    previous_period_end_session: date,
    current_entry_session: date,
) -> PredecessorBookState:
    """Replay and drift the exact adjacent predecessor; never turn missing history into cash.

    Random draw ordinal ``i`` is reconstructed from the predecessor's own commitment seed and
    paired with ordinal ``i`` in the next week. Stored results are checked against a fresh pure
    replay before any predecessor weight is used.
    """
    if previous_inputs.week_start != expected_previous_week_start:
        raise BenchmarkEvidenceError(
            "trial predecessor is not the immediately preceding scheduled week"
        )
    if previous_period_end_session != current_entry_session:
        raise BenchmarkEvidenceError("predecessor endpoint does not match current entry session")
    identity = previous_inputs.trial_identity.identity_sha256
    if identity != current_trial_identity_sha256:
        raise BenchmarkEvidenceError(
            "predecessor trial identity does not match current trial identity"
        )
    completeness_check = (
        assert_benchmark_variant_completeness
        if previous_inputs.completeness is OutcomeCompleteness.HALTED
        else assert_benchmark_completeness
    )
    try:
        completeness_check(
            ((row.week_start, row.benchmark, row.variant) for row in previous_results),
            (previous_inputs.week_start,),
        )
    except ValueError as exc:
        raise BenchmarkEvidenceError("predecessor benchmark variant set is incomplete") from exc
    if any(
        row.run_id != previous_inputs.run_id
        or row.commitment_sha256 != previous_inputs.commitment_sha256
        or row.trial_identity_sha256 != identity
        or row.completeness is not previous_inputs.completeness
        for row in previous_results
    ):
        raise BenchmarkEvidenceError("predecessor result identity or completeness is inconsistent")
    expected_results = evaluate_benchmark_week(previous_inputs)
    expected_results = evaluate_benchmark_week(previous_inputs)
    if tuple(
        sorted(previous_results, key=lambda row: (row.benchmark.value, row.variant.value))
    ) != tuple(sorted(expected_results, key=lambda row: (row.benchmark.value, row.variant.value))):
        raise BenchmarkEvidenceError("predecessor benchmark replay does not match stored results")

    target_books = _weighted_books(previous_inputs)
    random_targets = _random_committee_books(previous_inputs)
    drifted: dict[BenchmarkVariant, dict[Benchmark, dict[int, float]]] = {}
    committee_drifted: dict[BenchmarkVariant, dict[int, float]] = {}
    random_drifted: dict[BenchmarkVariant, tuple[dict[int, float], ...]] = {}
    for variant in BenchmarkVariant:
        if previous_inputs.completeness is OutcomeCompleteness.HALTED and (
            variant is BenchmarkVariant.ADJUSTED
        ):
            drifted[variant] = {benchmark: {} for benchmark in Benchmark}
            committee_drifted[variant] = {}
            random_drifted[variant] = tuple({} for _ in range(1000))
            continue
        period_returns = previous_inputs.period_returns
        drifted[variant] = {
            benchmark: drift_book_weights(
                target_weights=book,
                period_returns=period_returns,
                tbill_return=previous_inputs.tbill_return,
            )
            for benchmark, book in target_books.items()
        }
        committee_drifted[variant] = drift_book_weights(
            target_weights=previous_inputs.committee_weights,
            period_returns=period_returns,
            tbill_return=previous_inputs.tbill_return,
        )
        random_drifted[variant] = tuple(
            drift_book_weights(
                target_weights=book,
                period_returns=period_returns,
                tbill_return=previous_inputs.tbill_return,
            )
            for book in random_targets
        )
    return PredecessorBookState(
        run_id=previous_inputs.run_id,
        trial_identity_sha256=identity,
        week_start=previous_inputs.week_start,
        period_end_session=previous_period_end_session,
        completeness=previous_inputs.completeness,
        previous_weights=drifted,
        committee_previous_weights=committee_drifted,
        random_previous_books=random_drifted,
    )


def benchmark_inputs_from_result(result: BenchmarkResult) -> BenchmarkWeekInputs:
    """Rebuild a weekly evaluator input exclusively from one immutable result snapshot."""
    payload = result.replay_inputs
    try:
        continuity = payload["trial_continuity"]
        spreads = {
            int(security_id): SpreadEstimate(
                spread_ar_21d_bps=value["spread_ar_21d_bps"],
                spread_cs_21d_bps=value["spread_cs_21d_bps"],
                spread_est_bps=value["spread_est_bps"],
                per_side_cost_bps=value["per_side_cost_bps"],
                ar_daily_bps=tuple(value["ar_daily_bps"]),
                cs_daily_bps=tuple(value["cs_daily_bps"]),
                input_sha256=value["input_sha256"],
                input_bars=tuple(PriceBar.model_validate(bar) for bar in value["input_bars"]),
            )
            for security_id, value in payload["spreads"].items()
        }
        previous_weights = {
            BenchmarkVariant(variant): {
                Benchmark(benchmark): {int(sid): weight for sid, weight in book.items()}
                for benchmark, book in books.items()
            }
            for variant, books in payload["previous_weights"].items()
        }
        committee_previous_weights = {
            BenchmarkVariant(variant): {int(sid): weight for sid, weight in weights.items()}
            for variant, weights in payload["committee_previous_weights"].items()
        }
        random_previous_books = {
            BenchmarkVariant(variant): tuple(
                {int(sid): weight for sid, weight in book.items()} for book in books
            )
            for variant, books in payload["random_previous_books"].items()
        }
        tbill_steps = tuple(
            TBillAccrualStep(
                accrual_session=date.fromisoformat(step["accrual_session"]),
                accrual_days=step["accrual_days"],
                rate=TBillRate(
                    observation_date=date.fromisoformat(step["observation_date"]),
                    vintage_date=date.fromisoformat(step["vintage_date"]),
                    yield_pct=step["yield_pct"],
                    source_version=step["source_version"],
                ),
            )
            for step in payload["tbill_accrual_steps"]
        )
        return BenchmarkWeekInputs(
            run_id=result.run_id,
            run_as_of=datetime.fromisoformat(payload["run_as_of"]),
            week_start=result.week_start,
            entry_references={
                int(sid): ExecutionReference.model_validate(reference)
                for sid, reference in payload["entry_references"].items()
            },
            period_references={
                int(sid): BenchmarkPeriodReference.model_validate(reference)
                for sid, reference in payload["period_references"].items()
            },
            cost_cutoff=datetime.fromisoformat(payload["cost_cutoff"]),
            outcome_cutoff=result.outcome_cutoff,
            commitment_sha256=result.commitment_sha256,
            trial_identity=BenchmarkTrialIdentity.model_validate(payload["trial_identity"]),
            completeness=result.completeness,
            spy_security_id=payload["spy_security_id"],
            universe_security_ids=tuple(payload["scope"]),
            committee_weights={
                int(sid): weight for sid, weight in payload["committee_weights"].items()
            },
            committed_scope_ids=tuple(payload["committed_scope"]),
            sector_by_security_id={
                int(sid): sector for sid, sector in payload["sector_by_security_id"].items()
            },
            sector_etf_by_sector=payload["sector_etf_by_sector"],
            quant_weights={int(sid): weight for sid, weight in payload["quant_weights"].items()},
            random_bundles={
                int(sid): RandomCommitteeBundle.model_validate(bundle)
                for sid, bundle in payload["random_bundles"].items()
            },
            random_entity_tokens={
                int(sid): token for sid, token in payload["random_entity_tokens"].items()
            },
            random_calibration_fit=CalibrationFit.model_validate(payload["random_fit"]),
            random_risk_config=RiskConfig.model_validate(payload["random_risk_config"]),
            volatilities={
                int(sid): volatility for sid, volatility in payload["random_volatilities"].items()
            },
            period_returns={int(sid): value for sid, value in payload["period_returns"].items()},
            spreads=spreads,
            tbill_return=payload["tbill_return"],
            tbill_accrual_steps=tbill_steps,
            previous_weights=previous_weights,
            committee_previous_weights=committee_previous_weights,
            random_previous_books=random_previous_books,
            first_trial_week=continuity["first_trial_week"],
            previous_run_id=(
                UUID(continuity["previous_run_id"])
                if continuity["previous_run_id"] is not None
                else None
            ),
            previous_trial_identity_sha256=continuity["previous_trial_identity_sha256"],
            previous_week_start=(
                date.fromisoformat(continuity["previous_week_start"])
                if continuity["previous_week_start"] is not None
                else None
            ),
            expected_previous_week_start=(
                date.fromisoformat(continuity["expected_previous_week_start"])
                if continuity["expected_previous_week_start"] is not None
                else None
            ),
            previous_period_end_session=(
                date.fromisoformat(continuity["previous_period_end_session"])
                if continuity["previous_period_end_session"] is not None
                else None
            ),
            entry_session=(
                date.fromisoformat(continuity["entry_session"])
                if continuity["entry_session"] is not None
                else None
            ),
            previous_completeness=(
                OutcomeCompleteness(continuity["previous_completeness"])
                if continuity["previous_completeness"] is not None
                else None
            ),
            halted=result.completeness is OutcomeCompleteness.HALTED,
            pre_halt_returns=(
                {int(sid): value for sid, value in payload["pre_halt_returns"].items()}
                if payload["pre_halt_returns"] is not None
                else None
            ),
            post_halt_tbill_return=payload["post_halt_tbill_return"],
            post_halt_tbill_returns=(
                {int(sid): value for sid, value in payload["post_halt_tbill_returns"].items()}
                if payload.get("post_halt_tbill_returns") is not None
                else None
            ),
            halt_tau=(
                datetime.fromisoformat(payload["halt_tau"])
                if payload.get("halt_tau") is not None
                else None
            ),
            halt_trigger=payload.get("halt_trigger"),
            halt_event_provenance=payload.get("halt_event_provenance"),
            halt_symbol_set_provenance=payload.get("halt_symbol_set_provenance"),
            halt_reference_provenance=(
                {int(sid): value for sid, value in payload["halt_reference_provenance"].items()}
                if payload.get("halt_reference_provenance") is not None
                else None
            ),
            post_halt_tbill_steps=(
                {
                    int(sid): tuple(
                        TBillAccrualStep(
                            accrual_session=date.fromisoformat(step["accrual_session"]),
                            accrual_days=step["accrual_days"],
                            rate=TBillRate(
                                observation_date=date.fromisoformat(step["observation_date"]),
                                vintage_date=date.fromisoformat(step["vintage_date"]),
                                yield_pct=step["yield_pct"],
                                source_version=step["source_version"],
                            ),
                        )
                        for step in steps
                    )
                    for sid, steps in payload["post_halt_tbill_steps"].items()
                }
                if payload.get("post_halt_tbill_steps") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BenchmarkEvidenceError("benchmark result lacks complete replay inputs") from exc


def instrument_costs(
    *,
    previous_weights: dict[int, float],
    target_weights: dict[int, float],
    spreads: dict[int, SpreadEstimate],
) -> tuple[InstrumentCost, ...]:
    """Charge each instrument's own spread on its absolute traded notional."""
    ids = sorted(set(previous_weights) | set(target_weights))
    costs: list[InstrumentCost] = []
    for security_id in ids:
        previous = previous_weights.get(security_id, 0.0)
        target = target_weights.get(security_id, 0.0)
        turnover = abs(target - previous)
        if turnover == 0:
            continue
        spread = spreads.get(security_id)
        if spread is None or not spread.input_sha256:
            raise BenchmarkEvidenceError(f"missing cost evidence for instrument {security_id}")
        costs.append(
            InstrumentCost(
                security_id=security_id,
                previous_weight=previous,
                target_weight=target,
                turnover=turnover,
                per_side_cost_bps=spread.per_side_cost_bps,
                cost_return=turnover * spread.per_side_cost_bps / 10_000.0,
                spread_input_sha256=spread.input_sha256,
            )
        )
    return tuple(costs)
