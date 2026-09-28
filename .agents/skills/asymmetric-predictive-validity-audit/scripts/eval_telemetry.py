#!/usr/bin/env python3
"""Shared deterministic telemetry helpers for the packaged skill evaluation corpus."""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

TARGET_SKILL = "asymmetric-predictive-validity-audit"
ALLOWED_VERDICTS = {
    "READY TO PREREGISTER",
    "CONDITIONALLY READY",
    "NOT IDENTIFIABLE",
    "NOT FALSIFIABLE",
    "BLOCKED BY MISSING EVIDENCE",
}
NON_TOOL_ITEM_TYPES = {"agent_message", "reasoning", "todo_list"}
EXTERNAL_ITEM_MARKERS = ("mcp", "web_search", "browser", "computer", "image")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
            if isinstance(value, dict):
                out.append(value)
    return out


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        fh.flush()


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _otel_attributes(value: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    if not isinstance(value, list):
        return attrs
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        raw = item.get("value")
        if not isinstance(raw, dict):
            continue
        parsed: Any = None
        for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if key in raw:
                parsed = raw[key]
                break
        attrs[item["key"]] = parsed
    return attrs


def parse_otel_metrics(payloads: list[Any], target_skill: str = TARGET_SKILL) -> dict[str, Any]:
    """Parse OTLP/HTTP JSON enough to prove telemetry liveness and native skill injection."""
    metric_names: set[str] = set()
    injections: list[dict[str, Any]] = []
    for payload in payloads:
        for node in _walk_dicts(payload):
            name = node.get("name")
            if not isinstance(name, str):
                continue
            metric_names.add(name)
            if name != "codex.skill.injected":
                continue
            for child in _walk_dicts(node):
                attrs = _otel_attributes(child.get("attributes"))
                if attrs.get("skill") != target_skill:
                    continue
                count = child.get("asInt", child.get("asDouble", 1))
                try:
                    numeric = float(count)
                except (TypeError, ValueError):
                    numeric = 1.0
                if numeric <= 0:
                    continue
                injections.append(
                    {
                        "skill": attrs.get("skill"),
                        "status": attrs.get("status"),
                        "invoke_type": attrs.get("invoke_type"),
                        "plugin_id": attrs.get("plugin_id"),
                        "model_slug": attrs.get("model_slug"),
                        "reasoning_effort": attrs.get("reasoning_effort"),
                        "value": numeric,
                    }
                )
    return {
        "metrics_live": bool(metric_names),
        "metric_names": sorted(metric_names),
        "target_injected": bool(injections),
        "target_injection_count": sum(x["value"] for x in injections),
        "injections": injections,
    }


def _numeric_usage(value: Any, out: dict[str, int]) -> None:
    if not isinstance(value, dict):
        return
    aliases = {
        "input_tokens": "input_tokens",
        "cached_input_tokens": "cached_input_tokens",
        "cache_write_input_tokens": "cache_write_input_tokens",
        "output_tokens": "output_tokens",
        "reasoning_output_tokens": "reasoning_output_tokens",
        "total_tokens": "total_tokens",
    }
    for key, item in value.items():
        if key in aliases and isinstance(item, (int, float)):
            out[aliases[key]] = out.get(aliases[key], 0) + int(item)
        elif isinstance(item, dict):
            _numeric_usage(item, out)


def parse_codex_trace(path: Path) -> dict[str, Any]:
    events = load_jsonl(path)
    usage: dict[str, int] = {}
    commands = 0
    failed_commands = 0
    tool_calls = 0
    external_operations = 0
    trace_failures = 0
    item_types: dict[str, int] = {}
    for event in events:
        typ = str(event.get("type") or "")
        if typ == "turn.completed":
            _numeric_usage(event.get("usage", event), usage)
        if typ == "turn.failed" or "error" in typ.lower() or typ.lower().endswith("failed"):
            trace_failures += 1
        if typ != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "unknown")
        item_types[item_type] = item_types.get(item_type, 0) + 1
        if item_type == "command_execution":
            commands += 1
            code = item.get("exit_code")
            if isinstance(code, int) and code != 0:
                failed_commands += 1
        if item_type not in NON_TOOL_ITEM_TYPES:
            tool_calls += 1
        if any(marker in item_type for marker in EXTERNAL_ITEM_MARKERS):
            external_operations += 1
    if "total_tokens" not in usage:
        usage["total_tokens"] = (
            usage.get("input_tokens", 0)
            + usage.get("cache_write_input_tokens", 0)
            + usage.get("output_tokens", 0)
            + usage.get("reasoning_output_tokens", 0)
        )
    return {
        "usage": usage,
        "commands": commands,
        "failed_commands": failed_commands,
        "tool_calls": tool_calls,
        "external_operations": external_operations,
        "trace_failures": trace_failures,
        "item_types": item_types,
        "events": len(events),
    }


def extract_verdict(text: str) -> str | None:
    """Read only the report's Final Verdict section; earlier discussion may name rejected verdicts."""
    match = re.search(r"(?ms)^#+\s+Final Verdict\s*$\n(.*?)(?=^#+\s+|\Z)", text)
    final = match.group(1) if match else ""
    matches = [
        verdict
        for verdict in sorted(ALLOWED_VERDICTS)
        if re.search(rf"\b{re.escape(verdict)}\b", final)
    ]
    return matches[0] if len(matches) == 1 else None


def _group_satisfied(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.I | re.S) for pattern in patterns)


def grade_report(
    report_path: Path, grader: dict[str, Any], validator_exit_code: int | None
) -> dict[str, Any]:
    failures: list[str] = []
    text = report_path.read_text(encoding="utf-8", errors="replace") if report_path.exists() else ""
    if grader.get("report_required", True) and not report_path.exists():
        failures.append("report_missing")
    if grader.get("validator_required", True) and validator_exit_code != 0:
        failures.append("report_validator_failed")
    verdict = extract_verdict(text)
    allowed = grader.get("allowed_verdicts")
    if isinstance(allowed, list) and allowed and verdict not in allowed:
        failures.append(f"verdict_not_allowed:{verdict or 'none'}")
    forbidden = grader.get("forbidden_verdicts")
    if isinstance(forbidden, list) and verdict in forbidden:
        failures.append(f"forbidden_verdict:{verdict}")
    for idx, group in enumerate(grader.get("required_pattern_groups", []), 1):
        if (
            isinstance(group, list)
            and group
            and not _group_satisfied(text, [str(x) for x in group])
        ):
            failures.append(f"required_pattern_group_{idx}_missing")
    for pattern in grader.get("forbidden_patterns", []):
        if re.search(str(pattern), text, flags=re.I | re.S):
            failures.append(f"forbidden_pattern:{pattern}")
    return {
        "success": not failures,
        "failures": failures,
        "verdict": verdict,
        "report_bytes": len(text.encode("utf-8")),
        "validator_exit_code": validator_exit_code,
    }


def routing_classification(expected: bool, otel: dict[str, Any]) -> dict[str, Any]:
    if not otel.get("metrics_live"):
        return {"known": False, "actual_activation": None, "classification": "unknown"}
    actual = bool(otel.get("target_injected"))
    if expected and not actual:
        classification = "routing_miss"
    elif not expected and actual:
        classification = "false_activation"
    elif expected and actual:
        classification = "true_activation"
    else:
        classification = "true_non_activation"
    return {"known": True, "actual_activation": actual, "classification": classification}


def _median(values: list[float | int]) -> float | None:
    return float(statistics.median(values)) if values else None


def _safe_rate(successes: int, total: int) -> float | None:
    return (successes / total) if total else None


def _relative_error_reduction(
    skill_rate: float | None, baseline_rate: float | None
) -> float | None:
    if not isinstance(skill_rate, (int, float)) or not isinstance(baseline_rate, (int, float)):
        return None
    baseline_error = 1.0 - baseline_rate
    if baseline_error <= 0:
        return None
    return (baseline_error - (1.0 - skill_rate)) / baseline_error


def _resource_delta(
    skill_value: float | None, baseline_value: float | None
) -> dict[str, float | None]:
    if not isinstance(skill_value, (int, float)) or not isinstance(baseline_value, (int, float)):
        return {"absolute": None, "relative": None}
    absolute = float(skill_value) - float(baseline_value)
    relative = (absolute / float(baseline_value)) if baseline_value else None
    return {"absolute": absolute, "relative": relative}


def _suite_condition_metrics(
    results: list[dict[str, Any]], suite: str, condition: str
) -> dict[str, Any]:
    rows = [r for r in results if r.get("suite") == suite and r.get("condition") == condition]
    valid_rows = [
        r
        for r in rows
        if r.get("routing", {}).get("classification")
        not in {"baseline_contamination", "baseline_routing_unknown"}
        and not (
            condition == "skill" and r.get("routing", {}).get("classification") != "true_activation"
        )
    ]
    successful = [r for r in valid_rows if r.get("grade", {}).get("success")]
    return {
        "runs": len(rows),
        "valid_runs": len(valid_rows),
        "invalid_runs": len(rows) - len(valid_rows),
        "successes": len(successful),
        "success_rate": _safe_rate(len(successful), len(valid_rows)),
        "median_success_total_tokens": _median(
            [r.get("cost", {}).get("usage", {}).get("total_tokens", 0) for r in successful]
        ),
        "median_success_wall_time_ms": _median(
            [r.get("cost", {}).get("wall_time_ms", 0) for r in successful]
        ),
        "median_success_tool_calls": _median(
            [r.get("cost", {}).get("tool_calls", 0) for r in successful]
        ),
        "median_success_commands": _median(
            [r.get("cost", {}).get("commands", 0) for r in successful]
        ),
        "median_success_external_operations": _median(
            [r.get("cost", {}).get("external_operations", 0) for r in successful]
        ),
    }


def summarize_case_results(
    results: list[dict[str, Any]], routing_targets: dict[str, Any] | None = None
) -> dict[str, Any]:
    routing_all = [r for r in results if r.get("suite") == "routing"]
    routing = [r for r in routing_all if r.get("routing", {}).get("known")]
    expected_pos = [r for r in routing if r.get("expected_activation") is True]
    expected_neg = [r for r in routing if r.get("expected_activation") is False]
    activated = [r for r in routing if r.get("routing", {}).get("actual_activation") is True]
    true_activated = [r for r in activated if r.get("expected_activation") is True]
    misses = [r for r in expected_pos if r.get("routing", {}).get("actual_activation") is False]
    false_acts = [r for r in expected_neg if r.get("routing", {}).get("actual_activation") is True]
    neighbor_neg = [r for r in expected_neg if r.get("routing_group") == "neighbor"]
    neighbor_false = [
        r for r in neighbor_neg if r.get("routing", {}).get("actual_activation") is True
    ]

    precision = _safe_rate(len(true_activated), len(activated))
    recall = _safe_rate(len(expected_pos) - len(misses), len(expected_pos))
    neighbor_far = _safe_rate(len(neighbor_false), len(neighbor_neg))
    routing_summary: dict[str, Any] = {
        "runs": len(routing_all),
        "known_runs": len(routing),
        "unknown_runs": len(routing_all) - len(routing),
        "precision": precision,
        "recall": recall,
        "routing_misses": len(misses),
        "false_activations": len(false_acts),
        "neighbor_false_activation_rate": neighbor_far,
    }
    if routing_targets:
        checks = {
            "precision": precision is not None
            and precision >= float(routing_targets.get("precision_min", 0.0)),
            "recall": recall is not None
            and recall >= float(routing_targets.get("recall_min", 0.0)),
            "neighbor_false_activation": neighbor_far is not None
            and neighbor_far <= float(routing_targets.get("neighbor_false_activation_max", 1.0)),
            "native_evidence_complete": len(routing_all) > 0 and len(routing_all) == len(routing),
        }
        unique_positive = {
            r.get("case_id") for r in routing_all if r.get("routing_group") == "positive"
        }
        unique_negative = {
            r.get("case_id") for r in routing_all if r.get("routing_group") == "negative"
        }
        unique_neighbor = {
            r.get("case_id") for r in routing_all if r.get("routing_group") == "neighbor"
        }
        evaluable = (
            len(unique_positive) >= 20 and len(unique_negative) >= 20 and len(unique_neighbor) >= 10
        )
        routing_summary["target_checks"] = checks
        routing_summary["targets_evaluable"] = evaluable
        routing_summary["targets_pass"] = all(checks.values()) if evaluable else None

    suites: dict[str, Any] = {}
    for suite in ("task", "recovery"):
        skill = _suite_condition_metrics(results, suite, "skill")
        baseline = _suite_condition_metrics(results, suite, "baseline")
        uplift = None
        if isinstance(skill["success_rate"], (int, float)) and isinstance(
            baseline["success_rate"], (int, float)
        ):
            uplift = float(skill["success_rate"]) - float(baseline["success_rate"])
        resource_delta = {}
        for key in (
            "median_success_total_tokens",
            "median_success_wall_time_ms",
            "median_success_tool_calls",
            "median_success_commands",
            "median_success_external_operations",
        ):
            resource_delta[key] = _resource_delta(skill.get(key), baseline.get(key))
        suites[suite] = {
            "skill": skill,
            "baseline": baseline,
            "absolute_success_uplift": uplift,
            "relative_error_reduction": _relative_error_reduction(
                skill.get("success_rate"), baseline.get("success_rate")
            ),
            "successful_run_resource_delta": resource_delta,
        }

    return {
        "routing": routing_summary,
        "task": suites["task"],
        "recovery": suites["recovery"],
        "task_failures": sum(
            1
            for r in results
            if r.get("suite") == "task"
            and r.get("condition") == "skill"
            and not r.get("grade", {}).get("success")
        ),
        "recovery_failures": sum(
            1
            for r in results
            if r.get("suite") == "recovery"
            and r.get("condition") == "skill"
            and not r.get("grade", {}).get("success")
        ),
        "baseline_contaminations": sum(
            1
            for r in results
            if r.get("routing", {}).get("classification") == "baseline_contamination"
        ),
    }
