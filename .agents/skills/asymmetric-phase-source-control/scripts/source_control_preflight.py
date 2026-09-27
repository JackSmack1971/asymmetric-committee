#!/usr/bin/env python3
"""Read-only Git/phase preflight for Asymmetric Committee change management."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


def run(repo: Path, *argv: str) -> dict[str, object]:
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


def out(repo: Path, *argv: str) -> str:
    result = run(repo, *argv)
    return str(result.get("stdout") or "")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ns = ap.parse_args()
    repo = Path(ns.repo).resolve()

    branch = out(repo, "git", "branch", "--show-current")
    head = out(repo, "git", "rev-parse", "HEAD")
    status = run(repo, "git", "status", "--short")
    staged = run(repo, "git", "diff", "--cached", "--name-only")
    unstaged = run(repo, "git", "diff", "--name-only")
    untracked = run(repo, "git", "ls-files", "--others", "--exclude-standard")
    upstream = run(repo, "git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")

    plans_dir = repo / "docs" / "plans"
    plan_files = sorted(p.name for p in plans_dir.glob("P*.md")) if plans_dir.is_dir() else []

    phase_match = re.fullmatch(r"phase/(P\d+)", branch)
    active_phase = phase_match.group(1) if phase_match else None
    expected_plan = f"{active_phase}.md" if active_phase else None

    plans_readme = read(plans_dir / "README.md")
    convention_signals = {
        "phase_branch_convention": "phase/P<n>" in plans_readme,
        "one_pr_to_main": "one PR to `main`" in plans_readme,
        "small_commit_guidance": "small commits" in plans_readme,
        "gate_required": "make gate-P<n>" in plans_readme,
    }

    report = {
        "repo": str(repo),
        "git": {
            "branch": branch,
            "head": head,
            "upstream": upstream,
            "status": status,
            "staged_paths": staged,
            "unstaged_paths": unstaged,
            "untracked_paths": untracked,
            "dirty": bool(status.get("stdout")),
        },
        "phase": {
            "active_phase": active_phase,
            "expected_plan": expected_plan,
            "plan_present": bool(expected_plan and expected_plan in plan_files),
            "available_plans": plan_files,
        },
        "conventions": convention_signals,
    }

    git_ok = bool(head) and status.get("returncode") == 0
    conventions_ok = all(convention_signals.values())
    report["structural_preflight"] = "PASS" if git_ok and conventions_ok else "PARTIAL"
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
