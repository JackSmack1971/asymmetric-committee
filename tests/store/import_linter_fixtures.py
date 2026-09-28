"""Build synthetic source trees from the repository's configured import-linter contracts."""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def configured_import_linter() -> tuple[list[str], list[str]]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lint_config = config["tool"]["importlinter"]
    root_packages: list[str] = lint_config["root_packages"]
    modules = {
        module
        for contract in lint_config["contracts"]
        for module in [*contract.get("source_modules", []), *contract.get("forbidden_modules", [])]
        if module.split(".", maxsplit=1)[0] in root_packages
    }
    return root_packages, sorted(modules)


def _write_module(tree: Path, module: str) -> None:
    parts = module.split(".")
    package = tree.joinpath(*parts[:-1])
    package.mkdir(parents=True, exist_ok=True)
    for parent in [tree.joinpath(*parts[:index]) for index in range(1, len(parts))]:
        parent.mkdir(parents=True, exist_ok=True)
        (parent / "__init__.py").touch()
    (tree.joinpath(*parts).with_suffix(".py")).write_text("", encoding="utf-8")


def make_import_linter_tree(destination: Path, files: dict[str, str]) -> None:
    """Populate configured packages/modules, then overlay scenario-specific source files."""
    shutil.copy(ROOT / "pyproject.toml", destination)
    root_packages, modules = configured_import_linter()
    for package in root_packages:
        package_path = destination / package
        package_path.mkdir(parents=True, exist_ok=True)
        (package_path / "__init__.py").touch()
    for module in modules:
        _write_module(destination, module)
    for name, body in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
