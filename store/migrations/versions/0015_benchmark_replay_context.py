"""Persist run-scoped benchmark sizing and version evidence.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "benchmark_replay_contexts",
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runs.run_id"),
            primary_key=True,
        ),
        sa.Column("replay_context", JSONB, nullable=False),
        sa.Column("evidence_sha256", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "evidence_sha256 ~ '^[0-9a-f]{64}$'", name="ck_benchmark_replay_context_hash"
        ),
    )
    op.execute(
        """
        CREATE FUNCTION reject_benchmark_replay_context_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'benchmark replay contexts are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER benchmark_replay_contexts_immutable
        BEFORE UPDATE OR DELETE ON benchmark_replay_contexts
        FOR EACH ROW EXECUTE FUNCTION reject_benchmark_replay_context_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION require_committed_benchmark_replay_context() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM decision_commitments WHERE run_id = NEW.run_id
            ) OR NOT EXISTS (
                SELECT 1 FROM runs WHERE run_id = NEW.run_id AND status = 'COMMITTED'
            ) THEN
                RAISE EXCEPTION 'benchmark replay context requires a committed run';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER benchmark_replay_contexts_require_commitment
        BEFORE INSERT ON benchmark_replay_contexts
        FOR EACH ROW EXECUTE FUNCTION require_committed_benchmark_replay_context()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER benchmark_replay_contexts_require_commitment ON benchmark_replay_contexts"
    )
    op.execute("DROP FUNCTION require_committed_benchmark_replay_context()")
    op.execute("DROP TRIGGER benchmark_replay_contexts_immutable ON benchmark_replay_contexts")
    op.execute("DROP FUNCTION reject_benchmark_replay_context_mutation()")
    op.drop_table("benchmark_replay_contexts")
