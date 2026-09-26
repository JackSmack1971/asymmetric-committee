"""Per-verdict tokens and latency are nullable: unknown is not zero.

A cache hit makes no call and a provider may report only ``usage.cost``, so token counts and
latency can be genuinely unknown. Storing 0 would misstate the accounting.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in ("tokens_in", "tokens_out", "latency_ms"):
        op.alter_column("agent_verdicts", column, existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # Unknown values cannot be represented as NOT NULL without inventing a number: refuse if any.
    bind = op.get_bind()
    unknown: int = bind.execute(
        sa.text(
            "SELECT count(*) FROM agent_verdicts "
            "WHERE tokens_in IS NULL OR tokens_out IS NULL OR latency_ms IS NULL"
        )
    ).scalar_one()
    if unknown:
        raise RuntimeError(
            f"{unknown} agent_verdicts rows have unknown telemetry; cannot downgrade"
        )
    for column in ("tokens_in", "tokens_out", "latency_ms"):
        op.alter_column("agent_verdicts", column, existing_type=sa.Integer(), nullable=False)
