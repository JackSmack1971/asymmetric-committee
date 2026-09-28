from __future__ import annotations

from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from store.migrate import alembic_config
from tests.services import scratch_database


def test_migration_0011_up_down_up(pg_engine: Engine) -> None:
    server = pg_engine.url.set(database="postgres").render_as_string(hide_password=False)
    with scratch_database(server) as url:
        cfg = alembic_config(url)
        command.upgrade(cfg, "0010")
        command.upgrade(cfg, "head")
        eng = create_engine(url)
        with eng.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('benchmark_results')")).scalar_one() == (
                "benchmark_results"
            )
            columns = {
                row["column_name"]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='benchmark_results'"
                    )
                ).mappings()
            }
        assert {
            "benchmark",
            "variant",
            "commitment_sha256",
            "replay_inputs",
            "input_sha256",
            "provenance_sha256",
            "seed_sha256",
            "random_summary",
        } <= columns
        command.downgrade(cfg, "0010")
        with eng.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('benchmark_results')")).scalar_one() is None
            )
        command.upgrade(cfg, "head")
        with eng.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('benchmark_results')")).scalar_one() == (
                "benchmark_results"
            )
        eng.dispose()


def test_migration_0012_period_references_up_down_up(pg_engine: Engine) -> None:
    server = pg_engine.url.set(database="postgres").render_as_string(hide_password=False)
    with scratch_database(server) as url:
        cfg = alembic_config(url)
        command.upgrade(cfg, "0011")
        command.upgrade(cfg, "head")
        eng = create_engine(url)
        with eng.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('benchmark_period_references')")).scalar_one()
                == "benchmark_period_references"
            )
            columns = {
                row["column_name"]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='benchmark_period_references'"
                    )
                ).mappings()
            }
        assert {
            "run_id",
            "symbol_ref",
            "period_start",
            "session_date",
            "available_at",
            "source_version",
        } <= columns
        command.downgrade(cfg, "0011")
        with eng.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('benchmark_period_references')")).scalar_one()
                is None
            )
        command.upgrade(cfg, "head")
        with eng.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('benchmark_period_references')")).scalar_one()
                == "benchmark_period_references"
            )
        eng.dispose()


def test_migration_0013_trial_identity_up_down_up(pg_engine: Engine) -> None:
    server = pg_engine.url.set(database="postgres").render_as_string(hide_password=False)
    with scratch_database(server) as url:
        cfg = alembic_config(url)
        command.upgrade(cfg, "0012")
        command.upgrade(cfg, "head")
        eng = create_engine(url)
        with eng.connect() as conn:
            columns = {
                row["column_name"]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='benchmark_results'"
                    )
                ).mappings()
            }
            index: str | None = conn.execute(
                text("SELECT to_regclass('ix_benchmark_results_trial_week')")
            ).scalar_one()
        assert "trial_identity_sha256" in columns
        assert index == "ix_benchmark_results_trial_week"
        command.downgrade(cfg, "0012")
        with eng.connect() as conn:
            columns_after = {
                row["column_name"]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='benchmark_results'"
                    )
                ).mappings()
            }
        assert "trial_identity_sha256" not in columns_after
        command.upgrade(cfg, "head")
        with eng.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT to_regclass('ix_benchmark_results_trial_week')")
                ).scalar_one()
                == "ix_benchmark_results_trial_week"
            )
        eng.dispose()


def test_migration_0014_gate_identity_versions_up_down_up(pg_engine: Engine) -> None:
    server = pg_engine.url.set(database="postgres").render_as_string(hide_password=False)
    with scratch_database(server) as url:
        cfg = alembic_config(url)
        command.upgrade(cfg, "0013")
        command.upgrade(cfg, "head")
        eng = create_engine(url)
        with eng.connect() as conn:
            columns = {
                row["column_name"]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='gate_decisions'"
                    )
                ).mappings()
            }
        assert {"feature_set_version", "gate_model_version"} <= columns
        command.downgrade(cfg, "0013")
        with eng.connect() as conn:
            columns_after = {
                row["column_name"]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='gate_decisions'"
                    )
                ).mappings()
            }
        assert not {"feature_set_version", "gate_model_version"} & columns_after
        command.upgrade(cfg, "head")
        with eng.connect() as conn:
            columns_final = {
                row["column_name"]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='gate_decisions'"
                    )
                ).mappings()
            }
        assert {"feature_set_version", "gate_model_version"} <= columns_final
        eng.dispose()


def test_migration_0015_replay_context_up_down_up(pg_engine: Engine) -> None:
    server = pg_engine.url.set(database="postgres").render_as_string(hide_password=False)
    with scratch_database(server) as url:
        cfg = alembic_config(url)
        command.upgrade(cfg, "0014")
        command.upgrade(cfg, "head")
        eng = create_engine(url)
        with eng.connect() as conn:
            columns = {
                row["column_name"]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='benchmark_replay_contexts'"
                    )
                ).mappings()
            }
            triggers: set[str] = set(
                conn.execute(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE tgname IN "
                        "('benchmark_replay_contexts_immutable', "
                        "'benchmark_replay_contexts_require_commitment')"
                    )
                ).scalars()
            )
        assert {"run_id", "replay_context", "evidence_sha256"} <= columns
        assert triggers == {
            "benchmark_replay_contexts_immutable",
            "benchmark_replay_contexts_require_commitment",
        }
        command.downgrade(cfg, "0014")
        with eng.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('benchmark_replay_contexts')")).scalar_one()
                is None
            )
        command.upgrade(cfg, "head")
        with eng.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('benchmark_replay_contexts')")).scalar_one()
                == "benchmark_replay_contexts"
            )
        eng.dispose()
