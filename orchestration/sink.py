"""The only writer of run outputs (P5 plan, step 2; import-linter enforces the boundary).

Decision code (agents, committee, risk, execution) hands finished artifacts to this module and
never touches the database. ``flush_step`` writes everything one ``as_of`` step produced in a single
transaction: a failure anywhere leaves no partial step behind. Every insert is idempotent, so
replaying a step is a no-op except for the run row (status, end time, reason, cost), which moves
forward. The commitment row goes in the same transaction as the decisions it hashes (invariant 5).
"""

from __future__ import annotations

from sqlalchemy import Engine

from contracts.models import CommitmentAnchor, DlqRecord, KillSwitchEvent, StepArtifacts
from store import write


class DecisionSink:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def flush_step(self, step: StepArtifacts) -> None:
        """Write one ``as_of`` step atomically. The run row goes first (foreign-key parent)."""
        with self._engine.begin() as conn:
            write.upsert_run(conn, step.run)
            write.insert_agent_verdicts(conn, step.verdicts)
            write.insert_committee_decisions(conn, step.decisions)
            if step.portfolio is not None:
                write.insert_portfolio_snapshot(conn, step.portfolio)
            write.insert_dlq_records(conn, step.dlq)
            write.insert_kill_switch_events(conn, step.kill_switch)
            if step.commitment is not None:
                write.insert_commitment(conn, step.commitment)

    def record_dlq(self, records: tuple[DlqRecord, ...]) -> None:
        """Failures that happen between steps (e.g. a dead-lettered order)."""
        with self._engine.begin() as conn:
            write.insert_dlq_records(conn, records)

    def record_kill_switch(self, event: KillSwitchEvent) -> None:
        """A halt is written the moment it fires, not at the end of the step."""
        with self._engine.begin() as conn:
            write.insert_kill_switch_events(conn, (event,))

    def record_anchor(self, anchor: CommitmentAnchor) -> None:
        """Anchors arrive after ``COMMITTED``, so they cannot share the step transaction."""
        with self._engine.begin() as conn:
            write.upsert_anchor(conn, anchor)
