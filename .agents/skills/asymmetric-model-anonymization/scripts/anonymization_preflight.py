#!/usr/bin/env python3
"""Read-only structural preflight for model-input anonymization work."""
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
        return {"command": " ".join(argv), "returncode": None, "stdout": "", "stderr": str(exc)}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default=".")
    ns = p.parse_args()
    repo = Path(ns.repo).resolve()

    paths = {
        "aliases_yaml": repo / "config" / "aliases.yaml",
        "aliases_config": repo / "config" / "aliases.py",
        "store_aliases": repo / "store" / "aliases.py",
        "store_as_of": repo / "store" / "as_of.py",
        "renderer": repo / "features" / "renderer.py",
        "partitioner": repo / "agents" / "partitioner.py",
        "partitions": repo / "agents" / "partitions.py",
        "tests_agents": repo / "tests" / "agents",
        "tests_features": repo / "tests" / "features",
    }
    present = {k: v.exists() for k, v in paths.items()}

    renderer = read(paths["renderer"])
    partition = read(paths["partitioner"]) + "\n" + read(paths["partitions"])
    alias = read(paths["aliases_config"]) + "\n" + read(paths["store_aliases"])

    signals = {
        "alias_component_present": present["aliases_yaml"] or present["aliases_config"] or present["store_aliases"],
        "renderer_present": present["renderer"],
        "partition_component_present": present["partitioner"] or present["partitions"],
        "renderer_has_scrub_or_alias_signal": bool(re.search(r"scrub|alias|token|mask", renderer, re.I)),
        "partition_has_agent_signal": bool(re.search(r"agent|partition", partition, re.I)),
        "alias_has_temporal_signal": bool(re.search(r"as_of|available_at|alias", alias, re.I)),
    }

    tests = []
    for d in (paths["tests_agents"], paths["tests_features"]):
        if d.is_dir():
            tests.extend(str(x.relative_to(repo)) for x in d.rglob("test_*.py") if x.is_file())

    status = cmd(repo, "git", "status", "--short")
    head = cmd(repo, "git", "rev-parse", "HEAD")

    core = (
        signals["alias_component_present"]
        and signals["renderer_present"]
        and signals["partition_component_present"]
    )
    report = {
        "repo": str(repo),
        "paths": present,
        "signals": signals,
        "candidate_leak_tests": sorted(
            x for x in tests if re.search(r"leak|alias|partition|render|anonym", x, re.I)
        ),
        "git": {"head": head, "status": status, "dirty": bool(status.get("stdout"))},
        "structural_preflight": "PASS" if core else "PARTIAL",
        "note": (
            "PASS means the historical anonymization components are present. "
            "PARTIAL means inspect branch/phase state before assuming they exist."
        ),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
