#!/usr/bin/env python3
"""Read-only structural preflight for an Asymmetric Committee phase."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


def run(repo: Path, *args: str) -> dict[str, object]:
    try:
        cp = subprocess.run(
            list(args),
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "command": " ".join(args),
            "returncode": cp.returncode,
            "stdout": cp.stdout.strip(),
            "stderr": cp.stderr.strip(),
        }
    except OSError as exc:
        return {
            "command": " ".join(args),
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", help="Phase identifier such as P3")
    parser.add_argument("--repo", default=".", help="Repository root (default: cwd)")
    ns = parser.parse_args()

    phase = ns.phase.upper()
    if not re.fullmatch(r"P(?:-BOOT|\d+)", phase):
        parser.error("phase must look like P0, P5, or P-boot")

    repo = Path(ns.repo).resolve()
    required = {
        "blueprint": repo / "docs" / "asymmetric-committee-blueprint.md",
        "progress": repo / "docs" / "PROGRESS.md",
        "plans_readme": repo / "docs" / "plans" / "README.md",
        "makefile": repo / "Makefile",
    }
    plan = repo / "docs" / "plans" / f"{phase}.md"

    make_text = required["makefile"].read_text(encoding="utf-8") if required["makefile"].is_file() else ""
    gate = f"gate-{phase}"
    gate_present = bool(re.search(rf"(?m)^{re.escape(gate)}\s*:", make_text))

    plan_sections = {}
    if plan.is_file():
        text = plan.read_text(encoding="utf-8")
        for heading in ("Scope", "Invariants touched", "Steps", "Gate", "Spec issues"):
            plan_sections[heading] = bool(
                re.search(rf"(?im)^#+\s+{re.escape(heading)}\s*$", text)
            )

    branch = run(repo, "git", "branch", "--show-current")
    head = run(repo, "git", "rev-parse", "HEAD")
    status = run(repo, "git", "status", "--short")

    report = {
        "phase": phase,
        "repo": str(repo),
        "required_files": {k: p.is_file() for k, p in required.items()},
        "plan": {
            "path": str(plan.relative_to(repo)),
            "exists": plan.is_file(),
            "required_sections": plan_sections,
        },
        "gate": {"target": gate, "present_in_makefile": gate_present},
        "git": {
            "branch": branch,
            "head": head,
            "status": status,
            "dirty": bool(status.get("stdout")),
        },
    }

    fatal = not all(report["required_files"].values()) or not gate_present
    report["structural_preflight"] = "FAIL" if fatal else "PASS"
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if fatal else 0


if __name__ == "__main__":
    raise SystemExit(main())
