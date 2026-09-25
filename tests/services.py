"""Throwaway Postgres and Redis for tests.

``TEST_DATABASE_URL`` / ``TEST_REDIS_URL`` point at existing servers (CI, compose). Without them we
start local ones from the installed binaries. If neither works the test is skipped, unless
``REQUIRE_SERVICES=1`` (set by ``make gate-P1``), which turns the skip into a failure.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def unavailable(reason: str) -> None:
    if os.environ.get("REQUIRE_SERVICES") == "1":
        pytest.fail(f"required test service unavailable: {reason}")
    pytest.skip(reason)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _pg_bin() -> Path | None:
    for base in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        if (base / "initdb").exists():
            return base
    found = shutil.which("initdb")
    return Path(found).parent if found else None


@contextmanager
def _local_postgres() -> Iterator[str]:
    """initdb + pg_ctl in a temp dir; runs as the ``postgres`` user when we are root."""
    pg = _pg_bin()
    if pg is None:
        unavailable("no TEST_DATABASE_URL and no local Postgres binaries")
        raise AssertionError  # unreachable
    tmp = Path(tempfile.mkdtemp(prefix="pgtest-"))
    prefix: list[str] = []
    if os.geteuid() == 0:
        shutil.chown(tmp, "postgres")
        prefix = ["runuser", "-u", "postgres", "--"]
    port = _free_port()
    data = tmp / "data"
    subprocess.run(
        [*prefix, str(pg / "initdb"), "-D", str(data), "-U", "postgres", "-A", "trust"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            *prefix,
            str(pg / "pg_ctl"),
            "-D",
            str(data),
            "-w",
            "-l",
            str(tmp / "log"),
            "-o",
            f"-p {port} -k {tmp} -c fsync=off",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run(
            [*prefix, str(pg / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            capture_output=True,
        )
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def postgres_server() -> Iterator[str]:
    if url := os.environ.get("TEST_DATABASE_URL"):
        yield url
        return
    with _local_postgres() as url:
        yield url


@contextmanager
def scratch_database(server_url: str) -> Iterator[str]:
    """A fresh database on the server, dropped afterwards."""
    name = f"p1test_{uuid.uuid4().hex[:10]}"
    admin = create_engine(server_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as e:  # server unreachable or no CREATEDB right
        admin.dispose()
        unavailable(f"cannot create a test database: {e}")
    try:
        yield make_url(server_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@contextmanager
def redis_server() -> Iterator[str]:
    if url := os.environ.get("TEST_REDIS_URL"):
        yield url
        return
    exe = shutil.which("redis-server")
    if exe is None:
        unavailable("no TEST_REDIS_URL and no redis-server binary")
    port = _free_port()
    proc = subprocess.Popen(
        [str(exe), "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
    )
    try:
        import redis

        client = redis.Redis(port=port)
        for _ in range(100):
            try:
                client.ping()
                break
            except redis.ConnectionError:
                time.sleep(0.05)
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        proc.terminate()
        proc.wait(5)
