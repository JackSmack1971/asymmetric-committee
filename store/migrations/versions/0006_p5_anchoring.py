"""Anchoring integrity (§11, §12.2): committee entity token, anchor -> commitment FK, run resets.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("committee_decisions", sa.Column("entity_token", sa.Text(), nullable=True))
    op.create_unique_constraint(
        "uq_decision_commitments_run_sha", "decision_commitments", ["run_id", "sha256"]
    )
    # An anchor must name a real commitment. None can exist before P5 anchoring ran, so a database
    # holding an anchor without a matching commitment is refused rather than silently repaired.
    orphans: int = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM commitment_anchors a LEFT JOIN decision_commitments c "
                "ON c.run_id = a.run_id AND c.sha256 = a.sha256 WHERE c.run_id IS NULL"
            )
        )
        .scalar_one()
    )
    if orphans:
        raise RuntimeError(f"{orphans} anchors do not match a stored commitment; cannot migrate")
    op.create_foreign_key(
        "fk_commitment_anchors_commitment",
        "commitment_anchors",
        "decision_commitments",
        ["run_id", "sha256"],
        ["run_id", "sha256"],
    )
    op.create_table(
        "run_resets",
        sa.Column("reset_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("prior_status", sa.Text(), nullable=False),
        sa.Column("cleared", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
        sa.PrimaryKeyConstraint("reset_id"),
    )
    op.execute(
        "CREATE TRIGGER run_resets_immutable BEFORE UPDATE OR DELETE ON run_resets "
        "FOR EACH ROW EXECUTE FUNCTION reject_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER run_resets_immutable ON run_resets")
    op.drop_table("run_resets")
    op.drop_constraint("fk_commitment_anchors_commitment", "commitment_anchors", type_="foreignkey")
    op.drop_constraint("uq_decision_commitments_run_sha", "decision_commitments", type_="unique")
    op.drop_column("committee_decisions", "entity_token")
