"""P6.5 append-only revisioned outcomes and action query scope.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outcomes", sa.Column("revision_id", sa.BigInteger(), sa.Identity()))
    op.execute("UPDATE outcomes SET revision_id = DEFAULT WHERE revision_id IS NULL")
    op.drop_constraint("uq_outcomes", "outcomes", type_="unique")
    op.create_primary_key("pk_outcomes", "outcomes", ["revision_id"])
    op.add_column("outcomes", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE outcomes SET resolved_at = scored_at")
    op.alter_column("outcomes", "resolved_at", nullable=False)
    op.alter_column("outcomes", "sector_fwd_return", nullable=False)
    op.add_column(
        "outcomes",
        sa.Column("completeness", sa.Text(), nullable=False, server_default="COMPLETE"),
    )
    op.add_column("outcomes", sa.Column("evidence_revision_sha256", sa.Text(), nullable=True))
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT revision_id, run_id, security_id, horizon, fwd_return, "
            "sector_fwd_return, scored_at FROM outcomes"
        )
    ).mappings()
    for row in rows:
        material = "|".join(str(row[k]) for k in row)
        digest = hashlib.sha256(material.encode()).hexdigest()
        conn.execute(
            sa.text("UPDATE outcomes SET evidence_revision_sha256=:h WHERE revision_id=:id"),
            {"h": digest, "id": row["revision_id"]},
        )
    op.alter_column("outcomes", "evidence_revision_sha256", nullable=False)
    op.create_check_constraint(
        "ck_outcomes_completeness", "outcomes", "completeness IN ('COMPLETE', 'HALTED')"
    )
    op.create_check_constraint("ck_outcomes_horizon", "outcomes", "horizon IN (5, 21, 63)")
    op.create_check_constraint(
        "ck_outcomes_returns", "outcomes", "fwd_return >= -1 AND sector_fwd_return >= -1"
    )
    op.create_unique_constraint(
        "uq_outcomes_revision",
        "outcomes",
        ["run_id", "security_id", "horizon", "evidence_revision_sha256"],
    )
    op.execute(
        "CREATE TRIGGER outcomes_immutable BEFORE UPDATE OR DELETE ON outcomes "
        "FOR EACH ROW EXECUTE FUNCTION reject_mutation()"
    )
    op.execute(
        """CREATE FUNCTION guard_outcome_insert() RETURNS trigger AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM runs r
            JOIN decision_commitments c ON c.run_id = r.run_id
            JOIN commitment_anchors a ON a.run_id = c.run_id AND a.sha256 = c.sha256
            WHERE r.run_id = NEW.run_id
              AND a.ots_proof IS NOT NULL AND a.git_commit IS NOT NULL
              AND a.verified_at IS NOT NULL AND NEW.scored_at >= a.verified_at
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
            RAISE EXCEPTION 'outcome admission evidence or status is invalid';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql"""
    )
    op.execute(
        "CREATE TRIGGER outcomes_admission_guard BEFORE INSERT ON outcomes "
        "FOR EACH ROW EXECUTE FUNCTION guard_outcome_insert()"
    )

    op.add_column(
        "corporate_action_coverage",
        sa.Column("pagination_exhausted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "corporate_action_coverage",
        sa.Column("full_history", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "corporate_action_coverage", sa.Column("provider_lower_bound", sa.Date(), nullable=True)
    )
    op.create_check_constraint(
        "ck_action_coverage_full_history",
        "corporate_action_coverage",
        "NOT full_history OR (pagination_exhausted AND provider_lower_bound IS NOT NULL)",
    )
    op.execute(
        """CREATE FUNCTION guard_scored_run() RETURNS trigger AS $$
        BEGIN
          IF NEW.status = 'SCORED' AND OLD.status <> 'SCORED' THEN
            IF NOT ((OLD.mode = 'live' AND OLD.status = 'EXECUTED') OR
                    (OLD.mode IN ('backtest', 'ablation') AND OLD.status = 'ANCHORED')) THEN
              RAISE EXCEPTION 'only complete anchored/executed runs may be scored';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM decision_commitments c JOIN commitment_anchors a
                           ON a.run_id=c.run_id AND a.sha256=c.sha256
                           WHERE c.run_id=NEW.run_id AND a.ots_proof IS NOT NULL
                             AND a.git_commit IS NOT NULL AND a.verified_at IS NOT NULL) THEN
              RAISE EXCEPTION 'scoring requires a matching commitment and anchor';
            END IF;
            IF EXISTS (SELECT 1 FROM outcomes o JOIN commitment_anchors a
                       ON a.run_id=o.run_id WHERE o.run_id=NEW.run_id
                         AND o.scored_at < a.verified_at) THEN
              RAISE EXCEPTION 'outcome scoring must follow anchor verification';
            END IF;
            IF EXISTS (SELECT 1 FROM outcomes o WHERE o.run_id=NEW.run_id
                       AND o.completeness <> 'COMPLETE') OR NOT EXISTS (
              SELECT 1 FROM outcomes o WHERE o.run_id=NEW.run_id GROUP BY o.security_id
              HAVING count(DISTINCT o.horizon) = 3 AND
                     bool_and(o.horizon IN (5,21,63))
            ) THEN
              RAISE EXCEPTION 'scoring requires complete 5/21/63 outcomes';
            END IF;
            IF EXISTS (
              SELECT 1 FROM (SELECT DISTINCT security_id FROM committee_decisions
                             WHERE run_id=NEW.run_id) expected
              WHERE NOT EXISTS (
                SELECT 1 FROM outcomes o WHERE o.run_id=NEW.run_id
                  AND o.security_id=expected.security_id
                GROUP BY o.security_id
                HAVING count(DISTINCT o.horizon)=3
              )
            ) THEN
              RAISE EXCEPTION 'scoring outcomes omit a committed security';
            END IF;
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql"""
    )
    op.execute(
        "CREATE TRIGGER runs_scored_guard BEFORE UPDATE OF status ON runs "
        "FOR EACH ROW EXECUTE FUNCTION guard_scored_run()"
    )


def downgrade() -> None:
    conn = op.get_bind()
    outcome_rows: int = conn.execute(sa.text("SELECT count(*) FROM outcomes")).scalar_one()
    if outcome_rows:
        raise RuntimeError(
            "outcome evidence exists; dropping revision metadata requires a manual decision"
        )
    scoped_rows: int = conn.execute(
        sa.text(
            "SELECT count(*) FROM corporate_action_coverage WHERE full_history "
            "OR pagination_exhausted OR provider_lower_bound IS NOT NULL"
        )
    ).scalar_one()
    if scoped_rows:
        raise RuntimeError(
            "action query-scope evidence exists; downgrade requires a manual decision"
        )
    op.drop_constraint("ck_action_coverage_full_history", "corporate_action_coverage")
    op.drop_column("corporate_action_coverage", "provider_lower_bound")
    op.drop_column("corporate_action_coverage", "full_history")
    op.drop_column("corporate_action_coverage", "pagination_exhausted")
    op.execute("DROP TRIGGER outcomes_immutable ON outcomes")
    op.execute("DROP TRIGGER outcomes_admission_guard ON outcomes")
    op.execute("DROP FUNCTION guard_outcome_insert()")
    op.execute("DROP TRIGGER runs_scored_guard ON runs")
    op.execute("DROP FUNCTION guard_scored_run()")
    op.drop_constraint("uq_outcomes_revision", "outcomes", type_="unique")
    op.drop_constraint("ck_outcomes_completeness", "outcomes", type_="check")
    op.drop_constraint("ck_outcomes_horizon", "outcomes", type_="check")
    op.drop_constraint("ck_outcomes_returns", "outcomes", type_="check")
    op.alter_column("outcomes", "sector_fwd_return", nullable=True)
    op.drop_constraint("pk_outcomes", "outcomes", type_="primary")
    op.drop_column("outcomes", "resolved_at")
    op.drop_column("outcomes", "evidence_revision_sha256")
    op.drop_column("outcomes", "completeness")
    op.drop_column("outcomes", "revision_id")
    op.create_unique_constraint("uq_outcomes", "outcomes", ["run_id", "security_id", "horizon"])
