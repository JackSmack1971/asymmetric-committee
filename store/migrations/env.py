"""Alembic environment. The URL comes from the config attribute set by ``store.migrate`` or from
``DATABASE_URL``."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from store._tables import metadata

url = context.config.get_main_option("sqlalchemy.url") or os.environ["DATABASE_URL"]
engine = create_engine(url)
with engine.begin() as conn:
    context.configure(connection=conn, target_metadata=metadata, compare_type=True)
    context.run_migrations()
engine.dispose()
