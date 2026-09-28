"""Retain gate and feature version fields for complete P6.6 trial identity.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("gate_decisions", sa.Column("feature_set_version", sa.Text(), nullable=True))
    op.add_column("gate_decisions", sa.Column("gate_model_version", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("gate_decisions", "gate_model_version")
    op.drop_column("gate_decisions", "feature_set_version")
