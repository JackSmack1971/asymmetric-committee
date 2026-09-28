#!/usr/bin/env python3
"""Read-only structural preflight for atomic decision-persistence work."""
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


def files_under(path: Path) -> list[str]:
    return sorted(str(p) for p in path.rglob("*.py") if p.is_file()) if path.is_dir() else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ns = ap.parse_args()
    repo = Path(ns.repo).resolve()

    dirs = {
        "committee": repo / "committee",
        "risk": repo / "risk",
        "store": repo / "store",
        "orchestration": repo / "orchestration",
        "evaluation": repo / "evaluation",
        "tests_committee": repo / "tests" / "committee",
        "tests_risk": repo / "tests" / "risk",
        "tests_store": repo / "tests" / "store",
    }

    inventories = {k: files_under(v) for k, v in dirs.items()}
    corpus = ""
    for group in ("committee", "risk", "store", "orchestration"):
        for file_name in inventories[group]:
            try:
                corpus += Path(file_name).read_text(encoding="utf-8") + "\n"
            except OSError:
                pass

    signals = {
        "decision_pipeline_files_present": len(inventories["committee"]) > 1 or len(inventories["risk"]) > 1,
        "store_present": bool(inventories["store"]),
        "transaction_signal": bool(re.search(r"begin\(|begin_nested|transaction|commit\(|rollback", corpus, re.I)),
        "idempotency_signal": bool(re.search(r"idempot|on_conflict|unique|duplicate", corpus, re.I)),
        "replay_signal": bool(re.search(r"replay|resume|rebuild", corpus, re.I)),
        "run_state_signal": bool(re.search(r"completed|partial|failed|status", corpus, re.I)),
    }

    mature = (
        signals["decision_pipeline_files_present"]
        and signals["store_present"]
        and signals["transaction_signal"]
    )

    status = cmd(repo, "git", "status", "--short")
    head = cmd(repo, "git", "rev-parse", "HEAD")

    report = {
        "repo": str(repo),
        "file_counts": {k: len(v) for k, v in inventories.items()},
        "signals": signals,
        "git": {"head": head, "status": status, "dirty": bool(status.get("stdout"))},
        "structural_preflight": "PASS" if mature else "PARTIAL",
        "note": (
            "PASS means a nontrivial decision pipeline plus store/transaction signals are present. "
            "PARTIAL means the checkout appears earlier or structurally different from the historical workflow."
        ),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
