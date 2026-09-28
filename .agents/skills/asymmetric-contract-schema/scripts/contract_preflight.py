#!/usr/bin/env python3
"""Read-only structural preflight for Asymmetric Committee contract/schema work."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


def command(repo: Path, *argv: str) -> dict[str, object]:
    try:
        cp = subprocess.run(
            argv, cwd=repo, text=True, capture_output=True, check=False
        )
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


def contains(path: Path, pattern: str) -> bool:
    if not path.is_file():
        return False
    return bool(re.search(pattern, path.read_text(encoding="utf-8"), re.M))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", help="Repository root (default: cwd)")
    ns = parser.parse_args()
    repo = Path(ns.repo).resolve()

    paths = {
        "models": repo / "contracts" / "models.py",
        "enums": repo / "contracts" / "enums.py",
        "data": repo / "contracts" / "data.py",
        "schema_export": repo / "contracts" / "schema_export.py",
        "schema_dir": repo / "contracts" / "schemas",
    }

    schema_export_ok = (
        contains(paths["schema_export"], r"def\s+stale_schemas\s*\(")
        and contains(paths["schema_export"], r"""["']--check["']""")
        and contains(paths["schema_export"], r"def\s+write_all\s*\(")
    )

    llm_split = (
        contains(paths["models"], r"class\s+\w+LLM\s*\(")
        and contains(paths["models"], r"LLM_OUTPUT_MODELS\s*:")
    )

    schema_files = []
    if paths["schema_dir"].is_dir():
        schema_files = sorted(p.name for p in paths["schema_dir"].glob("*.json"))

    git_status = command(repo, "git", "status", "--short")
    git_head = command(repo, "git", "rev-parse", "HEAD")

    report = {
        "repo": str(repo),
        "paths": {name: path.exists() for name, path in paths.items()},
        "schema_export_contract": {
            "has_stale_check_and_writer": schema_export_ok,
            "llm_source_envelope_split_detected": llm_split,
            "checked_in_schema_files": schema_files,
        },
        "git": {
            "head": git_head,
            "status": git_status,
            "dirty": bool(git_status.get("stdout")),
        },
    }

    required = (
        paths["models"].is_file()
        and paths["enums"].is_file()
        and paths["schema_export"].is_file()
        and paths["schema_dir"].is_dir()
        and schema_export_ok
    )
    report["structural_preflight"] = "PASS" if required else "FAIL"
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if required else 1


if __name__ == "__main__":
    raise SystemExit(main())
