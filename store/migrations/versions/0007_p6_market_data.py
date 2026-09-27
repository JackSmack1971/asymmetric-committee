"""Market-data foundations (P6.3): reference instruments, calendar, DGS3MO vintages, references.

Revision ID: 0007
Revises: 0006

Every new table is insert-only (``reject_mutation``). ``securities.cik`` becomes nullable only for
``kind = 'etf'`` rows (SPY and the sector ETFs); equities still require a real CIK.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

IMMUTABLE = (
    "trading_calendar",
    "calendar_coverage",
    "tbill_rates",
    "tbill_vintage_coverage",
    "execution_references",
    "halt_reference_requests",
    "halt_reference_symbol_sets",
    "halt_references",
)
_EXEC_STATUS_CHECK = (
    "(status = 'resolved' AND price IS NOT NULL AND source IS NOT NULL AND reason IS NULL "
    "AND ref_time IS NOT NULL AND session_date IS NOT NULL) "
    "OR (status = 'unresolved' AND price IS NULL AND source IS NULL AND reason IS NOT NULL "
    "AND ((ref_time IS NOT NULL AND session_date IS NOT NULL) "
    "OR reason = 'calendar_uncovered'))"
)
_STATUS_CHECK = (
    "(status = 'resolved' AND price IS NOT NULL AND source IS NOT NULL AND reason IS NULL) "
    "OR (status = 'unresolved' AND price IS NULL AND source IS NULL AND reason IS NOT NULL)"
)


def _ts(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _ingested() -> sa.Column[Any]:
    return sa.Column(
        "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    op.add_column(
        "securities", sa.Column("kind", sa.Text(), nullable=False, server_default="equity")
    )
    op.alter_column("securities", "cik", existing_type=sa.BigInteger(), nullable=True)
    op.create_check_constraint(
        "ck_securities_kind_cik",
        "securities",
        "(kind = 'equity' AND cik IS NOT NULL) OR (kind = 'etf' AND cik IS NULL)",
    )
    op.create_index(
        "uq_securities_reference_ticker",
        "securities",
        ["ticker"],
        unique=True,
        postgresql_where=sa.text("kind <> 'equity'"),
    )

    op.create_table(
        "trading_calendar",
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=False),
        _ts("open_at"),
        _ts("close_at"),
        _ts("event_time"),
        _ts("available_at"),
        _ingested(),
        sa.PrimaryKeyConstraint("session_date", "source_version"),
    )
    op.create_index("ix_trading_calendar_available_at", "trading_calendar", ["available_at"])

    op.create_table(
        "calendar_coverage",
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("range_start", sa.Date(), nullable=False),
        sa.Column("range_end", sa.Date(), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=False),
        _ts("available_at"),
        _ingested(),
        sa.Column("session_count", sa.Integer(), nullable=False),
        sa.Column("sessions_sha256", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("source", "range_start", "range_end", "source_version"),
    )
    op.create_index("ix_calendar_coverage_available_at", "calendar_coverage", ["available_at"])

    op.create_table(
        "tbill_rates",
        sa.Column("series", sa.Text(), nullable=False),
        sa.Column("observation_date", sa.Date(), nullable=False),
        sa.Column("vintage_date", sa.Date(), nullable=False),
        sa.Column("yield_pct", sa.Double(), nullable=False),
        _ingested(),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("series", "observation_date", "vintage_date"),
    )

    op.create_table(
        "tbill_vintage_coverage",
        sa.Column("series", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=False),
        _ts("established_at"),
        sa.Column("earliest_vintage", sa.Date(), nullable=False),
        sa.Column("latest_vintage", sa.Date(), nullable=False),
        sa.Column("vintage_count", sa.Integer(), nullable=False),
        sa.Column("vintage_dates_sha256", sa.Text(), nullable=False),
        _ingested(),
        sa.PrimaryKeyConstraint("series", "source_version"),
    )

    op.create_table(
        "execution_references",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("symbol_ref", sa.Text(), nullable=False),
        _ts("ref_time", nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("price", sa.Double()),
        sa.Column("source", sa.Text()),
        _ts("trade_time", nullable=True),
        sa.Column("trade_tape", sa.Text()),
        sa.Column(
            "trade_conditions", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("trade_id", sa.Text()),
        sa.Column("session_date", sa.Date(), nullable=True),
        _ts("available_at"),
        _ingested(),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.CheckConstraint(_EXEC_STATUS_CHECK, name="ck_execution_references_status"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
        sa.PrimaryKeyConstraint("run_id", "symbol_ref"),
    )

    op.create_table(
        "halt_reference_requests",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        _ts("tau"),
        sa.Column("status", sa.Text(), nullable=False),
        _ts("requested_at"),
        _ingested(),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.CheckConstraint("status = 'symbols_pending'", name="ck_halt_reference_requests_status"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
        sa.PrimaryKeyConstraint("run_id", "trigger"),
    )

    op.create_table(
        "halt_reference_symbol_sets",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("symbols", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("symbol_count", sa.Integer(), nullable=False),
        sa.Column("symbols_sha256", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=False),
        _ts("resolved_at"),
        _ingested(),
        sa.CheckConstraint("symbol_count > 0", name="ck_halt_symbol_sets_nonempty"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
        sa.PrimaryKeyConstraint("run_id", "trigger"),
    )

    op.create_table(
        "halt_references",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("symbol_ref", sa.Text(), nullable=False),
        _ts("observed_at"),
        sa.Column("lag_seconds", sa.Double(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("price", sa.Double()),
        sa.Column("source", sa.Text()),
        _ts("trade_time", nullable=True),
        sa.Column("trade_tape", sa.Text()),
        sa.Column(
            "trade_conditions", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("trade_id", sa.Text()),
        sa.Column("symbol_set_source", sa.Text(), nullable=False),
        sa.Column("symbol_set_version", sa.Text(), nullable=False),
        _ts("available_at"),
        _ingested(),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.CheckConstraint(_STATUS_CHECK, name="ck_halt_references_status"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
        sa.PrimaryKeyConstraint("run_id", "trigger", "symbol_ref"),
    )

    for table in IMMUTABLE:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_mutation()"
        )


def downgrade() -> None:
    etfs: int = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM securities WHERE kind <> 'equity'"))
        .scalar_one()
    )
    if etfs:
        raise RuntimeError(
            f"{etfs} reference instruments exist; deleting them is a manual decision"
        )
    for table in reversed(IMMUTABLE):
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
        op.drop_table(table)
    op.drop_index("uq_securities_reference_ticker", table_name="securities")
    op.drop_constraint("ck_securities_kind_cik", "securities", type_="check")
    op.alter_column("securities", "cik", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("securities", "kind")
