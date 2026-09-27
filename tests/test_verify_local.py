from __future__ import annotations

from collections.abc import Sequence
from subprocess import CompletedProcess
from typing import Any
from unittest.mock import patch

import pytest

from scripts import verify_local


def test_phase_mapping_tracks_implemented_make_gates() -> None:
    assert verify_local.PHASES["P0"].tests == ("tests/contracts", "tests/config")
    assert verify_local.PHASES["P1"].services == ("db", "redis")
    assert "tests" in verify_local.PHASES["P5"].tests
    assert verify_local.PHASES["P5"].services == ("db", "redis")
    assert all(not verify_local.PHASES[f"P{i}"].implemented for i in (6, 7, 8))


def test_result_semantics_are_distinct() -> None:
    assert verify_local.result_label(0) == "PASS"
    assert verify_local.result_label(1) == "FAIL"
    assert verify_local.result_label(2) == "BLOCKED"


def test_unimplemented_phase_is_blocked(capsys: pytest.CaptureFixture[str]) -> None:
    assert verify_local.verify("P6") == 2
    assert "BLOCKED" in capsys.readouterr().out


def test_owner_decision_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        verify_local.PHASES,
        "P0",
        verify_local.Phase(("tests/contracts",), owner_decision="confirm contract shape"),
    )
    assert verify_local.verify("P0") == 2


def test_unknown_phase_is_clear_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert verify_local.verify("P999") == 2
    output = capsys.readouterr().err
    assert "Unsupported phase 'P999'" in output
    assert "P0" in output and "P8" in output


def test_failed_required_check_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_local, "wait_for_services", lambda *_: True)
    monkeypatch.setattr(verify_local, "run", lambda *_args, **_kwargs: 1)
    assert verify_local.verify("P0") == 1


def test_required_services_set_test_urls_and_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_wait(services: tuple[str, ...], env: dict[str, str]) -> bool:
        captured["services"] = services
        captured["env"] = env.copy()
        return True

    def fake_run(command: Sequence[str], env: dict[str, str], **kwargs: object) -> int:
        captured.setdefault("commands", []).append(command)
        return 0

    monkeypatch.setattr(verify_local, "wait_for_services", fake_wait)
    monkeypatch.setattr(verify_local, "run", fake_run)
    assert verify_local.verify("P5") == 0
    env = captured["env"]
    assert captured["services"] == ("db", "redis")
    assert env["TEST_DATABASE_URL"] == verify_local.DATABASE_URL
    assert env["TEST_REDIS_URL"] == verify_local.REDIS_URL
    assert env["REQUIRE_SERVICES"] == "1"
    assert env["REQUIRE_TIMESCALE"] == "1"
    commands = captured["commands"]
    assert commands[0] == ["uv", "run", "ruff", "check", "."]
    assert commands[4] == [
        "uv",
        "run",
        "python",
        "-m",
        "contracts.schema_export",
        "--check",
    ]
    assert commands[5] == ["uv", "run", "pytest", "tests"]


def test_unavailable_service_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_local, "wait_for_services", lambda *_: False)
    with patch.object(verify_local, "run") as run:
        assert verify_local.verify("P1") == 2
        run.assert_not_called()


def test_compose_waits_for_selected_services(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(
        command: Sequence[str], *, cwd: Any, env: dict[str, str], check: bool
    ) -> CompletedProcess[str]:
        seen["command"] = command
        seen["cwd"] = cwd
        return CompletedProcess(command, 0)

    monkeypatch.setattr(verify_local, "run_process", fake_run)
    assert verify_local.wait_for_services(("db", "redis"), {})
    assert seen["command"] == [
        "docker",
        "compose",
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "120",
        "db",
        "redis",
    ]
    assert seen["cwd"] == verify_local.ROOT


def test_missing_verification_tool_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_tool(*_args: Any, **_kwargs: Any) -> CompletedProcess[str]:
        raise FileNotFoundError("uv")

    monkeypatch.setattr(verify_local, "run_process", missing_tool)
    assert verify_local.run(("uv", "run", "ruff"), {}) == 2
