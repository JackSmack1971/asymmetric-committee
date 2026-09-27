"""The shared commitment_v1 hash: canonical, order-free, zone-free and tamper-evident (§12.2)."""

from __future__ import annotations

import random
from datetime import timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from contracts import models as m
from contracts.commitment import (
    COMMITMENT_VERSION,
    CommitmentIntegrityError,
    commitment_hash,
    commitment_payload,
    verify_commitment,
)
from tests.contracts.strategies import committee_decision_record, portfolio_snapshot, run_record


def _for_run(
    run: m.RunRecord, snap: m.PortfolioSnapshot, decs: list[m.CommitteeDecisionRecord]
) -> tuple[m.RunRecord, list[m.CommitteeDecisionRecord], m.PortfolioSnapshot]:
    """Re-key everything to the snapshot's run, as the pipeline would have built it."""
    run = run.model_copy(update={"run_id": snap.run_id, "as_of": snap.as_of})
    fixed = [
        d.model_copy(update={"decision": d.decision.model_copy(update={"run_id": snap.run_id})})
        for d in decs
    ]
    # one decision per (security, horizon), as the table's unique key guarantees
    unique = {(d.decision.security_id, int(d.decision.horizon_days)): d for d in fixed}
    return run, list(unique.values()), snap


material = st.tuples(
    run_record(), portfolio_snapshot(), st.lists(committee_decision_record, min_size=1, max_size=6)
).map(lambda t: _for_run(*t))


@settings(max_examples=60, deadline=None)
@given(material, st.randoms(use_true_random=False))
def test_hash_ignores_decision_order(
    mat: tuple[m.RunRecord, list[m.CommitteeDecisionRecord], m.PortfolioSnapshot],
    rng: random.Random,
) -> None:
    run, decs, snap = mat
    shuffled = list(decs)
    rng.shuffle(shuffled)
    assert commitment_hash(run, shuffled, snap) == commitment_hash(run, decs, snap)


@settings(max_examples=60, deadline=None)
@given(material)
def test_hash_survives_json_round_trip(
    mat: tuple[m.RunRecord, list[m.CommitteeDecisionRecord], m.PortfolioSnapshot],
) -> None:
    run, decs, snap = mat
    again = (
        m.RunRecord.model_validate_json(run.model_dump_json()),
        [m.CommitteeDecisionRecord.model_validate_json(d.model_dump_json()) for d in decs],
        m.PortfolioSnapshot.model_validate_json(snap.model_dump_json()),
    )
    assert commitment_hash(*(again[0], again[1], again[2])) == commitment_hash(run, decs, snap)


@settings(max_examples=40, deadline=None)
@given(material)
def test_hash_is_time_zone_independent(
    mat: tuple[m.RunRecord, list[m.CommitteeDecisionRecord], m.PortfolioSnapshot],
) -> None:
    run, decs, snap = mat
    eastern = timezone(timedelta(hours=-4))
    moved = run.model_copy(update={"as_of": run.as_of.astimezone(eastern)})
    assert commitment_hash(moved, decs, snap) == commitment_hash(run, decs, snap)


@settings(max_examples=40, deadline=None)
@given(material)
def test_payload_is_versioned_and_status_free(
    mat: tuple[m.RunRecord, list[m.CommitteeDecisionRecord], m.PortfolioSnapshot],
) -> None:
    run, decs, snap = mat
    payload = commitment_payload(run, decs, snap)
    assert payload["v"] == COMMITMENT_VERSION == "commitment_v1"
    other = run.model_copy(update={"status_reason": "x", "total_cost_usd": 1.5})
    assert commitment_hash(other, decs, snap) == commitment_hash(run, decs, snap)


@settings(max_examples=60, deadline=None)
@given(material)
def test_every_tampering_is_detected(
    mat: tuple[m.RunRecord, list[m.CommitteeDecisionRecord], m.PortfolioSnapshot],
) -> None:
    run, decs, snap = mat
    stored = commitment_hash(run, decs, snap)
    verify_commitment(stored, run, decs, snap)

    first = decs[0]
    bumped = first.decision.model_copy(
        update={"pooled_p": 0.5 if first.decision.pooled_p != 0.5 else 0.25}
    )
    variants = [
        (
            run.model_copy(
                update={"config_hash": "0" * 64 if run.config_hash != "0" * 64 else "1" * 64}
            ),
            decs,
            snap,
        ),
        (run, decs[1:], snap),
        (run, [first.model_copy(update={"decision": bumped}), *decs[1:]], snap),
        (
            run,
            [first.model_copy(update={"rationale": (first.rationale or "") + "x"}), *decs[1:]],
            snap,
        ),
        (
            run,
            decs,
            snap.model_copy(update={"cash_weight": 0.99 if snap.cash_weight == 1.0 else 1.0}),
        ),
    ]
    for r, d, s in variants:
        with pytest.raises(CommitmentIntegrityError):
            verify_commitment(stored, r, d, s)


@settings(max_examples=20, deadline=None)
@given(material)
def test_cio_decision_is_committed(
    mat: tuple[m.RunRecord, list[m.CommitteeDecisionRecord], m.PortfolioSnapshot],
) -> None:
    run, decs, snap = mat
    if snap.cio is None:
        return
    stored = commitment_hash(run, decs, snap)
    changed = snap.model_copy(
        update={"cio": snap.cio.model_copy(update={"rationale": snap.cio.rationale + "!"})}
    )
    with pytest.raises(CommitmentIntegrityError):
        verify_commitment(stored, run, decs, changed)


@settings(max_examples=20, deadline=None)
@given(material)
def test_foreign_rows_are_refused(
    mat: tuple[m.RunRecord, list[m.CommitteeDecisionRecord], m.PortfolioSnapshot],
) -> None:
    run, decs, snap = mat
    other = run.model_copy(update={"run_id": type(run.run_id)(int=run.run_id.int ^ 1)})
    with pytest.raises(CommitmentIntegrityError):
        verify_commitment("0" * 64, other, decs, snap)
