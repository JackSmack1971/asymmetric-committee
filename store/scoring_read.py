"""Read-only evidence for the scorability gate (§12.2), taken from one database snapshot.

``ScoringReader.load`` opens a single ``REPEATABLE READ, READ ONLY`` transaction and reads the
run, commitment row, decisions, book, anchor and kill-switch events inside it, so the evidence
cannot combine a commitment from one database state with an anchor or halt row from another.
``load_scoring_evidence`` refuses a connection that is not in such a transaction. Nothing here
writes, and nothing decides: the pure gate in ``evaluation.scorable`` classifies the evidence.

Row -> contract mapping for the committed material is shared with the write side
(``store._material``); a row that cannot be rebuilt becomes a typed ``MaterialDefect`` instead of
an exception, so the original reason survives to the refusal.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, select

from contracts.commitment import (
    MaterialDefect,
    MaterialDefectError,
    MaterialDefectKind,
    ScoringEvidence,
)
from contracts.models import (
    CommitmentAnchor,
    DecisionCommitment,
    KillSwitchEvent,
    PortfolioSnapshot,
    RunRecord,
)
from store import _tables as t
from store._material import decisions_from_rows, snapshot_from_row

__all__ = ["ScoringReader", "load_scoring_evidence"]


def _run(conn: Connection, run_id: UUID) -> RunRecord | None:
    row = conn.execute(select(t.runs).where(t.runs.c.run_id == run_id)).mappings().first()
    return None if row is None else RunRecord.model_validate(dict(row))


def _commitment(conn: Connection, run_id: UUID) -> DecisionCommitment | None:
    c = t.decision_commitments
    row = conn.execute(select(c).where(c.c.run_id == run_id)).mappings().first()
    return None if row is None else DecisionCommitment.model_validate(dict(row))


def _decision_rows(conn: Connection, run_id: UUID) -> list[dict[str, Any]]:
    c = t.committee_decisions.c
    query = (
        select(t.committee_decisions).where(c.run_id == run_id).order_by(c.security_id, c.horizon)
    )
    return [dict(r) for r in conn.execute(query).mappings()]


def _snapshot_row(conn: Connection, run_id: UUID) -> dict[str, Any] | None:
    c = t.portfolio_snapshots.c
    row = conn.execute(select(t.portfolio_snapshots).where(c.run_id == run_id)).mappings().first()
    return None if row is None else dict(row)


def _anchor(conn: Connection, run_id: UUID) -> CommitmentAnchor | None:
    c = t.commitment_anchors.c
    row = conn.execute(select(t.commitment_anchors).where(c.run_id == run_id)).mappings().first()
    return None if row is None else CommitmentAnchor.model_validate(dict(row))


def _events(conn: Connection, run_id: UUID) -> tuple[KillSwitchEvent, ...]:
    c = t.kill_switch_events.c
    rows = conn.execute(
        select(t.kill_switch_events).where(c.run_id == run_id).order_by(c.triggered_at, c.event_id)
    ).mappings()
    return tuple(
        KillSwitchEvent.model_validate(
            {k: r[k] for k in KillSwitchEvent.model_fields if k in r}
            | {"cancelled_order_ids": tuple(r["cancelled_order_ids"])}
        )
        for r in rows
    )


def _require_snapshot_isolation(conn: Connection) -> None:
    level: str = conn.exec_driver_sql(
        "SELECT current_setting('transaction_isolation')"
    ).scalar_one()
    read_only: str = conn.exec_driver_sql(
        "SELECT current_setting('transaction_read_only')"
    ).scalar_one()
    if level != "repeatable read" or read_only != "on":
        raise RuntimeError(
            f"scoring evidence needs a REPEATABLE READ, READ ONLY transaction (got {level}, "
            f"read_only={read_only})"
        )


def load_scoring_evidence(conn: Connection, run_id: UUID) -> ScoringEvidence:
    """Evidence for one run from the caller's snapshot transaction (refused if it is not one)."""
    _require_snapshot_isolation(conn)
    run = _run(conn, run_id)
    if run is None:
        return ScoringEvidence()
    commitment = _commitment(conn, run_id)
    decisions: Sequence[Any] = ()
    snapshot: PortfolioSnapshot | None = None
    defects: list[MaterialDefect] = []
    try:
        decisions = decisions_from_rows(run, _decision_rows(conn, run_id))
    except MaterialDefectError as exc:
        defects.append(MaterialDefect(kind=exc.kind, detail=exc.detail))
    snap_row = _snapshot_row(conn, run_id)
    if snap_row is None:
        defects.append(
            MaterialDefect(kind=MaterialDefectKind.SNAPSHOT_MISSING, detail="no stored book")
        )
    else:
        try:
            snapshot = snapshot_from_row(snap_row)
        except ValueError as exc:
            defects.append(
                MaterialDefect(kind=MaterialDefectKind.SNAPSHOT_INVALID, detail=str(exc)[:500])
            )
    return ScoringEvidence(
        run=run,
        commitment=commitment,
        decisions=tuple(decisions),
        snapshot=snapshot,
        anchor=_anchor(conn, run_id),
        kill_switch_events=_events(conn, run_id),
        defects=tuple(defects),
    )


class ScoringReader:
    """``ScoringEvidenceSource`` over an engine: one snapshot transaction per ``load``."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(self, run_id: UUID) -> ScoringEvidence:
        with self._engine.connect() as raw:
            conn = raw.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            with conn.begin():
                return load_scoring_evidence(conn, run_id)
