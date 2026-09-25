"""Schema matches §4.2/§4.3; migrations match the table metadata; as-filed rows are immutable."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, Table, inspect, text
from sqlalchemy.exc import DBAPIError

from store import _tables as t
from store.write import insert_fundamentals
from tests.store import factories as f

BITEMPORAL = {"event_time", "available_at", "ingested_at", "source_version"}


def test_migrations_match_metadata(pg_engine: Engine) -> None:
    with pg_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), t.metadata)
    assert diff == []


def test_every_section_4_3_table_exists(pg_engine: Engine) -> None:
    names = set(inspect(pg_engine).get_table_names())
    spec = {
        "securities",
        "universe_snapshots",
        "price_bars",
        "fundamentals_asfiled",
        "insider_txns",
        "news_items",
        "features",
        "runs",
        "gate_decisions",
        "agent_verdicts",
        "committee_decisions",
        "decision_commitments",
        "orders",
        "fills",
        "outcomes",
        "agent_scores",
        "feed_health",
    }
    assert spec <= names


@pytest.mark.parametrize("table", t.FACT_TABLES, ids=lambda x: x.name)
def test_fact_tables_are_bitemporal_and_indexed(pg_engine: Engine, table: Table) -> None:
    insp = inspect(pg_engine)
    cols = {c["name"]: c for c in insp.get_columns(table.name)}
    assert set(cols) >= BITEMPORAL
    assert all(not cols[c]["nullable"] for c in BITEMPORAL)
    indexed = [tuple(ix["column_names"]) for ix in insp.get_indexes(table.name)]
    if "security_id" in cols:
        assert ("security_id", "available_at") in indexed
    else:  # news: many securities per row
        assert ("available_at",) in indexed and ("security_ids",) in indexed


def test_fundamentals_are_insert_only(db: Connection) -> None:
    sid = f.security(db)
    insert_fundamentals(db, [f.fact(sid, available_at=f.T0, accn=f.accession(1))])
    with pytest.raises(DBAPIError, match="insert-only"), db.begin_nested():
        db.execute(text("UPDATE fundamentals_asfiled SET value = 2"))
    with pytest.raises(DBAPIError, match="insert-only"), db.begin_nested():
        db.execute(text("DELETE FROM fundamentals_asfiled"))


def test_reingest_is_a_noop(db: Connection) -> None:
    sid = f.security(db)
    row = f.fact(sid, available_at=f.T0, accn=f.accession(1))
    assert insert_fundamentals(db, [row]) == 1
    changed = row.model_copy(update={"value": 99.0})  # same natural key + accession
    assert insert_fundamentals(db, [changed]) == 0
    assert db.execute(text("SELECT value FROM fundamentals_asfiled")).scalar_one() == 1.0


def test_decision_commitments_are_append_only(db: Connection) -> None:
    db.execute(
        text(
            "INSERT INTO runs VALUES ('00000000-0000-0000-0000-000000000001', 'backtest', "
            ":ts, 'h', 'COMMITTED', :ts, NULL)"
        ),
        {"ts": datetime(2024, 1, 1, tzinfo=UTC)},
    )
    db.execute(
        text(
            "INSERT INTO decision_commitments (run_id, sha256) VALUES "
            "('00000000-0000-0000-0000-000000000001', 'x')"
        )
    )
    with pytest.raises(DBAPIError, match="insert-only"), db.begin_nested():
        db.execute(text("UPDATE decision_commitments SET sha256 = 'y'"))


def test_hypertables(pg_engine: Engine) -> None:
    with pg_engine.connect() as conn:
        has_ts = conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        ).first()
        if has_ts is None:
            if os.environ.get("REQUIRE_TIMESCALE") == "1":
                pytest.fail("TimescaleDB required but not installed")
            pytest.skip("TimescaleDB not installed on this test server")
        names: set[str] = set(
            conn.execute(text("SELECT hypertable_name FROM timescaledb_information.hypertables"))
            .scalars()
            .all()
        )
    assert names == {tb.name for tb in t.HYPERTABLES}
