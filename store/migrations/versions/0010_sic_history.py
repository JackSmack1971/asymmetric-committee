"""Point-in-time SIC observations for sector-relative outcomes.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sic_history",
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("sic", sa.Integer(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.CheckConstraint("sic BETWEEN 0 AND 9999", name="ck_sic_history_range"),
        sa.ForeignKeyConstraint(["security_id"], ["securities.security_id"]),
        sa.UniqueConstraint("security_id", "event_time", "source_version", name="uq_sic_history"),
    )
    op.create_index("ix_sic_history_sid_avail", "sic_history", ["security_id", "available_at"])
    op.execute(
        "CREATE TRIGGER sic_history_immutable BEFORE UPDATE OR DELETE ON sic_history "
        "FOR EACH ROW EXECUTE FUNCTION reject_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER sic_history_immutable ON sic_history")
    op.drop_index("ix_sic_history_sid_avail", table_name="sic_history")
    op.drop_table("sic_history")
