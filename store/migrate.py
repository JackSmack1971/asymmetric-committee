"""Apply Alembic migrations from code (``python -m store.migrate``)."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

INI = Path(__file__).with_name("alembic.ini")


def alembic_config(url: str) -> Config:
    cfg = Config(str(INI))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url or os.environ["DATABASE_URL"]), revision)


if __name__ == "__main__":
    upgrade()
