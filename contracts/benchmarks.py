"""Typed identities and immutable results for evaluation books and benchmarks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from contracts.enums import (
    AgentName,
    Benchmark,
    BenchmarkVariant,
    OutcomeCompleteness,
    RunMode,
)
from contracts.models import (
    BenchmarkForecastBundle,
    Contract,
    Finite,
    NonNegative,
    Sha256Hex,
)

RandomCommitteeBundle = BenchmarkForecastBundle


class RandomCommitteeSummary(Contract):
    """K-draw net-return summary; quantiles use linear interpolation between order statistics."""

    k: Literal[1000] = 1000
    mean: Finite
    p05: Finite
    p25: Finite
    p50: Finite
    p75: Finite
    p95: Finite
    committee_percentile: Finite
    seed_sha256: Sha256Hex

    @model_validator(mode="after")
    def _ordered(self) -> RandomCommitteeSummary:
        if not (self.p05 <= self.p25 <= self.p50 <= self.p75 <= self.p95):
            raise ValueError("random committee return quantiles must be ordered")
        if not 0.0 <= self.committee_percentile <= 1.0:
            raise ValueError("committee percentile must be a fraction in [0, 1]")
        return self


class BenchmarkTrialIdentity(Contract):
    """The durable §12.3 identity used only to link P6.6 weekly turnover series."""

    config_sha256: Sha256Hex
    prompt_versions: dict[AgentName, tuple[str, ...]]
    served_model_slugs: dict[AgentName, tuple[str, ...]]
    feature_set_versions: tuple[str, ...] = Field(min_length=1)
    gate_model_versions: tuple[str, ...] = Field(min_length=1)
    mode: RunMode
    ablation_id: str | None
    evaluation_parameters_sha256: Sha256Hex
    code_revision: str = Field(min_length=7)

    @model_validator(mode="after")
    def _canonical_sources(self) -> BenchmarkTrialIdentity:
        if set(self.prompt_versions) != set(self.served_model_slugs):
            raise ValueError("trial prompt and served-model agent keys must match")
        if AgentName.QUANT_BASELINE in self.prompt_versions:
            raise ValueError("deterministic quant baseline has no prompt/model identity")
        if any(
            not values or tuple(sorted(set(values))) != values
            for values in self.prompt_versions.values()
        ):
            raise ValueError("trial prompt versions must be nonempty, unique, and sorted per agent")
        if any(
            not values or tuple(sorted(set(values))) != values
            for values in self.served_model_slugs.values()
        ):
            raise ValueError("trial served models must be nonempty, unique, and sorted per agent")
        if tuple(sorted(set(self.feature_set_versions))) != self.feature_set_versions:
            raise ValueError("trial feature-set versions must be unique and sorted")
        if tuple(sorted(set(self.gate_model_versions))) != self.gate_model_versions:
            raise ValueError("trial gate-model versions must be unique and sorted")
        return self

    @property
    def identity_sha256(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(canonical).hexdigest()


class BenchmarkResult(Contract):
    """One benchmark/variant weekly result, backed by a durable replay-input snapshot."""

    run_id: UUID
    week_start: date
    benchmark: Benchmark
    variant: BenchmarkVariant
    gross_return: Finite
    cost_return: NonNegative
    net_return: Finite
    turnover: NonNegative
    outcome_cutoff: AwareDatetime
    commitment_sha256: Sha256Hex
    trial_identity_sha256: Sha256Hex
    input_sha256: Sha256Hex
    provenance_sha256: Sha256Hex
    replay_inputs: dict[str, Any]
    k: int | None = None
    seed_sha256: Sha256Hex | None = None
    completeness: OutcomeCompleteness
    random_summary: RandomCommitteeSummary | None = None

    @model_validator(mode="after")
    def _replay_contract(self) -> BenchmarkResult:
        if self.replay_inputs.get("trial_identity_sha256") != self.trial_identity_sha256:
            raise ValueError("trial identity digest does not match replay inputs")
        canonical_inputs = json.dumps(
            self.replay_inputs, sort_keys=True, separators=(",", ":")
        ).encode()
        if hashlib.sha256(canonical_inputs).hexdigest() != self.provenance_sha256:
            raise ValueError("provenance digest does not match replay inputs")
        calculation_inputs = dict(self.replay_inputs)
        calculation_inputs.pop("cost_attribution", None)
        canonical_calculation = json.dumps(
            calculation_inputs, sort_keys=True, separators=(",", ":")
        ).encode()
        if hashlib.sha256(canonical_calculation).hexdigest() != self.input_sha256:
            raise ValueError("input digest does not match replay calculation inputs")
        if self.benchmark is Benchmark.RANDOM_COMMITTEE:
            if self.k != 1000 or self.seed_sha256 is None or self.random_summary is None:
                raise ValueError("random committee results require K=1000 and a full summary")
            expected_seed = hashlib.sha256(
                bytes.fromhex(self.commitment_sha256) + b"|random-committee|1000"
            ).hexdigest()
            if self.seed_sha256 != expected_seed:
                raise ValueError("random committee seed does not match committed identity")
            if self.random_summary.seed_sha256 != self.seed_sha256:
                raise ValueError("random summary seed does not match result seed")
            if self.random_summary.mean != self.net_return:
                raise ValueError("random summary mean must match benchmark net return")
        elif self.k is not None or self.seed_sha256 is not None:
            raise ValueError("K and seed are only valid for random committee results")
        elif self.random_summary is not None:
            raise ValueError("random summary is only valid for random committee results")
        if abs(self.net_return - (self.gross_return - self.cost_return)) > 1e-9:
            raise ValueError("net return must equal gross return less modeled costs")
        return self


def assert_benchmark_completeness(
    results: Iterable[tuple[date, Benchmark, BenchmarkVariant]],
    expected_weeks: Sequence[date],
) -> None:
    """Require exactly every enum-defined benchmark/variant for every expected week."""
    expected_week_set = set(expected_weeks)
    if len(expected_week_set) != len(expected_weeks):
        raise ValueError("expected benchmark weeks contain duplicates")

    expected = {
        (week, benchmark, variant)
        for week in expected_week_set
        for benchmark in Benchmark
        for variant in BenchmarkVariant
    }
    actual_items = tuple(results)
    actual = set(actual_items)
    if len(actual) != len(actual_items):
        raise ValueError("duplicate benchmark result key")
    unexpected = actual - expected
    if unexpected:
        raise ValueError(f"unexpected benchmark week or identity: {sorted(unexpected, key=str)}")
    missing = expected - actual
    if missing:
        raise ValueError(f"incomplete benchmark set: {sorted(missing, key=str)}")


def assert_benchmark_variant_completeness(
    results: Iterable[tuple[date, Benchmark, BenchmarkVariant]],
    expected_weeks: Sequence[date],
) -> None:
    """Require all six benchmark rows for each independently persisted variant."""
    actual_items = tuple(results)
    if not actual_items:
        raise ValueError("benchmark result batch cannot be empty")
    if len(set(actual_items)) != len(actual_items):
        raise ValueError("duplicate benchmark result key")
    expected_week_set = set(expected_weeks)
    if len(expected_week_set) != len(expected_weeks):
        raise ValueError("expected benchmark weeks contain duplicates")
    variants = {variant for _, _, variant in actual_items}
    expected = {
        (week, benchmark, variant)
        for week in expected_week_set
        for benchmark in Benchmark
        for variant in variants
    }
    actual = set(actual_items)
    if actual - expected:
        raise ValueError(
            f"unexpected benchmark week or identity: {sorted(actual - expected, key=str)}"
        )
    missing = expected - actual
    if missing:
        raise ValueError(f"incomplete benchmark variant: {sorted(missing, key=str)}")
