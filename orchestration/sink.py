"""The only writer of run outputs (P5 plan, step 2; import-linter enforces the boundary).

Decision code (agents, committee, risk, execution) hands finished artifacts to this module and
never touches the database. ``flush_step`` writes everything one ``as_of`` step produced in a single
transaction: a failure anywhere leaves no partial step behind. Every insert is idempotent, so
replaying a step is a no-op except for the run row (status, end time, reason, cost), which moves
forward. The commitment row goes in the same transaction as the decisions it hashes (invariant 5).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import Engine, func, select

from contracts.commitment import CommitmentMaterial
from contracts.enums import RunMode, RunStatus
from contracts.errors import ResetRefusedError
from contracts.models import (
    CommitmentAnchor,
    DlqRecord,
    ExecutionRecord,
    KillSwitchEvent,
    PortfolioSnapshot,
    ProposedBook,
    RunRecord,
    StepArtifacts,
    VerdictRecord,
)
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

    def load_run(self, run_id: UUID) -> RunRecord | None:
        with self._engine.connect() as conn:
            return write.load_run(conn, run_id)

    def load_verdicts(self, run_id: UUID) -> list[VerdictRecord]:
        with self._engine.connect() as conn:
            return write.load_verdicts(conn, run_id)

    def load_book(self, run_id: UUID) -> ProposedBook | None:
        with self._engine.connect() as conn:
            return write.load_book(conn, run_id)

    def record_dlq(self, records: tuple[DlqRecord, ...]) -> None:
        """Failures that happen between steps (e.g. a dead-lettered order)."""
        with self._engine.begin() as conn:
            write.insert_dlq_records(conn, records)

    def record_halt(self, event: KillSwitchEvent) -> bool:
        """A halt is written the moment it fires: the event row and the run's ``-> PARTIAL`` move
        commit together (§9). True when this call moved the run."""
        with self._engine.begin() as conn:
            return write.record_halt(conn, event)

    def record_anchor(self, anchor: CommitmentAnchor) -> None:
        """Upgrade an existing anchor (proof, git commit, Bitcoin confirmation). It never moves the
        run: ``mark_anchored`` is the only ``COMMITTED -> ANCHORED`` path."""
        with self._engine.begin() as conn:
            write.upsert_anchor(conn, anchor)

    def mark_anchored(self, anchor: CommitmentAnchor) -> bool:
        """Anchor row and ``COMMITTED -> ANCHORED`` in one transaction; True for one caller only."""
        with self._engine.begin() as conn:
            return write.advance_to_anchored(conn, anchor)

    def load_anchor(self, run_id: UUID) -> CommitmentAnchor | None:
        with self._engine.connect() as conn:
            return write.load_anchor(conn, run_id)

    def load_commitment_material(self, run_id: UUID) -> CommitmentMaterial | None:
        """Run, decisions, book, commitment and anchor as one consistent snapshot, for recomputing
        the commitment hash. ``None`` when the run has no commitment row."""
        repeatable = self._engine.connect().execution_options(isolation_level="REPEATABLE READ")
        with repeatable as conn, conn.begin():
            return write.load_commitment_material(conn, run_id)

    def claim_run(
        self,
        *,
        mode: RunMode,
        as_of: datetime,
        config_hash: str,
        run_id: UUID,
        started_at: datetime,
        fresh: bool = False,
    ) -> tuple[RunRecord, bool]:
        with self._engine.begin() as conn:
            return write.claim_run(
                conn,
                mode=mode,
                as_of=as_of,
                config_hash=config_hash,
                run_id=run_id,
                started_at=started_at,
                fresh=fresh,
            )

    def list_runs(self, *, mode: RunMode, statuses: Sequence[RunStatus]) -> list[RunRecord]:
        with self._engine.connect() as conn:
            return write.list_runs(conn, mode=mode, statuses=statuses)

    def anchors_awaiting_confirmation(self) -> list[CommitmentAnchor]:
        with self._engine.connect() as conn:
            return write.anchors_awaiting_confirmation(conn)

    def load_open_executions(self, run_id: UUID) -> list[ExecutionRecord]:
        with self._engine.connect() as conn:
            return write.load_open_executions(conn, run_id)

    def run_ids_with_open_orders(self) -> list[UUID]:
        with self._engine.connect() as conn:
            return write.run_ids_with_open_orders(conn)

    @contextmanager
    def run_lock(self, run_id: UUID, stage: str) -> Iterator[bool]:
        """A Postgres advisory lock on ``(stage, run_id)`` held on its own connection for the
        block. Yields False (and does nothing else) when another worker holds it: business truth
        stays in Postgres, so a second delivery of the same task simply skips.

        The lock is released when the block ends, or by Postgres if the worker dies.
        """
        key = func.hashtextextended(f"{stage}|{run_id}", 0)
        with self._engine.connect() as conn:
            got = bool(conn.execute(select(func.pg_try_advisory_lock(key))).scalar_one())
            conn.commit()
            try:
                yield got
            finally:
                if got:
                    conn.execute(select(func.pg_advisory_unlock(key)))
                    conn.commit()

    def reset_run(self, run_id: UUID, *, actor: str, reason: str, now: datetime) -> dict[str, int]:
        """Reset a run that never committed. Refused while a worker holds the run (its
        ``advance`` lock), so a reset can never race a pipeline that is mid-flush."""
        with self.run_lock(run_id, "advance") as got:
            if not got:
                raise ResetRefusedError(f"run {run_id} is being processed by a worker")
            with self._engine.begin() as conn:
                return write.reset_run(conn, run_id, actor=actor, reason=reason, now=now)

    def record_executions(self, records: Sequence[ExecutionRecord]) -> None:
        """Broker evidence is written as it arrives, not at the end of the step."""
        with self._engine.begin() as conn:
            write.upsert_execution_records(conn, records)

    def find_execution(self, client_order_id: str) -> ExecutionRecord | None:
        with self._engine.connect() as conn:
            return write.load_execution_record(conn, client_order_id)

    def load_snapshot(self, run_id: UUID) -> PortfolioSnapshot | None:
        with self._engine.connect() as conn:
            return write.load_snapshot(conn, run_id)

    def load_commitment_hash(self, run_id: UUID) -> str | None:
        with self._engine.connect() as conn:
            return write.load_commitment_hash(conn, run_id)

    def load_kill_switch_events(self, run_id: UUID) -> list[KillSwitchEvent]:
        """The durable kill-switch state of a run (rebuild a ``KillSwitch`` from it on restart)."""
        with self._engine.connect() as conn:
            return write.load_kill_switch_events(conn, run_id)

    def mark_executed(self, run_id: UUID, ended_at: datetime) -> bool:
        """``ANCHORED -> EXECUTED``; True only for the call that made the transition."""
        with self._engine.begin() as conn:
            return write.advance_to_executed(conn, run_id, ended_at)
