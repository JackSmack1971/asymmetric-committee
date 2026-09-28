#!/usr/bin/env python3
"""Read-only structural preflight for point-in-time persistence work."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


def cmd(repo: Path, *argv: str) -> dict[str, object]:
    try:
        cp = subprocess.run(argv, cwd=repo, text=True, capture_output=True, check=False)
        return {
            "command": " ".join(argv),
            "returncode": cp.returncode,
            "stdout": cp.stdout.strip(),
            "stderr": cp.stderr.strip(),
        }
    except OSError as exc:
        return {
            "command": " ".join(argv),
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
        }


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def has(src: str, pattern: str) -> bool:
    return bool(re.search(pattern, src, re.M))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", help="Repository root (default: cwd)")
    ns = parser.parse_args()
    repo = Path(ns.repo).resolve()

    paths = {
        "contracts_data": repo / "contracts" / "data.py",
        "as_of": repo / "store" / "as_of.py",
        "write": repo / "store" / "write.py",
        "tables": repo / "store" / "_tables.py",
        "migrate": repo / "store" / "migrate.py",
        "migrations": repo / "store" / "migrations" / "versions",
        "test_as_of": repo / "tests" / "store" / "test_as_of.py",
        "test_schema": repo / "tests" / "store" / "test_schema.py",
        "test_import_rules": repo / "tests" / "store" / "test_import_rules.py",
        "pyproject": repo / "pyproject.toml",
    }

    asof = text(paths["as_of"])
    imports = text(paths["test_import_rules"])
    project = text(paths["pyproject"])

    temporal_contract = (
        has(asof, r"available_at\s*<=\s*as_of")
        and has(asof, r"source_version")
        and has(asof, r"def\s+_latest\s*\(")
    )
    import_boundary = (
        has(project, r"\[tool\.importlinter\]")
        and "store._tables" in project
        and "sqlalchemy" in project
        and has(imports, r"test_.*raw_db_import.*rejected")
    )

    migration_files = []
    if paths["migrations"].is_dir():
        migration_files = sorted(p.name for p in paths["migrations"].glob("*.py"))

    status = cmd(repo, "git", "status", "--short")
    head = cmd(repo, "git", "rev-parse", "HEAD")

    required_files = {
        k: p.exists()
        for k, p in paths.items()
        if k not in {"test_schema"}
    }

    report = {
        "repo": str(repo),
        "required_paths": required_files,
        "optional_paths": {"test_schema": paths["test_schema"].exists()},
        "invariants": {
            "canonical_as_of_selection_detected": temporal_contract,
            "import_boundary_enforcement_detected": import_boundary,
        },
        "migrations": migration_files,
        "git": {
            "head": head,
            "status": status,
            "dirty": bool(status.get("stdout")),
        },
    }

    ok = all(required_files.values()) and temporal_contract and import_boundary
    report["structural_preflight"] = "PASS" if ok else "FAIL"
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
