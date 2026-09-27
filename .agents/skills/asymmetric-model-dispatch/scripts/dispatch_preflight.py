#!/usr/bin/env python3
"""Read-only structural preflight for reliable model-dispatch work."""
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ns = ap.parse_args()
    repo = Path(ns.repo).resolve()

    paths = {
        "models_yaml": repo / "config" / "models.yaml",
        "config_loader": repo / "config" / "loader.py",
        "contracts_models": repo / "contracts" / "models.py",
        "agents_base": repo / "agents" / "base.py",
        "openrouter": repo / "agents" / "llm" / "openrouter.py",
        "ratelimit": repo / "agents" / "llm" / "ratelimit.py",
        "store": repo / "agents" / "llm" / "store.py",
        "runner": repo / "agents" / "runner.py",
        "prompts": repo / "prompts" / "agents",
        "test_openrouter": repo / "tests" / "agents" / "test_openrouter.py",
        "test_llm_redis": repo / "tests" / "agents" / "test_llm_redis.py",
        "test_base": repo / "tests" / "agents" / "test_base.py",
        "test_runner": repo / "tests" / "agents" / "test_runner.py",
        "test_p3_e2e": repo / "tests" / "agents" / "test_p3_e2e.py",
    }
    present = {k: p.exists() for k, p in paths.items()}

    models = read(paths["models_yaml"])
    openrouter = read(paths["openrouter"])
    ratelimit = read(paths["ratelimit"])
    store = read(paths["store"])
    runner = read(paths["runner"])

    signals = {
        "model_config_present": present["models_yaml"],
        "placeholder_models_detected": bool(re.search(r"\bTODO/", models)),
        "cutoff_metadata_detected": "stated_training_cutoff" in models,
        "openrouter_boundary_present": present["openrouter"],
        "shared_rate_component_present": present["ratelimit"],
        "cache_or_dlq_component_present": present["store"],
        "runner_present": present["runner"],
        "budget_signal_detected": bool(re.search(r"budget|cost", openrouter + "\n" + runner + "\n" + store, re.I)),
        "cache_signal_detected": bool(re.search(r"cache|config.?hash", store + "\n" + runner, re.I)),
        "dlq_signal_detected": bool(re.search(r"dlq|dead.?letter", store + "\n" + runner, re.I)),
        "resume_signal_detected": bool(re.search(r"resume|completed|task.?key", runner + "\n" + store, re.I)),
    }

    historical_stack = all(
        present[x] for x in ("openrouter", "ratelimit", "store", "runner")
    )

    status = cmd(repo, "git", "status", "--short")
    head = cmd(repo, "git", "rev-parse", "HEAD")

    report = {
        "repo": str(repo),
        "paths": present,
        "signals": signals,
        "git": {"head": head, "status": status, "dirty": bool(status.get("stdout"))},
        "structural_preflight": "PASS" if historical_stack else "PARTIAL",
        "note": (
            "PASS means the historical centralized dispatch/rate/store/runner stack is present. "
            "PARTIAL means this checkout appears earlier or structurally different; inspect the active phase before editing."
        ),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
