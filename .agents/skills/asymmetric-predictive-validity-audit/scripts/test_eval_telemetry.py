#!/usr/bin/env python3
"""Offline regression tests for eval telemetry parsing, routing, cost, and aggregation."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from eval_telemetry import (
    parse_codex_trace,
    parse_otel_metrics,
    routing_classification,
    summarize_case_results,
)
from run_skill_evals import prompt_for_condition


def main() -> int:
    otel = {
        "resourceMetrics": [
            {
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "codex.thread.started",
                                "sum": {"dataPoints": [{"asInt": "1"}]},
                            },
                            {
                                "name": "codex.skill.injected",
                                "sum": {
                                    "dataPoints": [
                                        {
                                            "asInt": "1",
                                            "attributes": [
                                                {
                                                    "key": "skill",
                                                    "value": {
                                                        "stringValue": "asymmetric-predictive-validity-audit"
                                                    },
                                                },
                                                {"key": "status", "value": {"stringValue": "ok"}},
                                                {
                                                    "key": "invoke_type",
                                                    "value": {"stringValue": "implicit"},
                                                },
                                                {
                                                    "key": "model_slug",
                                                    "value": {"stringValue": "gpt-test"},
                                                },
                                            ],
                                        }
                                    ]
                                },
                            },
                        ]
                    }
                ]
            }
        ]
    }
    parsed = parse_otel_metrics([otel])
    assert parsed["metrics_live"] is True
    assert parsed["target_injected"] is True
    assert parsed["injections"][0]["invoke_type"] == "implicit"
    assert routing_classification(True, parsed)["classification"] == "true_activation"
    assert routing_classification(False, parsed)["classification"] == "false_activation"
    live_no_injection = parse_otel_metrics(
        [{"resourceMetrics": [{"scopeMetrics": [{"metrics": [{"name": "codex.thread.started"}]}]}]}]
    )
    assert routing_classification(True, live_no_injection)["classification"] == "routing_miss"
    assert (
        routing_classification(False, live_no_injection)["classification"] == "true_non_activation"
    )
    assert (
        routing_classification(False, {"metrics_live": False, "target_injected": False})[
            "classification"
        ]
        == "unknown"
    )

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "trace.jsonl"
        rows = [
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "python check.py", "exit_code": 1},
            },
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "status": "completed"}},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "output_tokens": 30,
                    "reasoning_output_tokens": 10,
                },
            },
        ]
        p.write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        trace = parse_codex_trace(p)
        assert trace["commands"] == 1
        assert trace["failed_commands"] == 1
        assert trace["tool_calls"] == 2
        assert trace["external_operations"] == 1
        assert trace["usage"]["total_tokens"] == 140

    summary = summarize_case_results(
        [
            {
                "suite": "routing",
                "routing_group": "positive",
                "expected_activation": True,
                "routing": {"known": True, "actual_activation": False},
            },
            {
                "suite": "routing",
                "routing_group": "negative",
                "expected_activation": False,
                "routing": {"known": True, "actual_activation": True},
            },
            {
                "suite": "task",
                "condition": "skill",
                "routing": {"classification": "true_activation"},
                "grade": {"success": True},
                "cost": {
                    "usage": {"total_tokens": 100},
                    "wall_time_ms": 10,
                    "tool_calls": 2,
                    "commands": 1,
                    "external_operations": 0,
                },
            },
            {
                "suite": "task",
                "condition": "baseline",
                "routing": {"classification": "baseline_no_target_skill"},
                "grade": {"success": False},
                "cost": {
                    "usage": {"total_tokens": 80},
                    "wall_time_ms": 8,
                    "tool_calls": 1,
                    "commands": 1,
                    "external_operations": 0,
                },
            },
            {
                "suite": "recovery",
                "condition": "skill",
                "routing": {"classification": "true_activation"},
                "grade": {"success": True},
                "cost": {
                    "usage": {"total_tokens": 120},
                    "wall_time_ms": 12,
                    "tool_calls": 2,
                    "commands": 2,
                    "external_operations": 0,
                },
            },
            {
                "suite": "recovery",
                "condition": "baseline",
                "routing": {"classification": "baseline_no_target_skill"},
                "grade": {"success": False},
                "cost": {
                    "usage": {"total_tokens": 90},
                    "wall_time_ms": 9,
                    "tool_calls": 1,
                    "commands": 1,
                    "external_operations": 0,
                },
            },
        ],
        {"precision_min": 0.9, "recall_min": 0.9, "neighbor_false_activation_max": 0.15},
    )
    assert summary["routing"]["routing_misses"] == 1
    assert summary["routing"]["false_activations"] == 1
    assert summary["routing"]["targets_evaluable"] is False
    assert summary["routing"]["targets_pass"] is None
    assert summary["task"]["absolute_success_uplift"] == 1.0
    assert summary["task"]["relative_error_reduction"] == 1.0
    assert summary["recovery"]["absolute_success_uplift"] == 1.0
    assert (
        summary["task"]["successful_run_resource_delta"]["median_success_total_tokens"]["absolute"]
        is None
    )
    case = {"suite": "task", "prompt": "Neutral audit task"}
    assert prompt_for_condition(case, "baseline") == "Neutral audit task"
    assert prompt_for_condition(case, "skill").startswith("$asymmetric-predictive-validity-audit\n")
    routing_case = {"suite": "routing", "prompt": "Implicit routing prompt"}
    assert prompt_for_condition(routing_case, "skill") == "Implicit routing prompt"
    print("PASS: eval telemetry regression tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
