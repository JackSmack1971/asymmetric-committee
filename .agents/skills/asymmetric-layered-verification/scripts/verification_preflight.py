#!/usr/bin/env python3
"""Read-only structural preflight for layered verification and phase gates."""
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

    makefile = repo / "Makefile"
    pyproject = repo / "pyproject.toml"
    tests = repo / "tests"
    compose = repo / "docker-compose.yml"
    if not compose.exists():
        compose = repo / "compose.yaml"

    mk = read(makefile)

    gates = {}
    for m in re.finditer(r"(?m)^gate-(P\d+)\s*:(.*)$", mk):
        phase = m.group(1)
        start = m.start()
        nxt = re.search(r"(?m)^gate-P\d+\s*:", mk[m.end():])
        end = m.end() + (nxt.start() if nxt else len(mk[m.end():]))
        block = mk[start:end]
        gates[phase] = {
            "implemented": "not implemented" not in block.lower(),
            "fail_closed_stub": "not implemented" in block.lower() and "exit 1" in block,
            "requires_services": "REQUIRE_SERVICES=1" in block,
            "has_pytest": "pytest" in block,
            "has_schema_check": "schema_export --check" in block,
        }

    signals = {
        "makefile_present": makefile.exists(),
        "pyproject_present": pyproject.exists(),
        "tests_present": tests.is_dir(),
        "compose_present": compose.exists(),
        "lint_target_detected": bool(re.search(r"(?m)^lint\s*:", mk)),
        "test_target_detected": bool(re.search(r"(?m)^test\s*:", mk)),
        "phase_gate_count": len(gates),
        "service_required_gate_detected": any(x["requires_services"] for x in gates.values()),
        "fail_closed_stub_detected": any(x["fail_closed_stub"] for x in gates.values()),
    }

    status = cmd(repo, "git", "status", "--short")
    head = cmd(repo, "git", "rev-parse", "HEAD")

    ok = (
        signals["makefile_present"]
        and signals["pyproject_present"]
        and signals["tests_present"]
        and signals["lint_target_detected"]
        and signals["phase_gate_count"] > 0
    )

    report = {
        "repo": str(repo),
        "signals": signals,
        "gates": gates,
        "git": {"head": head, "status": status, "dirty": bool(status.get("stdout"))},
        "structural_preflight": "PASS" if ok else "PARTIAL",
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
