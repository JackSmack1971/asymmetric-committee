"""Shared benchmark identities, result contracts, and completeness rules."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from contracts.benchmarks import (
    BenchmarkResult,
    BenchmarkTrialIdentity,
    assert_benchmark_completeness,
    assert_benchmark_variant_completeness,
)
from contracts.enums import AgentName, Benchmark, BenchmarkVariant, OutcomeCompleteness, RunMode


def _trial_identity(model_slug: str = "vendor/model") -> BenchmarkTrialIdentity:
    agents = tuple(agent for agent in AgentName if agent is not AgentName.QUANT_BASELINE)
    return BenchmarkTrialIdentity(
        config_sha256="a" * 64,
        prompt_versions={agent: ("v1",) for agent in agents},
        served_model_slugs={agent: (model_slug,) for agent in agents},
        feature_set_versions=("fs-v1",),
        gate_model_versions=("gate-v1",),
        mode=RunMode.BACKTEST,
        ablation_id=None,
        evaluation_parameters_sha256="b" * 64,
        code_revision="c" * 40,
    )


def test_trial_identity_is_canonical_and_tracks_prompt_model_and_code_changes() -> None:
    identity = _trial_identity()
    replay = BenchmarkTrialIdentity.model_validate(identity.model_dump())
    assert identity.identity_sha256 == replay.identity_sha256
    assert identity.identity_sha256 != _trial_identity("vendor/other").identity_sha256
    changed_prompt = identity.model_copy(
        update={"prompt_versions": {agent: ("v2",) for agent in identity.prompt_versions}}
    )
    assert identity.identity_sha256 != changed_prompt.identity_sha256
    changed_code = identity.model_copy(update={"code_revision": "d" * 40})
    assert identity.identity_sha256 != changed_code.identity_sha256


def test_benchmark_and_variant_enum_values_are_pinned() -> None:
    assert {item.value for item in Benchmark} == {
        "spy",
        "exposure_matched_spy",
        "equal_weight_universe",
        "sector_etf_matched",
        "quant_baseline_book",
        "random_committee",
    }
    assert {item.value for item in BenchmarkVariant} == {"adjusted", "unadjusted"}


def test_benchmark_completeness_is_the_enum_cartesian_product() -> None:
    weeks = (date(2026, 1, 5), date(2026, 1, 12))
    complete = tuple(
        (week, benchmark, variant)
        for week in weeks
        for benchmark in Benchmark
        for variant in BenchmarkVariant
    )
    assert_benchmark_completeness(complete, weeks)


def test_halted_variant_completeness_allows_independent_complete_six_row_variants() -> None:
    week = date(2026, 1, 5)
    unadjusted = tuple((week, benchmark, BenchmarkVariant.UNADJUSTED) for benchmark in Benchmark)
    adjusted = tuple((week, benchmark, BenchmarkVariant.ADJUSTED) for benchmark in Benchmark)
    assert_benchmark_variant_completeness(unadjusted, (week,))
    assert_benchmark_variant_completeness(adjusted, (week,))
    assert_benchmark_variant_completeness((*unadjusted, *adjusted), (week,))
    with pytest.raises(ValueError, match="incomplete benchmark variant"):
        assert_benchmark_variant_completeness(unadjusted[:-1], (week,))


@pytest.mark.parametrize("missing", list(Benchmark))
@pytest.mark.parametrize("variant", list(BenchmarkVariant))
def test_benchmark_completeness_rejects_each_missing_enum_pair(
    missing: Benchmark, variant: BenchmarkVariant
) -> None:
    week = date(2026, 1, 5)
    actual = tuple(
        (week, benchmark, candidate_variant)
        for benchmark in Benchmark
        for candidate_variant in BenchmarkVariant
        if (benchmark, candidate_variant) != (missing, variant)
    )
    with pytest.raises(ValueError, match="incomplete benchmark set"):
        assert_benchmark_completeness(actual, (week,))


def test_benchmark_completeness_rejects_duplicate_and_unexpected_weeks() -> None:
    week = date(2026, 1, 5)
    complete = tuple(
        (week, benchmark, variant) for benchmark in Benchmark for variant in BenchmarkVariant
    )
    with pytest.raises(ValueError, match="duplicate benchmark result key"):
        assert_benchmark_completeness((*complete, complete[0]), (week,))
    with pytest.raises(ValueError, match="unexpected benchmark week"):
        assert_benchmark_completeness(complete, (date(2026, 1, 12),))


def test_benchmark_result_is_strict_and_uses_enums() -> None:
    replay_inputs = {"scope": [1, 2], "trial_identity_sha256": _trial_identity().identity_sha256}
    provenance = hashlib.sha256(
        json.dumps(replay_inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload = {
        "run_id": uuid4(),
        "week_start": date(2026, 1, 5),
        "benchmark": Benchmark.SPY,
        "variant": BenchmarkVariant.ADJUSTED,
        "gross_return": 0.02,
        "cost_return": 0.001,
        "net_return": 0.019,
        "turnover": 0.2,
        "outcome_cutoff": datetime(2026, 1, 12, tzinfo=UTC),
        "commitment_sha256": "a" * 64,
        "trial_identity_sha256": _trial_identity().identity_sha256,
        "input_sha256": provenance,
        "provenance_sha256": provenance,
        "replay_inputs": replay_inputs,
        "completeness": OutcomeCompleteness.COMPLETE,
    }
    result = BenchmarkResult.model_validate(payload)
    assert result.benchmark is Benchmark.SPY
    assert result.variant is BenchmarkVariant.ADJUSTED
    with pytest.raises(ValidationError, match="extra_forbidden"):
        BenchmarkResult.model_validate({**payload, "unexpected": True})
    with pytest.raises(ValidationError):
        BenchmarkResult.model_validate({**payload, "benchmark": "invented"})
    with pytest.raises(ValidationError, match="input digest"):
        BenchmarkResult.model_validate({**payload, "input_sha256": "b" * 64})
