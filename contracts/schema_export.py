"""Export strict JSON Schemas for LLM-facing models (§10.2).

OpenRouter ``response_format.json_schema`` with ``strict: true`` needs every object to set
``additionalProperties: false`` and list every property in ``required``, and it rejects many
validation keywords. So refs are inlined, constraint keywords are dropped (Pydantic re-checks
them after parsing), and only an allow-listed keyword set remains.

Usage: ``python -m contracts.schema_export`` to write, ``--check`` to verify files are current.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from contracts.models import LLM_OUTPUT_MODELS, Contract

SCHEMA_DIR = Path(__file__).parent / "schemas"

ALLOWED_KEYWORDS = frozenset(
    {"type", "properties", "required", "additionalProperties", "items", "enum", "description"}
)


def _resolve(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_resolve(n, defs) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = _resolve(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
        # Keep a field-level description over the referenced type's.
        return {**target, **({"description": node["description"]} if "description" in node else {})}
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties":
            out[key] = {name: _resolve(sub, defs) for name, sub in value.items()}
        elif key in ALLOWED_KEYWORDS:
            out[key] = _resolve(value, defs)
    if out.get("type") == "object":
        out["additionalProperties"] = False
        out["required"] = list(out.get("properties", {}))
    return out


def strict_schema(model: type[Contract]) -> dict[str, Any]:
    raw = model.model_json_schema(mode="validation")
    defs = raw.pop("$defs", {})
    schema: dict[str, Any] = _resolve(raw, defs)
    return schema


def response_format(model: type[Contract]) -> dict[str, Any]:
    """The ``response_format`` payload for an OpenRouter request."""
    return {
        "type": "json_schema",
        "json_schema": {"name": model.__name__, "strict": True, "schema": strict_schema(model)},
    }


def render(model: type[Contract]) -> str:
    return json.dumps(strict_schema(model), indent=2, sort_keys=True) + "\n"


def schema_path(model: type[Contract]) -> Path:
    return SCHEMA_DIR / f"{model.__name__}.json"


def stale_schemas() -> list[Path]:
    stale = [
        schema_path(m)
        for m in LLM_OUTPUT_MODELS
        if not schema_path(m).exists() or schema_path(m).read_text() != render(m)
    ]
    expected = {schema_path(m) for m in LLM_OUTPUT_MODELS}
    stale += sorted(p for p in SCHEMA_DIR.glob("*.json") if p not in expected)
    return stale


def write_all() -> None:
    SCHEMA_DIR.mkdir(exist_ok=True)
    for m in LLM_OUTPUT_MODELS:
        schema_path(m).write_text(render(m))


def main(argv: list[str]) -> int:
    if "--check" in argv:
        stale = stale_schemas()
        for p in stale:
            print(f"stale or orphaned schema: {p}", file=sys.stderr)
        if stale:
            print("run: uv run python -m contracts.schema_export", file=sys.stderr)
        return 1 if stale else 0
    write_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
