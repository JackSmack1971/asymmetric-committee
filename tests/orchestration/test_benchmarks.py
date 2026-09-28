from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from contracts.enums import Benchmark, BenchmarkVariant, OutcomeCompleteness, RunMode
from evaluation.scorable import Completeness, ScoringTicket
from orchestration.benchmarks import evaluate_admitted_week
from tests.evaluation.test_benchmarks import _week_inputs


def _ticket(completeness: Completeness, run_id: UUID | None = None) -> ScoringTicket:
    return ScoringTicket(
        run_id=run_id or uuid4(),
        mode=RunMode.BACKTEST,
        completeness=completeness,
        commitment_sha256="a" * 64,
        git_commit="b" * 40,
        bitcoin_height=1,
        bitcoin_block_time=datetime(2026, 1, 1, tzinfo=UTC),
        run_as_of=datetime(2026, 1, 5, tzinfo=UTC),
        requested_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


def test_admitted_complete_week_emits_every_result_pair_from_ticket_scope() -> None:
    inputs = _week_inputs(date(2026, 1, 5))
    ticket = _ticket(Completeness.COMPLETE, inputs.run_id)
    results = evaluate_admitted_week(
        ticket,
        replace(
            inputs,
            run_id=ticket.run_id,
            run_as_of=ticket.run_as_of,
            outcome_cutoff=ticket.requested_at,
            commitment_sha256=ticket.commitment_sha256,
        ),
    )
    assert len(results) == len(Benchmark) * len(BenchmarkVariant)
    assert {result.completeness for result in results} == {OutcomeCompleteness.COMPLETE}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("commitment_sha256", "c" * 64, "admitted commitment"),
        ("outcome_cutoff", datetime(2026, 2, 2, tzinfo=UTC), "admitted outcome cutoff"),
    ],
)
def test_admitted_week_rejects_late_or_uncommitted_inputs(
    field: str, value: object, message: str
) -> None:
    from dataclasses import replace

    inputs = _week_inputs(date(2026, 1, 5))
    ticket = _ticket(Completeness.COMPLETE, inputs.run_id)
    inputs = replace(inputs, run_id=ticket.run_id)
    inputs = replace(inputs, run_as_of=ticket.run_as_of)
    if field == "commitment_sha256":
        inputs = replace(inputs, commitment_sha256=str(value))
    else:
        assert isinstance(value, datetime)
        inputs = replace(inputs, outcome_cutoff=value)
    with pytest.raises(ValueError, match=message):
        evaluate_admitted_week(ticket, inputs)


def test_halted_admission_stays_halted_and_never_becomes_complete() -> None:
    source_inputs = _week_inputs(date(2026, 1, 5))
    ticket = _ticket(Completeness.HALTED, source_inputs.run_id)
    inputs = replace(
        source_inputs,
        run_id=ticket.run_id,
        run_as_of=ticket.run_as_of,
        outcome_cutoff=ticket.requested_at,
        commitment_sha256=ticket.commitment_sha256,
        completeness=OutcomeCompleteness.HALTED,
        halted=True,
        pre_halt_returns={1: 0.01, 2: 0.01, 90: 0.01, 91: 0.01, 99: 0.01},
        post_halt_tbill_return=0.001,
        post_halt_tbill_returns={sid: 0.001 for sid in (1, 2, 90, 91, 99)},
        halt_tau=datetime(2026, 1, 7, tzinfo=UTC),
        halt_trigger="daily_loss",
        halt_event_provenance={"trigger": "daily_loss"},
        halt_symbol_set_provenance={"source_version": "fixture-symbol-set-v1"},
        halt_reference_provenance={
            sid: {"source_version": "fixture-halt-v1"} for sid in (1, 2, 90, 91, 99)
        },
        post_halt_tbill_steps={sid: () for sid in (1, 2, 90, 91, 99)},
    )
    results = evaluate_admitted_week(ticket, inputs)
    assert {result.completeness for result in results} == {OutcomeCompleteness.HALTED}
