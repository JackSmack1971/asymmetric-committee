"""Tests for local Claude Code harness utilities."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_python_edits

ROOT = Path(__file__).resolve().parents[2]


def _preflight(text: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "preflight_goal.py")],
        cwd=cwd,
        input=text,
        capture_output=True,
        text=True,
        check=False,
    )


def test_goal_preflight_accepts_recommended_limit_and_does_not_echo(tmp_path: Path) -> None:
    result = _preflight("x" * 3_500, tmp_path)
    assert result.returncode == 0
    assert "3500 characters" in result.stdout
    assert "WARNING" not in result.stdout
    assert "x" * 100 not in result.stdout + result.stderr
    assert list(tmp_path.iterdir()) == []


def test_goal_preflight_warns_above_recommended_limit(tmp_path: Path) -> None:
    result = _preflight("sensitive goal " + "x" * (3_501 - len("sensitive goal ")), tmp_path)
    assert result.returncode == 0
    assert "WARNING" in result.stdout
    assert "sensitive goal" not in result.stdout + result.stderr


def test_goal_preflight_accepts_command_limit_with_warning(tmp_path: Path) -> None:
    result = _preflight("x" * 4_000, tmp_path)
    assert result.returncode == 0
    assert "4000 characters" in result.stdout
    assert "WARNING" in result.stdout


def test_goal_preflight_blocks_above_command_limit_without_echo(tmp_path: Path) -> None:
    result = _preflight("private payload " + "x" * (4_001 - len("private payload ")), tmp_path)
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr
    assert "private payload" not in result.stdout + result.stderr
    assert list(tmp_path.iterdir()) == []


def test_python_edit_checker_validates_explicit_python_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "new module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    calls: list[list[str]] = []

    def successful_run(
        command: list[str], *, cwd: Path, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(command))
        assert cwd == ROOT
        assert check is False
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(check_python_edits, "run_process", successful_run)
    assert check_python_edits.main([str(target)]) == 0
    assert len(calls) == 2
    assert str(target.resolve()) in calls[0]
    assert "ruff" in calls[1]
    assert str(target.resolve()) in calls[1]


def test_python_edit_checker_propagates_ruff_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "valid.py"
    target.write_text("value = 1\n", encoding="utf-8")
    calls = 0

    def ruff_fails(
        command: list[str], *, cwd: Path, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 1 if "ruff" in command else 0)

    monkeypatch.setattr(check_python_edits, "run_process", ruff_fails)
    assert check_python_edits.main([str(target)]) == 1
    assert calls == 2


def test_python_edit_checker_propagates_compile_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "invalid.py"
    target.write_text("def broken(:\n", encoding="utf-8")
    calls = 0

    def compile_fails(
        command: list[str], *, cwd: Path, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 1 if "py_compile" in command else 0)

    monkeypatch.setattr(check_python_edits, "run_process", compile_fails)
    assert check_python_edits.main([str(target)]) == 1
    assert calls == 2


def test_python_edit_checker_rejects_missing_or_non_python_targets(tmp_path: Path) -> None:
    non_python = tmp_path / "file.txt"
    non_python.write_text("text", encoding="utf-8")
    assert check_python_edits.main([str(non_python)]) == 2
    assert check_python_edits.main([str(tmp_path / "missing.py")]) == 2
