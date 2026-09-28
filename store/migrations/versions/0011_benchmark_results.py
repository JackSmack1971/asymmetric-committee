"""Append-only weekly benchmark results with replay inputs and admission guard.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "benchmark_results",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("benchmark", sa.Text(), nullable=False),
        sa.Column("variant", sa.Text(), nullable=False),
        sa.Column("gross_return", sa.Float(), nullable=False),
        sa.Column("cost_return", sa.Float(), nullable=False),
        sa.Column("net_return", sa.Float(), nullable=False),
        sa.Column("turnover", sa.Float(), nullable=False),
        sa.Column("outcome_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("commitment_sha256", sa.Text(), nullable=False),
        sa.Column("input_sha256", sa.Text(), nullable=False),
        sa.Column("provenance_sha256", sa.Text(), nullable=False),
        sa.Column("replay_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("random_summary", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("k", sa.Integer()),
        sa.Column("seed_sha256", sa.Text()),
        sa.Column("completeness", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "benchmark IN ('spy', 'exposure_matched_spy', 'equal_weight_universe', "
            "'sector_etf_matched', 'quant_baseline_book', 'random_committee')",
            name="ck_benchmark_results_identity",
        ),
        sa.CheckConstraint(
            "variant IN ('adjusted', 'unadjusted')", name="ck_benchmark_results_variant"
        ),
        sa.CheckConstraint(
            "net_return = gross_return - cost_return", name="ck_benchmark_results_net"
        ),
        sa.CheckConstraint("cost_return >= 0 AND turnover >= 0", name="ck_benchmark_results_cost"),
        sa.CheckConstraint(
            "completeness IN ('COMPLETE', 'HALTED')", name="ck_benchmark_results_completeness"
        ),
        sa.CheckConstraint(
            "(benchmark = 'random_committee' AND k = 1000 AND seed_sha256 IS NOT NULL) OR "
            "(benchmark <> 'random_committee' AND k IS NULL AND seed_sha256 IS NULL)",
            name="ck_benchmark_results_random_replay",
        ),
        sa.CheckConstraint(
            "(benchmark = 'random_committee' AND random_summary IS NOT NULL) OR "
            "(benchmark <> 'random_committee' AND random_summary IS NULL)",
            name="ck_benchmark_results_summary",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
        sa.PrimaryKeyConstraint("run_id", "week_start", "benchmark", "variant"),
    )
    op.execute(
        """CREATE FUNCTION guard_benchmark_result_insert() RETURNS trigger AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM runs r
            JOIN decision_commitments c ON c.run_id = r.run_id
            JOIN commitment_anchors a ON a.run_id = c.run_id AND a.sha256 = c.sha256
            WHERE r.run_id = NEW.run_id AND c.sha256 = NEW.commitment_sha256
              AND a.ots_proof IS NOT NULL AND a.git_commit IS NOT NULL
              AND a.verified_at IS NOT NULL AND NEW.outcome_cutoff >= a.verified_at
              AND (
                (NEW.completeness = 'COMPLETE' AND (
                  (r.mode = 'live' AND r.status IN ('EXECUTED', 'SCORED')) OR
                  (r.mode IN ('backtest', 'ablation') AND r.status IN ('ANCHORED', 'SCORED'))
                )) OR
                (NEW.completeness = 'HALTED' AND r.status = 'PARTIAL' AND EXISTS (
                  SELECT 1 FROM kill_switch_events h WHERE h.run_id = r.run_id
                ))
              )
          ) THEN
            RAISE EXCEPTION 'benchmark result admission evidence or status is invalid';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql"""
    )
    op.execute(
        "CREATE TRIGGER benchmark_results_admission_guard BEFORE INSERT ON benchmark_results "
        "FOR EACH ROW EXECUTE FUNCTION guard_benchmark_result_insert()"
    )
    op.execute(
        "CREATE TRIGGER benchmark_results_immutable BEFORE UPDATE OR DELETE ON benchmark_results "
        "FOR EACH ROW EXECUTE FUNCTION reject_mutation()"
    )


def downgrade() -> None:
    count: int = (
        op.get_bind().execute(sa.text("SELECT count(*) FROM benchmark_results")).scalar_one()
    )
    if count:
        raise RuntimeError("benchmark evidence exists; downgrade requires a manual decision")
    op.execute("DROP TRIGGER benchmark_results_immutable ON benchmark_results")
    op.execute("DROP TRIGGER benchmark_results_admission_guard ON benchmark_results")
    op.execute("DROP FUNCTION guard_benchmark_result_insert()")
    op.drop_table("benchmark_results")
