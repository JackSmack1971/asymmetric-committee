"""Pure benchmark spread estimates and transaction-cost rules."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from math import isclose
from uuid import uuid4

import pytest

from config.loader import load_config
from contracts.benchmarks import BenchmarkTrialIdentity, RandomCommitteeBundle
from contracts.data import PriceBar
from contracts.enums import (
    AgentName,
    BearSeverity,
    Benchmark,
    BenchmarkVariant,
    DataSufficiency,
    FeedName,
    Horizon,
    OutcomeCompleteness,
    PriceFeed,
    ReferenceSource,
    RefMode,
    RefStatus,
    RunMode,
    Stance,
    Tape,
)
from contracts.market_data import BenchmarkPeriodReference, ExecutionReference
from contracts.models import (
    AgentVerdict,
    AgentWeight,
    CalibrationFit,
    EvidenceRef,
    HorizonPool,
    PooledForecast,
)
from evaluation.benchmarks import (
    BenchmarkEvidenceError,
    BenchmarkWeekInputs,
    benchmark_inputs_from_result,
    drift_book_weights,
    estimate_effective_spread,
    estimate_spreads_from_daily,
    evaluate_benchmark_week,
    instrument_costs,
    random_committee_permutations,
    reconstruct_predecessor_books,
)
from evaluation.tbill_rules import TBillAccrualStep, TBillRate


def _bar(
    day: date,
    *,
    security_id: int = 1,
    high: float = 105,
    low: float = 95,
    close: float = 100,
) -> PriceBar:
    stamp = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=18)
    return PriceBar(
        security_id=security_id,
        event_time=stamp,
        available_at=stamp + timedelta(hours=1),
        source_version=PriceFeed.SIP.value,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1000,
        feed=PriceFeed.SIP,
    )


def test_daily_values_are_floored_before_each_estimator_aggregation() -> None:
    estimate = estimate_spreads_from_daily(
        ar_daily=(-2.0, 10.0),
        cs_daily=(6.0, -4.0),
    )
    assert estimate.spread_ar_21d_bps == 5.0
    assert estimate.spread_cs_21d_bps == 3.0
    assert estimate.spread_est_bps == 4.0
    assert estimate.per_side_cost_bps == 7.0


def test_effective_spread_requires_22_sip_sessions_and_both_estimates() -> None:
    dates = tuple(date(2026, 1, 1) + timedelta(days=i) for i in range(22))
    bars = tuple(_bar(day) for day in dates)
    estimate = estimate_effective_spread(
        bars=bars, sessions=dates, security_id=1, cutoff=datetime(2026, 2, 1, tzinfo=UTC)
    )
    assert estimate.spread_ar_21d_bps >= 0
    assert estimate.spread_cs_21d_bps >= 0
    assert isclose(
        estimate.spread_est_bps, (estimate.spread_ar_21d_bps + estimate.spread_cs_21d_bps) / 2
    )
    assert isclose(estimate.per_side_cost_bps, 0.5 * estimate.spread_est_bps + 5)
    assert len(estimate.input_bars) == 22

    with pytest.raises(BenchmarkEvidenceError, match="missing eligible SIP bar"):
        estimate_effective_spread(
            bars=bars[:-1], sessions=dates, security_id=1, cutoff=datetime(2026, 2, 1, tzinfo=UTC)
        )


def test_future_sip_bar_is_not_used_for_spread_estimate() -> None:
    dates = tuple(date(2026, 1, 1) + timedelta(days=i) for i in range(22))
    bars = tuple(_bar(day) for day in dates)
    cutoff = datetime(2026, 1, 22, tzinfo=UTC)
    with pytest.raises(BenchmarkEvidenceError, match="eligible SIP"):
        estimate_effective_spread(bars=bars, sessions=dates, security_id=1, cutoff=cutoff)


def test_costs_are_attributed_to_each_instrument_and_missing_cost_fails_closed() -> None:
    first = estimate_spreads_from_daily(ar_daily=(10.0,), cs_daily=(10.0,))
    second = estimate_spreads_from_daily(ar_daily=(30.0,), cs_daily=(30.0,))
    from dataclasses import replace

    spreads = {1: replace(first, input_sha256="a" * 64), 2: replace(second, input_sha256="b" * 64)}
    costs = instrument_costs(previous_weights={}, target_weights={1: 0.5, 2: 0.5}, spreads=spreads)
    assert [item.per_side_cost_bps for item in costs] == [10.0, 20.0]
    assert [item.cost_return for item in costs] == [0.0005, 0.001]
    with pytest.raises(BenchmarkEvidenceError, match="missing cost evidence"):
        instrument_costs(previous_weights={}, target_weights={3: 0.5}, spreads=spreads)


def test_prior_weights_drift_by_each_instrument_return_and_residual_tbill() -> None:
    drifted = drift_book_weights(
        target_weights={1: 0.6, 2: 0.2},
        period_returns={1: 0.10, 2: -0.10},
        tbill_return=0.02,
    )
    equity_value = 0.6 * 1.10 + 0.2 * 0.90
    total_value = equity_value + 0.2 * 1.02
    assert drifted == pytest.approx({1: 0.6 * 1.10 / total_value, 2: 0.2 * 0.90 / total_value})

    with pytest.raises(BenchmarkEvidenceError, match="missing prior-period return"):
        drift_book_weights(target_weights={1: 1.0}, period_returns={}, tbill_return=0.0)

    with pytest.raises(BenchmarkEvidenceError, match="positive total value"):
        drift_book_weights(target_weights={1: 1.0}, period_returns={1: -1.0}, tbill_return=0.0)


def test_later_trial_week_requires_adjacent_same_identity_predecessor() -> None:
    inputs = _week_inputs(date(2026, 1, 12))
    with pytest.raises(BenchmarkEvidenceError, match="predecessor evidence"):
        evaluate_benchmark_week(replace(inputs, first_trial_week=False))

    valid = replace(
        inputs,
        first_trial_week=False,
        previous_run_id=uuid4(),
        previous_trial_identity_sha256=inputs.trial_identity.identity_sha256,
        previous_week_start=date(2026, 1, 5),
        expected_previous_week_start=date(2026, 1, 5),
        previous_period_end_session=date(2026, 1, 12),
        entry_session=date(2026, 1, 12),
        previous_completeness=OutcomeCompleteness.COMPLETE,
        previous_weights={
            variant: {benchmark: {} for benchmark in Benchmark} for variant in BenchmarkVariant
        },
        committee_previous_weights={variant: {} for variant in BenchmarkVariant},
        random_previous_books={
            variant: tuple({} for _ in range(1000)) for variant in BenchmarkVariant
        },
    )
    results = evaluate_benchmark_week(valid)
    assert len(results) == len(Benchmark) * len(BenchmarkVariant)
    continuity = results[0].replay_inputs["trial_continuity"]
    assert continuity["previous_run_id"] == str(valid.previous_run_id)
    assert continuity["previous_trial_identity_sha256"] == inputs.trial_identity.identity_sha256
    assert continuity["previous_period_end_session"] == date(2026, 1, 12).isoformat()
    assert all(
        len(results[0].replay_inputs["random_previous_books"][variant.value]) == 1000
        for variant in BenchmarkVariant
    )

    with pytest.raises(BenchmarkEvidenceError, match="trial identity"):
        evaluate_benchmark_week(replace(valid, previous_trial_identity_sha256="e" * 64))
    with pytest.raises(BenchmarkEvidenceError, match="immediately preceding scheduled week"):
        evaluate_benchmark_week(replace(valid, previous_week_start=date(2025, 12, 29)))
    with pytest.raises(BenchmarkEvidenceError, match="endpoint does not match current entry"):
        evaluate_benchmark_week(replace(valid, previous_period_end_session=date(2026, 1, 9)))


def test_halted_previous_period_requires_adjusted_cash_reset() -> None:
    inputs = _week_inputs(date(2026, 1, 12))
    previous_books: dict[BenchmarkVariant, dict[Benchmark, dict[int, float]]] = {
        variant: {benchmark: {} for benchmark in Benchmark} for variant in BenchmarkVariant
    }
    committee_books: dict[BenchmarkVariant, dict[int, float]] = {
        variant: {} for variant in BenchmarkVariant
    }
    random_books: dict[BenchmarkVariant, tuple[dict[int, float], ...]] = {
        variant: tuple({} for _ in range(1000)) for variant in BenchmarkVariant
    }
    previous_books[BenchmarkVariant.ADJUSTED][Benchmark.SPY] = {99: 1.0}
    committee_books[BenchmarkVariant.ADJUSTED] = {1: 0.5}
    random_books[BenchmarkVariant.ADJUSTED] = tuple({1: 0.5} for _ in range(1000))
    valid = replace(
        inputs,
        first_trial_week=False,
        previous_run_id=uuid4(),
        previous_trial_identity_sha256=inputs.trial_identity.identity_sha256,
        previous_week_start=date(2026, 1, 5),
        expected_previous_week_start=date(2026, 1, 5),
        previous_period_end_session=date(2026, 1, 12),
        entry_session=date(2026, 1, 12),
        previous_completeness=OutcomeCompleteness.HALTED,
        previous_weights=previous_books,
        committee_previous_weights=committee_books,
        random_previous_books=random_books,
    )
    with pytest.raises(BenchmarkEvidenceError, match="halted adjusted book must restart from cash"):
        evaluate_benchmark_week(valid)

    adjusted = BenchmarkVariant.ADJUSTED
    previous_books[adjusted] = {benchmark: {} for benchmark in Benchmark}
    committee_books[adjusted] = {}
    random_books[adjusted] = tuple({} for _ in range(1000))
    assert len(
        evaluate_benchmark_week(
            replace(
                valid,
                previous_weights=previous_books,
                committee_previous_weights=committee_books,
                random_previous_books=random_books,
            )
        )
    ) == len(Benchmark) * len(BenchmarkVariant)


def test_predecessor_replay_drifts_books_and_pairs_random_ordinals() -> None:
    previous = _week_inputs(date(2026, 1, 5))
    stored = evaluate_benchmark_week(previous)
    state = reconstruct_predecessor_books(
        previous_inputs=previous,
        previous_results=stored,
        current_trial_identity_sha256=previous.trial_identity.identity_sha256,
        expected_previous_week_start=date(2026, 1, 5),
        previous_period_end_session=date(2026, 1, 12),
        current_entry_session=date(2026, 1, 12),
    )
    assert state.run_id == previous.run_id
    assert state.trial_identity_sha256 == previous.trial_identity.identity_sha256
    assert state.previous_weights[BenchmarkVariant.UNADJUSTED][Benchmark.SPY] == pytest.approx(
        {99: 1.0}
    )
    assert (
        state.committee_previous_weights[BenchmarkVariant.UNADJUSTED] != previous.committee_weights
    )
    assert len(state.random_previous_books[BenchmarkVariant.ADJUSTED]) == 1000
    assert (
        state.random_previous_books[BenchmarkVariant.ADJUSTED]
        == (state.random_previous_books[BenchmarkVariant.UNADJUSTED])
    )

    with pytest.raises(BenchmarkEvidenceError, match="trial identity"):
        reconstruct_predecessor_books(
            previous_inputs=previous,
            previous_results=stored,
            current_trial_identity_sha256="e" * 64,
            expected_previous_week_start=date(2026, 1, 5),
            previous_period_end_session=date(2026, 1, 12),
            current_entry_session=date(2026, 1, 12),
        )
    with pytest.raises(BenchmarkEvidenceError, match="incomplete"):
        reconstruct_predecessor_books(
            previous_inputs=previous,
            previous_results=stored[:-1],
            current_trial_identity_sha256=previous.trial_identity.identity_sha256,
            expected_previous_week_start=date(2026, 1, 5),
            previous_period_end_session=date(2026, 1, 12),
            current_entry_session=date(2026, 1, 12),
        )


def test_halted_predecessor_resets_adjusted_series_but_keeps_unadjusted_drift() -> None:
    previous = replace(
        _week_inputs(date(2026, 1, 5), halted=True), completeness=OutcomeCompleteness.HALTED
    )
    state = reconstruct_predecessor_books(
        previous_inputs=previous,
        previous_results=evaluate_benchmark_week(previous),
        current_trial_identity_sha256=previous.trial_identity.identity_sha256,
        expected_previous_week_start=date(2026, 1, 5),
        previous_period_end_session=date(2026, 1, 12),
        current_entry_session=date(2026, 1, 12),
    )
    assert all(not book for book in state.previous_weights[BenchmarkVariant.ADJUSTED].values())
    assert state.committee_previous_weights[BenchmarkVariant.ADJUSTED] == {}
    assert not any(state.random_previous_books[BenchmarkVariant.ADJUSTED])
    assert state.previous_weights[BenchmarkVariant.UNADJUSTED][Benchmark.SPY]


def test_random_committee_has_exactly_1000_replayable_joint_permutations() -> None:
    bundles = {
        1: ("a", "a5", "a21", "bear"),
        2: ("b", "b5", "b21", "low"),
        3: ("c", "c5", "c21", "high"),
    }
    first = random_committee_permutations(
        bundles=bundles, eligible_security_ids=(1, 2, 3), commitment_sha256="a" * 64
    )
    replay = random_committee_permutations(
        bundles=bundles, eligible_security_ids=(1, 2, 3), commitment_sha256="a" * 64
    )
    other = random_committee_permutations(
        bundles=bundles, eligible_security_ids=(1, 2, 3), commitment_sha256="b" * 64
    )
    assert len(first) == 1000
    assert first == replay
    assert first != other
    for draw in first:
        assert set(draw) == set(bundles.values())
    with pytest.raises(BenchmarkEvidenceError, match="eligible scope"):
        random_committee_permutations(
            bundles=bundles,
            eligible_security_ids=(1, 2),
            commitment_sha256="a" * 64,
        )


def _week_inputs(week: date, *, halted: bool = False) -> BenchmarkWeekInputs:
    ids = (1, 2, 90, 91, 99)
    run_id = uuid4()

    def bundle(sid: int, token: str, logit: float, severity: BearSeverity) -> RandomCommitteeBundle:
        verdict = AgentVerdict(
            stance=Stance.BUY,
            p_outperform_5=0.6,
            p_outperform_21=0.65,
            p_outperform_63=0.7,
            key_evidence=(EvidenceRef(source=FeedName.FEATURES, row_id="f1", note="fixture"),),
            risks=(),
            data_sufficiency=DataSufficiency.FULL,
            run_id=run_id,
            agent=AgentName.VALUE,
            entity_token=token,
            as_of=datetime(2026, 1, 5, tzinfo=UTC),
            prompt_version="fixture-v1",
            model_served="fixture/model",
            valid=True,
        )
        pooled = PooledForecast(
            entity_token=token,
            pools=tuple(
                HorizonPool(
                    horizon=horizon,
                    logit=logit,
                    lambda_t=0.0,
                    dispersion=0.0,
                    weights=(AgentWeight(agent=AgentName.VALUE, weight=1.0),),
                )
                for horizon in Horizon
            ),
        )
        return RandomCommitteeBundle(
            security_id=sid,
            entity_token=token,
            agent_verdicts=(verdict,),
            pooled_forecast=pooled,
            bear_severity=severity,
        )

    config = load_config(allow_placeholders=True, env={})
    run_as_of = datetime.combine(week, datetime.min.time(), tzinfo=UTC)
    cost_sessions: list[date] = []
    cursor = week - timedelta(days=1)
    while len(cost_sessions) < 22:
        if cursor.weekday() < 5:
            cost_sessions.append(cursor)
        cursor -= timedelta(days=1)
    cost_sessions.reverse()
    entry_references = {
        sid: ExecutionReference(
            run_id=run_id,
            symbol_ref=f"S{sid}",
            ref_time=run_as_of,
            mode=RefMode.BACKTEST,
            session_date=week,
            available_at=run_as_of,
            source_version="fixture-entry-v1",
            status=RefStatus.RESOLVED,
            price=100.0,
            source=ReferenceSource.SIP_LAST,
            trade_time=run_as_of,
            trade_tape=Tape.A,
        )
        for sid in ids
    }
    endpoint_day = week + timedelta(days=7)
    period_references = {
        sid: BenchmarkPeriodReference(
            run_id=run_id,
            symbol_ref=f"S{sid}",
            period_start=week,
            ref_time=datetime.combine(endpoint_day, datetime.min.time(), tzinfo=UTC),
            mode=RefMode.BACKTEST,
            session_date=endpoint_day,
            available_at=datetime(2026, 2, 1, tzinfo=UTC),
            source_version="fixture-endpoint-v1",
            status=RefStatus.RESOLVED,
            price=101.0,
            source=ReferenceSource.SIP_LAST,
            trade_time=datetime.combine(endpoint_day, datetime.min.time(), tzinfo=UTC),
            trade_tape=Tape.A,
        )
        for sid in ids
    }
    return BenchmarkWeekInputs(
        run_id=run_id,
        run_as_of=run_as_of,
        week_start=week,
        outcome_cutoff=datetime(2026, 2, 1, tzinfo=UTC),
        commitment_sha256="a" * 64,
        trial_identity=BenchmarkTrialIdentity(
            config_sha256="b" * 64,
            prompt_versions={
                agent: ("fixture-v1",)
                for agent in AgentName
                if agent is not AgentName.QUANT_BASELINE
            },
            served_model_slugs={
                agent: ("fixture/model",)
                for agent in AgentName
                if agent is not AgentName.QUANT_BASELINE
            },
            feature_set_versions=("fixture-fs-v1",),
            gate_model_versions=("fixture-gate-v1",),
            mode=RunMode.BACKTEST,
            ablation_id=None,
            evaluation_parameters_sha256="c" * 64,
            code_revision="d" * 40,
        ),
        completeness=(OutcomeCompleteness.HALTED if halted else OutcomeCompleteness.COMPLETE),
        spy_security_id=99,
        universe_security_ids=(1, 2),
        committee_weights={1: 0.5},
        committed_scope_ids=(1, 2),
        sector_by_security_id={1: "Technology", 2: "Health"},
        sector_etf_by_sector={"Technology": 90, "Health": 91},
        quant_weights={2: 0.5},
        random_bundles={
            1: bundle(1, "ENTITY_01", 1.0, BearSeverity.LOW),
            2: bundle(2, "ENTITY_02", 0.5, BearSeverity.HIGH),
        },
        random_entity_tokens={1: "ENTITY_01", 2: "ENTITY_02"},
        random_calibration_fit=CalibrationFit(
            horizon=Horizon.D21,
            alpha=0.0,
            beta=1.0,
            active=False,
            independent_periods=0,
            observations=0,
            base_rate=0.5,
        ),
        random_risk_config=config.risk,
        volatilities={1: 0.1, 2: 0.1},
        period_returns={1: 0.1, 2: -0.1, 90: 0.04, 91: 0.05, 99: 0.02},
        entry_references=entry_references,
        period_references=period_references,
        cost_cutoff=run_as_of,
        spreads={
            sid: estimate_effective_spread(
                bars=tuple(_bar(day, security_id=sid) for day in cost_sessions),
                sessions=tuple(cost_sessions),
                security_id=sid,
                cutoff=run_as_of,
            )
            for sid in ids
        },
        tbill_return=0.001,
        tbill_accrual_steps=(
            TBillAccrualStep(
                accrual_session=week + timedelta(days=7),
                accrual_days=7,
                rate=TBillRate(
                    observation_date=week,
                    vintage_date=week + timedelta(days=1),
                    yield_pct=((1.001 ** (365 / 7)) - 1) * 100,
                    source_version="fixture-alfred-vintage",
                ),
            ),
        ),
        previous_weights={},
        committee_previous_weights={},
        first_trial_week=True,
        halted=halted,
        pre_halt_returns={sid: 0.01 for sid in ids} if halted else None,
        post_halt_tbill_return=0.002 if halted else None,
        post_halt_tbill_returns={sid: 0.002 for sid in ids} if halted else None,
        halt_tau=datetime(2026, 1, 7, tzinfo=UTC) if halted else None,
        halt_trigger="daily_loss" if halted else None,
        halt_event_provenance={"trigger": "daily_loss"} if halted else None,
        halt_symbol_set_provenance={"source_version": "fixture-symbol-set-v1"} if halted else None,
        halt_reference_provenance={sid: {"source_version": "fixture-halt-v1"} for sid in ids}
        if halted
        else None,
        post_halt_tbill_steps={sid: () for sid in ids} if halted else None,
    )


def test_every_benchmark_and_variant_is_produced_for_multiple_weeks() -> None:
    weeks = (date(2026, 1, 5), date(2026, 1, 12))
    first_input = _week_inputs(weeks[0])
    results = (
        *evaluate_benchmark_week(first_input),
        *evaluate_benchmark_week(_week_inputs(weeks[1])),
    )
    assert len(results) == len(weeks) * len(Benchmark) * len(BenchmarkVariant)
    assert {(r.week_start, r.benchmark, r.variant) for r in results} == {
        (week, benchmark, variant)
        for week in weeks
        for benchmark in Benchmark
        for variant in BenchmarkVariant
    }
    random = [r for r in results if r.benchmark is Benchmark.RANDOM_COMMITTEE]
    assert all(r.k == 1000 and r.seed_sha256 is not None for r in random)
    assert all(r.replay_inputs and r.provenance_sha256 for r in results)
    assert results[0].replay_inputs["previous_weights"] == {}
    assert results[0].replay_inputs["committee_previous_weights"] == {}
    assert (
        results[0].replay_inputs["spreads"]["1"]["spread_ar_21d_bps"]
        == first_input.spreads[1].spread_ar_21d_bps
    )
    assert (
        results[0].replay_inputs["spreads"]["1"]["spread_cs_21d_bps"]
        == first_input.spreads[1].spread_cs_21d_bps
    )
    restored = benchmark_inputs_from_result(results[0])
    assert restored == first_input
    assert evaluate_benchmark_week(restored) == evaluate_benchmark_week(first_input)
    random_result = next(r for r in random if r.variant is BenchmarkVariant.ADJUSTED)
    assert "random_bundles" in random_result.replay_inputs
    assert "random_draw_weights" not in random_result.replay_inputs
    assert random_result == next(
        r
        for r in evaluate_benchmark_week(first_input)
        if r.benchmark is Benchmark.RANDOM_COMMITTEE and r.variant is BenchmarkVariant.ADJUSTED
    )


def test_halt_adjusted_spy_return_uses_halt_mark_then_tbill() -> None:
    inputs = _week_inputs(date(2026, 1, 5), halted=True)
    results = evaluate_benchmark_week(inputs)
    adjusted = next(
        r
        for r in results
        if r.benchmark is Benchmark.SPY and r.variant is BenchmarkVariant.ADJUSTED
    )
    unadjusted = next(
        r
        for r in results
        if r.benchmark is Benchmark.SPY and r.variant is BenchmarkVariant.UNADJUSTED
    )
    assert isclose(adjusted.gross_return, (1.01 * 1.002) - 1.0)
    assert isclose(unadjusted.gross_return, 0.02)
    per_side = inputs.spreads[99].per_side_cost_bps / 10_000.0
    assert isclose(unadjusted.cost_return, per_side)
    assert isclose(adjusted.cost_return, per_side * (1.0 + 1.01))


def test_missing_halt_adjustment_evidence_keeps_unadjusted_grid() -> None:
    inputs = replace(
        _week_inputs(date(2026, 1, 5), halted=True),
        pre_halt_returns=None,
        post_halt_tbill_return=None,
        post_halt_tbill_returns=None,
        halt_tau=None,
        halt_trigger=None,
        halt_event_provenance=None,
        halt_symbol_set_provenance=None,
        halt_reference_provenance=None,
        post_halt_tbill_steps=None,
    )
    results = evaluate_benchmark_week(inputs)
    assert len(results) == len(Benchmark)
    assert {row.variant for row in results} == {BenchmarkVariant.UNADJUSTED}


def test_missing_halt_tbill_continuation_affects_only_adjusted_variant() -> None:
    inputs = replace(
        _week_inputs(date(2026, 1, 5), halted=True),
        post_halt_tbill_return=None,
        post_halt_tbill_returns=None,
        post_halt_tbill_steps=None,
    )
    results = evaluate_benchmark_week(inputs)
    assert len(results) == len(Benchmark)
    assert {row.variant for row in results} == {BenchmarkVariant.UNADJUSTED}


def test_missing_benchmark_return_evidence_fails_closed() -> None:
    from dataclasses import replace

    inputs = _week_inputs(date(2026, 1, 5))
    with pytest.raises(BenchmarkEvidenceError, match="missing point-in-time benchmark returns"):
        evaluate_benchmark_week(
            replace(
                inputs,
                period_returns={2: -0.1, 99: 0.02},
                entry_references={sid: inputs.entry_references[sid] for sid in (2, 99)},
                period_references={sid: inputs.period_references[sid] for sid in (2, 99)},
            )
        )


def test_missing_or_mismatched_tbill_vintage_trace_fails_closed() -> None:
    from dataclasses import replace

    inputs = _week_inputs(date(2026, 1, 5))
    with pytest.raises(BenchmarkEvidenceError, match="T-bill vintage evidence"):
        evaluate_benchmark_week(replace(inputs, tbill_accrual_steps=()))
    with pytest.raises(BenchmarkEvidenceError, match="does not match"):
        evaluate_benchmark_week(replace(inputs, tbill_return=0.0))


def test_week_requires_complete_same_run_entry_and_endpoint_references() -> None:
    from dataclasses import replace

    inputs = _week_inputs(date(2026, 1, 5))
    with pytest.raises(BenchmarkEvidenceError, match="reference evidence"):
        evaluate_benchmark_week(replace(inputs, entry_references={}))
    late = dict(inputs.period_references)
    first = next(iter(late))
    late[first] = late[first].model_copy(
        update={"available_at": inputs.outcome_cutoff + timedelta(seconds=1)}
    )
    with pytest.raises(BenchmarkEvidenceError, match="cutoff"):
        evaluate_benchmark_week(replace(inputs, period_references=late))


def test_random_committee_refuses_bundles_outside_admitted_and_eligible_scope() -> None:
    from dataclasses import replace

    inputs = _week_inputs(date(2026, 1, 5))
    with pytest.raises(BenchmarkEvidenceError, match="do not match committed scope"):
        evaluate_benchmark_week(replace(inputs, committed_scope_ids=(1,)))
    with pytest.raises(BenchmarkEvidenceError, match="exceeds eligible universe"):
        evaluate_benchmark_week(replace(inputs, universe_security_ids=(1,), quant_weights={1: 0.5}))


def test_spread_trace_is_decision_time_bound_and_replayable() -> None:
    inputs = _week_inputs(date(2026, 1, 5))
    result = evaluate_benchmark_week(inputs)[0]
    assert result.replay_inputs["cost_cutoff"] == inputs.run_as_of.isoformat()
    assert len(result.replay_inputs["spreads"]["1"]["input_bars"]) == 22
    with pytest.raises(BenchmarkEvidenceError, match="follows the benchmark decision time"):
        evaluate_benchmark_week(
            replace(inputs, cost_cutoff=inputs.run_as_of + timedelta(seconds=1))
        )
    changed_bar = inputs.spreads[1].input_bars[0].model_copy(update={"close": 101.0})
    changed_spread = replace(
        inputs.spreads[1], input_bars=(changed_bar, *inputs.spreads[1].input_bars[1:])
    )
    with pytest.raises(BenchmarkEvidenceError, match="does not replay"):
        evaluate_benchmark_week(replace(inputs, spreads={**inputs.spreads, 1: changed_spread}))
