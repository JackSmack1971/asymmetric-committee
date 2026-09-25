"""Invariant 2: only store.as_of reads fact tables. import-linter enforces it in `make lint`."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LINT = shutil.which("lint-imports")
DOWNSTREAM = {"agents", "features", "gate", "committee", "risk", "evaluation", "universe"}


def _contracts() -> list[dict[str, list[str]]]:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    contracts: list[dict[str, list[str]]] = cfg["tool"]["importlinter"]["contracts"]
    return contracts


def test_contract_covers_downstream_packages() -> None:
    (read_rule, *_) = _contracts()
    assert set(read_rule["source_modules"]) >= DOWNSTREAM
    assert {"sqlalchemy", "psycopg", "store._tables"} <= set(read_rule["forbidden_modules"])


def _lint(cwd: Path) -> subprocess.CompletedProcess[str]:
    assert LINT is not None
    return subprocess.run([LINT], cwd=cwd, capture_output=True, text=True, check=False)


@pytest.mark.skipif(LINT is None, reason="import-linter not installed")
def test_repo_keeps_the_contracts() -> None:
    result = _lint(ROOT)
    assert result.returncode == 0, result.stdout


@pytest.mark.skipif(LINT is None, reason="import-linter not installed")
@pytest.mark.parametrize("bad_import", ["import sqlalchemy", "from store import _tables"])
def test_a_raw_db_import_in_risk_is_rejected(tmp_path: Path, bad_import: str) -> None:
    shutil.copy(ROOT / "pyproject.toml", tmp_path)
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    for pkg in cfg["tool"]["importlinter"]["root_packages"]:
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "__init__.py").write_text("")
    (tmp_path / "store" / "_tables.py").write_text("")
    (tmp_path / "risk" / "sizing.py").write_text(bad_import + "\n")
    result = _lint(tmp_path)
    assert result.returncode != 0
    assert "BROKEN" in result.stdout
