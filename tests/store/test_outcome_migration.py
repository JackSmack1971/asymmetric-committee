from __future__ import annotations

from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from store.migrate import alembic_config
from tests.services import scratch_database


def test_migration_0009_up_down_up(pg_engine: Engine) -> None:
    server = pg_engine.url.set(database="postgres").render_as_string(hide_password=False)
    with scratch_database(server) as url:
        cfg = alembic_config(url)
        command.upgrade(cfg, "0008")
        command.upgrade(cfg, "head")
        eng = create_engine(url)
        with eng.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('sic_history')")).scalar_one()
                == "sic_history"
            )
            outcome_columns = {
                row["column_name"]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='outcomes'"
                    )
                ).mappings()
            }
        assert {
            "revision_id",
            "resolved_at",
            "completeness",
            "evidence_revision_sha256",
        } <= outcome_columns
        command.downgrade(cfg, "0008")
        with eng.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('sic_history')")).scalar_one() is None
            assert (
                conn.execute(text("SELECT to_regprocedure('guard_scored_run()')")).scalar() is None
            )
        command.upgrade(cfg, "head")
        with eng.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('outcomes')")).scalar() is not None
            assert conn.execute(text("SELECT to_regclass('sic_history')")).scalar() is not None
        eng.dispose()
