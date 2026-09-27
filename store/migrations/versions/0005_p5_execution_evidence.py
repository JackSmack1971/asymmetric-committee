"""Execution evidence on ``orders`` (§4.3, §9): client id, reference price/source, fills, slippage.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_NOT_NULL: tuple[tuple[str, Any], ...] = (
    ("client_order_id", sa.Text()),
    ("kind", sa.Text()),
    ("filled_qty", sa.Double()),
    ("decision_price", sa.Double()),
    ("reference_price", sa.Double()),
    ("reference_source", sa.Text()),
)


def upgrade() -> None:
    # Nothing wrote orders before P5 step 4. Refuse rather than invent evidence for old rows.
    existing: int = op.get_bind().execute(sa.text("SELECT count(*) FROM orders")).scalar_one()
    if existing:
        raise RuntimeError(f"orders has {existing} rows without execution evidence; cannot migrate")
    for name, type_ in _NOT_NULL:
        op.add_column("orders", sa.Column(name, type_, nullable=False))
    op.add_column("orders", sa.Column("fill_price", sa.Double(), nullable=True))
    op.add_column("orders", sa.Column("slippage_bps", sa.Double(), nullable=True))
    op.add_column("orders", sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint("orders_client_order_id_key", "orders", ["client_order_id"])


def downgrade() -> None:
    op.drop_constraint("orders_client_order_id_key", "orders", type_="unique")
    for name in ("filled_at", "slippage_bps", "fill_price"):
        op.drop_column("orders", name)
    for name, _ in reversed(_NOT_NULL):
        op.drop_column("orders", name)
