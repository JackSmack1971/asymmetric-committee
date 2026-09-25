"""Exported LLM schemas are strict-mode clean (§10.2) and current on disk."""

from __future__ import annotations

import json
from typing import Any

import pytest

from contracts import schema_export as se
from contracts.models import LLM_OUTPUT_MODELS, Contract

IDS = [m.__name__ for m in LLM_OUTPUT_MODELS]
# Fields the system fills; an LLM must never be asked for them (invariants 6, 7).
FORBIDDEN_FIELDS = {
    "model_served",
    "run_id",
    "prompt_version",
    "as_of",
    "target_weight",
    "weight",
    "valid",
}


def walk_objects(node: Any, path: str = "$") -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            out.append((path, node))
        for k, v in node.items():
            out += walk_objects(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += walk_objects(v, f"{path}[{i}]")
    return out


def all_keys(node: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "properties":
                for sub in v.values():
                    keys |= all_keys(sub)
            else:
                keys.add(k)
                keys |= all_keys(v)
    elif isinstance(node, list):
        for v in node:
            keys |= all_keys(v)
    return keys


def lint(schema: dict[str, Any]) -> list[str]:
    errors = []
    if schema.get("type") != "object":
        errors.append("root must be an object")
    for path, obj in walk_objects(schema):
        if obj.get("additionalProperties") is not False:
            errors.append(f"{path}: additionalProperties must be false")
        if sorted(obj.get("required", [])) != sorted(obj.get("properties", {})):
            errors.append(f"{path}: required must list every property")
    unsupported = all_keys(schema) - se.ALLOWED_KEYWORDS
    if unsupported:
        errors.append(f"unsupported keywords: {sorted(unsupported)}")
    return errors


@pytest.mark.parametrize("model", LLM_OUTPUT_MODELS, ids=IDS)
def test_generated_schema_is_strict(model: type[Contract]) -> None:
    assert lint(se.strict_schema(model)) == []


@pytest.mark.parametrize("model", LLM_OUTPUT_MODELS, ids=IDS)
def test_file_on_disk_is_strict_and_current(model: type[Contract]) -> None:
    path = se.schema_path(model)
    assert path.exists(), f"missing {path}; run python -m contracts.schema_export"
    assert path.read_text() == se.render(model), "stale; run python -m contracts.schema_export"
    assert lint(json.loads(path.read_text())) == []


def test_no_orphan_schema_files() -> None:
    assert se.stale_schemas() == []


@pytest.mark.parametrize("model", LLM_OUTPUT_MODELS, ids=IDS)
def test_llm_never_asked_for_system_fields(model: type[Contract]) -> None:
    def names(node: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            found |= set(node.get("properties", {}))
            for v in node.values():
                found |= names(v)
        elif isinstance(node, list):
            for v in node:
                found |= names(v)
        return found

    assert not names(se.strict_schema(model)) & FORBIDDEN_FIELDS


def test_lint_catches_violations() -> None:
    bad = {
        "type": "object",
        "properties": {"a": {"type": "number", "minimum": 0}, "b": {"type": "string"}},
        "required": ["a"],
    }
    errors = lint(bad)
    assert any("additionalProperties" in e for e in errors)
    assert any("required" in e for e in errors)
    assert any("minimum" in e for e in errors)


def test_response_format_shape() -> None:
    rf = se.response_format(LLM_OUTPUT_MODELS[0])
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "AgentVerdictLLM"


def test_check_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert se.main(["--check"]) == 0
