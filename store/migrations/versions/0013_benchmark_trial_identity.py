"""Persist the P6.6 weekly series identity with every benchmark result.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "benchmark_results",
        sa.Column("trial_identity_sha256", sa.Text(), nullable=True),
    )
    op.execute(
        """UPDATE benchmark_results
           SET trial_identity_sha256 = replay_inputs ->> 'trial_identity_sha256'
           WHERE replay_inputs ? 'trial_identity_sha256'"""
    )
    op.alter_column("benchmark_results", "trial_identity_sha256", nullable=False)
    op.create_index(
        "ix_benchmark_results_trial_week",
        "benchmark_results",
        ["trial_identity_sha256", "week_start"],
    )


def downgrade() -> None:
    count: int = (
        op.get_bind().execute(sa.text("SELECT count(*) FROM benchmark_results")).scalar_one()
    )
    if count:
        raise RuntimeError("benchmark evidence exists; downgrade requires a manual decision")
    op.drop_index("ix_benchmark_results_trial_week", table_name="benchmark_results")
    op.drop_column("benchmark_results", "trial_identity_sha256")
