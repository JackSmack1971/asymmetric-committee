"""Persist P2 proposed books.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proposed_books",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("book", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
        sa.PrimaryKeyConstraint("run_id"),
    )


def downgrade() -> None:
    op.drop_table("proposed_books")
