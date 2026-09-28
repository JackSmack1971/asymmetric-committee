"""Run syntax compilation and the repository Ruff configuration on explicit Python files."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_process(
    command: Sequence[str], *, cwd: Path, check: bool
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, cwd=cwd, check=check)


def main(argv: Sequence[str]) -> int:
    if not argv:
        print(
            "Usage: python scripts/check_python_edits.py <file.py> [<file.py> ...]",
            file=sys.stderr,
        )
        return 2

    targets = [Path(arg).resolve() for arg in argv]
    invalid = [path for path in targets if path.suffix != ".py" or not path.is_file()]
    if invalid:
        for path in invalid:
            print(f"Invalid Python target: {path}", file=sys.stderr)
        return 2

    failed = False
    commands = [
        [sys.executable, "-m", "py_compile", *(str(path) for path in targets)],
        [
            "uv",
            "run",
            "ruff",
            "check",
            "--config",
            str(ROOT / "pyproject.toml"),
            *(str(path) for path in targets),
        ],
    ]
    for command in commands:
        rendered = subprocess.list2cmdline(command)
        print(f"+ {rendered}", flush=True)
        try:
            result = run_process(command, cwd=ROOT, check=False)
        except FileNotFoundError:
            print(f"FAIL: required command is unavailable: {command[0]}", file=sys.stderr)
            failed = True
            continue
        if result.returncode:
            print(f"FAIL: command exited {result.returncode}: {rendered}", file=sys.stderr)
            failed = True

    if failed:
        return 1
    print(f"PASS: {len(targets)} Python file(s) compiled and passed Ruff.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
