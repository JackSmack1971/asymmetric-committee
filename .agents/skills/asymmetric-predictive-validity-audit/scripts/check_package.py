#!/usr/bin/env python3
"""Deterministic package sanity checks for this skill."""

from __future__ import annotations

import json
import py_compile
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
REQUIRED = [
    ROOT / "agents" / "openai.yaml",
    ROOT / "references" / "audit-rubric.md",
    ROOT / "references" / "statistical-decision-guide.md",
    ROOT / "references" / "research-basis.md",
    ROOT / "references" / "evaluation-protocol.md",
    ROOT / "scripts" / "validate_audit_report.py",
    ROOT / "scripts" / "eval_telemetry.py",
    ROOT / "scripts" / "run_skill_evals.py",
    ROOT / "scripts" / "score_eval_run.py",
    ROOT / "scripts" / "test_eval_telemetry.py",
    ROOT / "evals" / "corpus.json",
    ROOT / "evals" / "README.md",
    ROOT / "telemetry" / "README.md",
    ROOT / "telemetry" / "SCHEMA.md",
    ROOT / "telemetry" / "schemas" / "event.schema.json",
    ROOT / "telemetry" / "runs" / ".gitignore",
]


def check_corpus(errors: list[str]) -> None:
    path = ROOT / "evals" / "corpus.json"
    if not path.exists():
        return
    try:
        corpus = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"eval corpus is not valid JSON: {exc}")
        return
    if corpus.get("schema") != "apva.eval.corpus.v1":
        errors.append("eval corpus schema must be apva.eval.corpus.v1")
    if corpus.get("skill") != "asymmetric-predictive-validity-audit":
        errors.append("eval corpus skill name mismatch")
    cases = corpus.get("cases")
    if not isinstance(cases, list):
        errors.append("eval corpus cases must be a list")
        return
    ids = [c.get("id") for c in cases if isinstance(c, dict)]
    if len(ids) != len(set(ids)):
        errors.append("eval corpus case ids must be unique")
    suites = Counter(c.get("suite") for c in cases if isinstance(c, dict))
    if suites != Counter({"routing": 50, "task": 10, "recovery": 5}):
        errors.append(f"eval corpus suite counts changed unexpectedly: {dict(suites)}")
    routing = [c for c in cases if isinstance(c, dict) and c.get("suite") == "routing"]
    groups = Counter(c.get("routing_group") for c in routing)
    if groups != Counter({"positive": 20, "negative": 20, "neighbor": 10}):
        errors.append(f"routing group counts changed unexpectedly: {dict(groups)}")
    for case in routing:
        if not isinstance(case.get("expected_activation"), bool):
            errors.append(f"routing case {case.get('id')} lacks boolean expected_activation")
    for case in cases:
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("prompt"), str)
            or not case.get("prompt", "").strip()
        ):
            errors.append(f"case {getattr(case, 'get', lambda *_: '?')('id')} lacks a prompt")
    protocol = corpus.get("protocol", {})
    if protocol != {"routing_repeats": 3, "task_repeats": 5, "recovery_repeats": 3}:
        errors.append(f"unexpected protocol repeat contract: {protocol}")


def main() -> int:
    errors: list[str] = []
    if not SKILL.exists():
        errors.append("missing SKILL.md")
    else:
        text = SKILL.read_text(encoding="utf-8")
        fm = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.S)
        if not fm:
            errors.append("SKILL.md lacks YAML frontmatter")
        else:
            keys = []
            for line in fm.group(1).splitlines():
                if line and not line.startswith(" ") and ":" in line:
                    keys.append(line.split(":", 1)[0].strip())
            if keys != ["name", "description"]:
                errors.append(f"frontmatter keys must be exactly name, description; got {keys}")
        if "TODO" in text or "PLACEHOLDER" in text:
            errors.append("SKILL.md contains placeholder text")

    for path in REQUIRED:
        if not path.exists():
            errors.append(f"missing required package file: {path.relative_to(ROOT)}")

    openai_yaml = ROOT / "agents" / "openai.yaml"
    if openai_yaml.exists():
        yaml_text = openai_yaml.read_text(encoding="utf-8")
        for token in ("display_name:", "short_description:", "default_prompt:"):
            if token not in yaml_text:
                errors.append(f"agents/openai.yaml missing {token[:-1]}")
        if "$asymmetric-predictive-validity-audit" not in yaml_text:
            errors.append(
                "default_prompt must explicitly mention $asymmetric-predictive-validity-audit"
            )

    check_corpus(errors)

    for schema in (ROOT / "telemetry" / "schemas").glob("*.json"):
        try:
            json.loads(schema.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"invalid telemetry schema {schema.name}: {exc}")

    for script in (ROOT / "scripts").glob("*.py"):
        try:
            py_compile.compile(str(script), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"python compile failure {script.name}: {exc}")

    gi = ROOT / "telemetry" / "runs" / ".gitignore"
    if gi.exists() and "*" not in gi.read_text(encoding="utf-8"):
        errors.append("telemetry/runs/.gitignore must ignore raw run artifacts")

    if errors:
        for item in errors:
            print(f"FAIL: {item}")
        return 1
    print("PASS: package sanity checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
