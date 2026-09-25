from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
import redis
from sqlalchemy import Connection, Engine, create_engine, text

from tests.services import postgres_server, redis_server, scratch_database, unavailable

FACT_AND_REF_TABLES = (
    "price_bars, fundamentals_asfiled, insider_txns, news_items, features, universe_snapshots, "
    "feed_health, securities"
)


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    """A migrated scratch database for the whole session."""
    from store.migrate import upgrade

    os.environ.setdefault("STORE_ALLOW_PLAIN_POSTGRES", "1")
    with postgres_server() as server, scratch_database(server) as url:
        upgrade(url)
        engine = create_engine(url)
        yield engine
        engine.dispose()


def reset(conn: Connection) -> None:
    conn.execute(text(f"TRUNCATE {FACT_AND_REF_TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db(pg_engine: Engine) -> Iterator[Connection]:
    """An empty schema. Whatever the test does is rolled back afterwards."""
    with pg_engine.connect() as conn:
        reset(conn)
        conn.commit()
        yield conn
        conn.rollback()


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    with redis_server() as url:
        try:
            redis.Redis.from_url(url).ping()
        except redis.RedisError as e:
            unavailable(f"redis unreachable: {e}")
        yield url


@pytest.fixture
def redis_client(redis_url: str) -> Iterator[redis.Redis]:
    client = redis.Redis.from_url(redis_url)
    client.flushdb()
    yield client
    client.close()
