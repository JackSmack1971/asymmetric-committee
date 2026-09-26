"""P5 sink: run cost, per-horizon committee rows, final books, DLQ, kill switch, anchors.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column("runs", sa.Column("status_reason", sa.Text(), nullable=True))
    op.add_column(
        "runs", sa.Column("total_cost_usd", sa.Double(), server_default="0", nullable=False)
    )

    # Nothing wrote committee_decisions before P5; the defaults only keep the ALTER valid.
    op.add_column(
        "committee_decisions",
        sa.Column("horizon", sa.Integer(), server_default="21", nullable=False),
    )
    op.add_column(
        "committee_decisions", sa.Column("weights", JSONB, server_default="[]", nullable=False)
    )
    op.alter_column("committee_decisions", "horizon", server_default=None)
    op.alter_column("committee_decisions", "weights", server_default=None)
    op.add_column("committee_decisions", sa.Column("pooled_logit", sa.Double(), nullable=True))
    op.add_column("committee_decisions", sa.Column("sizing_mode", sa.Text(), nullable=True))
    op.add_column("committee_decisions", sa.Column("bear_severity", sa.Text(), nullable=True))
    op.drop_constraint("uq_committee_decisions", "committee_decisions", type_="unique")
    op.create_unique_constraint(
        "uq_committee_decisions", "committee_decisions", ["run_id", "security_id", "horizon"]
    )

    op.create_table(
        "portfolio_snapshots",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cash_weight", sa.Double(), nullable=False),
        sa.Column("book", JSONB, nullable=False),
        sa.Column("cio", JSONB, nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "dlq_records",
        sa.Column("dlq_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("error_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
        sa.PrimaryKeyConstraint("dlq_id"),
        sa.UniqueConstraint("run_id", "dedupe_key", name="uq_dlq_records"),
    )
    op.create_table(
        "kill_switch_events",
        sa.Column("event_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("daily_loss", sa.Double(), nullable=True),
        sa.Column("peak_drawdown", sa.Double(), nullable=True),
        sa.Column("cancelled_order_ids", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("flattened", sa.Boolean(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("run_id", "dedupe_key", name="uq_kill_switch_events"),
    )
    op.create_table(
        "commitment_anchors",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("ots_proof", sa.LargeBinary(), nullable=True),
        sa.Column("git_commit", sa.Text(), nullable=True),
        sa.Column("anchored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
        sa.PrimaryKeyConstraint("run_id"),
    )


def downgrade() -> None:
    op.drop_table("commitment_anchors")
    op.drop_table("kill_switch_events")
    op.drop_table("dlq_records")
    op.drop_table("portfolio_snapshots")
    op.drop_constraint("uq_committee_decisions", "committee_decisions", type_="unique")
    op.create_unique_constraint(
        "uq_committee_decisions", "committee_decisions", ["run_id", "security_id"]
    )
    for col in ("bear_severity", "sizing_mode", "pooled_logit", "weights", "horizon"):
        op.drop_column("committee_decisions", col)
    op.drop_column("runs", "total_cost_usd")
    op.drop_column("runs", "status_reason")
