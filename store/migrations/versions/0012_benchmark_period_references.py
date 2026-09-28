"""Same-run weekly benchmark endpoint references.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "benchmark_period_references",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol_ref", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("ref_time", sa.DateTime(timezone=True)),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("price", sa.Float()),
        sa.Column("source", sa.Text()),
        sa.Column("trade_time", sa.DateTime(timezone=True)),
        sa.Column("trade_tape", sa.Text()),
        sa.Column(
            "trade_conditions", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("trade_id", sa.Text()),
        sa.Column("session_date", sa.Date()),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "(status = 'resolved' AND price IS NOT NULL AND source IS NOT NULL AND reason IS NULL "
            "AND ref_time IS NOT NULL AND session_date IS NOT NULL "
            "AND session_date > period_start) "
            "OR (status = 'unresolved' AND price IS NULL AND source IS NULL AND reason IS NOT NULL "
            "AND ((ref_time IS NOT NULL AND session_date IS NOT NULL) "
            "OR reason = 'calendar_uncovered'))",
            name="ck_benchmark_period_references_evidence",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
        sa.PrimaryKeyConstraint("run_id", "symbol_ref"),
    )
    op.create_index(
        "ix_benchmark_period_references_session",
        "benchmark_period_references",
        ["session_date", "available_at"],
    )
    op.execute(
        """
        CREATE FUNCTION reject_benchmark_period_reference_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'benchmark period references are insert-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER benchmark_period_references_immutable
        BEFORE UPDATE OR DELETE ON benchmark_period_references
        FOR EACH ROW EXECUTE FUNCTION reject_benchmark_period_reference_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER benchmark_period_references_immutable ON benchmark_period_references")
    op.execute("DROP FUNCTION reject_benchmark_period_reference_mutation()")
    op.drop_index(
        "ix_benchmark_period_references_session", table_name="benchmark_period_references"
    )
    op.drop_table("benchmark_period_references")
