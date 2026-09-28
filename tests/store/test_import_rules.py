"""Invariant 2: only store.as_of reads fact tables. import-linter enforces it in `make lint`."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.store.import_linter_fixtures import (
    configured_import_linter,
    make_import_linter_tree,
)

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


def test_synthetic_tree_covers_all_configured_local_contract_modules(tmp_path: Path) -> None:
    make_import_linter_tree(tmp_path, {})
    root_packages, modules = configured_import_linter()
    assert all((tmp_path / package / "__init__.py").is_file() for package in root_packages)
    assert all(
        tmp_path.joinpath(*module.split(".")).with_suffix(".py").is_file() for module in modules
    )


@pytest.mark.skipif(LINT is None, reason="import-linter not installed")
@pytest.mark.parametrize("bad_import", ["import sqlalchemy", "from store import _tables"])
def test_a_raw_db_import_in_risk_is_rejected(tmp_path: Path, bad_import: str) -> None:
    shutil.copy(ROOT / "pyproject.toml", tmp_path)
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    for pkg in cfg["tool"]["importlinter"]["root_packages"]:
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "__init__.py").write_text("")
    (tmp_path / "store" / "_tables.py").write_text("")
    (tmp_path / "evaluation" / "scorable.py").write_text("")
    (tmp_path / "evaluation" / "anchoring.py").write_text("")
    (tmp_path / "evaluation" / "calendar_rules.py").write_text("")
    (tmp_path / "evaluation" / "tbill_rules.py").write_text("")
    (tmp_path / "risk" / "sizing.py").write_text(bad_import + "\n")
    result = _lint(tmp_path)
    assert result.returncode != 0
    assert "BROKEN" in result.stdout


@pytest.mark.skipif(LINT is None, reason="import-linter not installed")
def test_sink_is_the_only_orchestration_writer(tmp_path: Path) -> None:
    make_import_linter_tree(tmp_path, {"orchestration/sink.py": "from store import write\n"})
    assert _lint(tmp_path).returncode == 0


@pytest.mark.skipif(LINT is None, reason="import-linter not installed")
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("orchestration/pipeline.py", "from store import write\n"),
        ("execution/alpaca.py", "import sqlalchemy\n"),
        ("execution/alpaca.py", "from store import write\n"),
    ],
)
def test_other_modules_cannot_write_or_open_the_db(tmp_path: Path, path: str, body: str) -> None:
    make_import_linter_tree(tmp_path, {path: body})
    result = _lint(tmp_path)
    assert result.returncode != 0
    assert "BROKEN" in result.stdout


@pytest.mark.skipif(LINT is None, reason="import-linter not installed")
@pytest.mark.parametrize(
    "body",
    [
        "import sqlalchemy\n",
        "from store import as_of\n",
        "from store import scoring_read\n",
        "from orchestration import sink\n",
    ],
)
def test_the_scorability_gate_stays_pure(tmp_path: Path, body: str) -> None:
    make_import_linter_tree(
        tmp_path,
        {
            "evaluation/scorable.py": body,
            "store/as_of.py": "",
            "store/scoring_read.py": "",
            "orchestration/sink.py": "",
        },
    )
    result = _lint(tmp_path)
    assert result.returncode != 0
    assert "BROKEN" in result.stdout
