"""Engine construction. Only orchestration, ingest and tests open connections."""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine


def engine(url: str | None = None) -> Engine:
    return create_engine(url or os.environ["DATABASE_URL"], pool_pre_ping=True)
