"""Corporate actions, action coverage, symbol history, listing evidence, delistings (P6.4).

Revision ID: 0008
Revises: 0007

Every new table is insert-only (``reject_mutation``). ``security_symbols`` is seeded with each
existing security's current ticker (``source = 'seed'``, ``valid_from = 0001-01-01``) so every
historical ``security_id`` reference stays valid and resolvable.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

IMMUTABLE = (
    "security_symbols",
    "corporate_actions",
    "corporate_action_coverage",
    "asset_status_observations",
    "delisting_filings",
    "identity_conflicts",
    "delistings",
)
EVIDENCE = IMMUTABLE[1:]


def _ts(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _ingested() -> sa.Column[Any]:
    return sa.Column(
        "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _sid(name: str = "security_id", *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.Integer(), nullable=nullable)


def _fk(col: str = "security_id") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([col], ["securities.security_id"])


def upgrade() -> None:
    op.create_table(
        "security_symbols",
        _sid(),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        _ts("available_at"),
        _ingested(),
        _fk(),
        sa.PrimaryKeyConstraint("security_id", "symbol", "valid_from"),
    )
    op.create_index("ix_security_symbols_symbol", "security_symbols", ["symbol"])
    op.execute(
        "INSERT INTO security_symbols (security_id, symbol, valid_from, source, source_ref, "
        "available_at) SELECT security_id, ticker, DATE '0001-01-01', 'seed', 'migration_0008', "
        "now() FROM securities"
    )

    op.create_table(
        "corporate_actions",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_action_id", sa.Text(), nullable=False),
        _ts("available_at"),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.Column("withdrawn", sa.Boolean(), nullable=False),
        _sid(),
        sa.Column("subject_symbol", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("interpretation", sa.Text(), nullable=False),
        sa.Column("knowledge_basis", sa.Text(), nullable=False),
        sa.Column("process_date", sa.Date(), nullable=False),
        sa.Column("ex_date", sa.Date()),
        sa.Column("record_date", sa.Date()),
        sa.Column("payable_date", sa.Date()),
        sa.Column("effective_date", sa.Date()),
        sa.Column("old_rate", sa.Double()),
        sa.Column("new_rate", sa.Double()),
        sa.Column("cash_rate", sa.Double()),
        sa.Column("stock_rate", sa.Double()),
        sa.Column("acquirer_symbol", sa.Text()),
        _sid("acquirer_security_id", nullable=True),
        sa.Column("new_symbol", sa.Text()),
        sa.Column("currency", sa.Text()),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _ingested(),
        _fk(),
        _fk("acquirer_security_id"),
        sa.PrimaryKeyConstraint("provider", "provider_action_id", "available_at"),
    )
    op.create_index(
        "ix_corporate_actions_security_process",
        "corporate_actions",
        ["security_id", "process_date"],
    )
    op.create_index("ix_corporate_actions_available_at", "corporate_actions", ["available_at"])

    op.create_table(
        "corporate_action_coverage",
        sa.Column("provider", sa.Text(), nullable=False),
        _sid(),
        sa.Column("range_start", sa.Date(), nullable=False),
        sa.Column("range_end", sa.Date(), nullable=False),
        _ts("established_at"),
        sa.Column("date_filter", sa.Text(), nullable=False),
        sa.Column("symbols", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("action_types", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("action_count", sa.Integer(), nullable=False),
        sa.Column("actions_sha256", sa.Text(), nullable=False),
        sa.Column("knowledge_basis", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=False),
        _ingested(),
        sa.CheckConstraint("data_quality = 'complete'", name="ck_action_coverage_complete"),
        sa.CheckConstraint("range_end >= range_start", name="ck_action_coverage_range"),
        _fk(),
        sa.PrimaryKeyConstraint(
            "provider", "security_id", "range_start", "range_end", "established_at"
        ),
    )

    op.create_table(
        "asset_status_observations",
        _sid(),
        _ts("observed_at"),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("tradable", sa.Boolean()),
        sa.Column("source_version", sa.Text(), nullable=False),
        _ingested(),
        _fk(),
        sa.PrimaryKeyConstraint("security_id", "observed_at"),
    )

    op.create_table(
        "delisting_filings",
        sa.Column("cik", sa.BigInteger(), nullable=False),
        sa.Column("accession", sa.Text(), nullable=False),
        sa.Column("form", sa.Text(), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("earliest_effective_date", sa.Date(), nullable=False),
        sa.Column("rule_provision", sa.Text()),
        sa.Column("stated_effective_date", sa.Date()),
        _ts("available_at"),
        _ingested(),
        sa.PrimaryKeyConstraint("cik", "accession"),
    )

    op.create_table(
        "identity_conflicts",
        _sid(),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        _sid("holder_security_id"),
        _ts("detected_at"),
        _ingested(),
        _fk(),
        _fk("holder_security_id"),
        sa.PrimaryKeyConstraint("security_id", "symbol", "valid_from", "source_ref"),
    )

    op.create_table(
        "delistings",
        _sid(),
        _ts("available_at"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("last_trade_date", sa.Date()),
        sa.Column("terminal_return", sa.Double()),
        sa.Column("terminal_return_source", sa.Text()),
        sa.Column("cash_per_share", sa.Double()),
        _sid("acquirer_security_id", nullable=True),
        sa.Column("acquirer_symbol", sa.Text()),
        sa.Column("acquirer_rate", sa.Double()),
        sa.Column("evidence", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("knowledge_basis", sa.Text(), nullable=False),
        sa.Column("derivation_version", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=False),
        _ingested(),
        sa.CheckConstraint(
            "(status = 'delisted' AND terminal_return_source IS NOT NULL) "
            "OR (status <> 'delisted' AND terminal_return_source IS NULL "
            "AND terminal_return IS NULL)",
            name="ck_delistings_source",
        ),
        sa.CheckConstraint(
            "terminal_return_source IS DISTINCT FROM 'default' OR terminal_return = -0.3",
            name="ck_delistings_default",
        ),
        _fk(),
        _fk("acquirer_security_id"),
        sa.PrimaryKeyConstraint("security_id", "available_at"),
    )

    for table in IMMUTABLE:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_mutation()"
        )


def downgrade() -> None:
    bind = op.get_bind()
    renamed: int = bind.execute(
        sa.text("SELECT count(*) FROM security_symbols WHERE source <> 'seed'")
    ).scalar_one()
    if renamed:
        raise RuntimeError(f"{renamed} symbol-continuity rows exist; dropping them is manual")
    for table in EVIDENCE:
        n: int = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        if n:
            raise RuntimeError(
                f"{table} holds {n} evidence rows; dropping them is a manual decision"
            )
    for table in reversed(IMMUTABLE):
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
        op.drop_table(table)
