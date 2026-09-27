"""Run the authoritative local verification recipe for one project phase."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = "postgresql+psycopg://committee:committee@127.0.0.1:5432/committee"
REDIS_URL = "redis://127.0.0.1:6379/0"
run_process = subprocess.run


@dataclass(frozen=True)
class Phase:
    tests: tuple[str, ...]
    services: tuple[str, ...] = ()
    implemented: bool = True
    owner_decision: str | None = None


PHASES: dict[str, Phase] = {
    "P0": Phase(("tests/contracts", "tests/config")),
    "P1": Phase(
        ("tests/store", "tests/ingest", "tests/universe", "tests/config"),
        ("db", "redis"),
    ),
    "P2": Phase(
        (
            "tests/contracts",
            "tests/config",
            "tests/features",
            "tests/gate",
            "tests/risk",
            "tests/evaluation",
        )
    ),
    "P3": Phase(
        ("tests/contracts", "tests/config", "tests/features", "tests/universe", "tests/agents"),
        ("redis",),
    ),
    "P4": Phase(
        (
            "tests/contracts",
            "tests/config",
            "tests/features",
            "tests/agents",
            "tests/committee",
            "tests/risk",
        ),
        ("redis",),
    ),
    "P5": Phase(("tests",), ("db", "redis")),
    "P6": Phase((), implemented=False),
    "P7": Phase((), implemented=False),
    "P8": Phase((), implemented=False),
}


def result_label(code: int) -> str:
    return {0: "PASS", 1: "FAIL", 2: "BLOCKED"}[code]


def run(command: Sequence[str], env: dict[str, str], *, check_failure: bool = True) -> int:
    rendered = subprocess.list2cmdline(list(command))
    print(f"+ {rendered}", flush=True)
    try:
        completed = run_process(command, cwd=ROOT, env=env, check=False)
    except FileNotFoundError:
        print(f"BLOCKED: required command is unavailable: {command[0]}")
        return 2
    if completed.returncode and check_failure:
        print(f"FAIL: required command exited {completed.returncode}: {rendered}")
        return 1
    return completed.returncode


def wait_for_services(services: tuple[str, ...], env: dict[str, str]) -> bool:
    if not services:
        return True
    command = ["docker", "compose", "up", "-d", "--wait", "--wait-timeout", "120", *services]
    print(f"+ {subprocess.list2cmdline(command)}", flush=True)
    try:
        completed = run_process(command, cwd=ROOT, env=env, check=False)
    except FileNotFoundError:
        print("BLOCKED: Docker Compose is required for this phase but is unavailable.")
        return False
    if completed.returncode:
        print("BLOCKED: required Compose services could not be started or did not become healthy.")
        return False
    return True


def verify(phase_name: str) -> int:
    phase = PHASES.get(phase_name.upper())
    if phase is None:
        supported = ", ".join(PHASES)
        print(f"Unsupported phase {phase_name!r}. Supported phases: {supported}.", file=sys.stderr)
        return 2
    if not phase.implemented:
        print(f"BLOCKED: {phase_name.upper()} gate is not implemented.")
        return 2
    if phase.owner_decision is not None:
        print(f"BLOCKED: {phase_name.upper()} requires an owner decision: {phase.owner_decision}")
        return 2

    env = os.environ.copy()
    if "db" in phase.services:
        env["TEST_DATABASE_URL"] = DATABASE_URL
        if phase_name.upper() == "P5":
            env["REQUIRE_TIMESCALE"] = "1"
    if "redis" in phase.services:
        env["TEST_REDIS_URL"] = REDIS_URL
    if phase.services:
        env["REQUIRE_SERVICES"] = "1"
        if not wait_for_services(phase.services, env):
            return 2

    checks = (
        ["uv", "run", "ruff", "check", "."],
        ["uv", "run", "ruff", "format", "--check", "."],
        ["uv", "run", "mypy", "."],
        ["uv", "run", "lint-imports"],
        ["uv", "run", "python", "-m", "contracts.schema_export", "--check"],
    )
    for command in checks:
        result = run(command, env)
        if result:
            return result

    pytest = ["uv", "run", "pytest", *phase.tests]
    result = run(pytest, env)
    if result:
        return result
    print(f"PASS: {phase_name.upper()} required verification succeeded.")
    return 0


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print("Usage: python scripts/verify_local.py <phase> (for example, P2).", file=sys.stderr)
        return 2
    return verify(argv[1])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
