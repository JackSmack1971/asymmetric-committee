#!/usr/bin/env python3
"""Run the packaged Codex skill evaluation corpus and record evidence-backed telemetry.

Routing uses Codex's native `codex.skill.injected` OTel metric. Task/recovery
success is graded from report artifacts and deterministic report invariants.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from eval_telemetry import (
    TARGET_SKILL,
    append_jsonl,
    grade_report,
    parse_codex_trace,
    parse_otel_metrics,
    read_json,
    routing_classification,
    summarize_case_results,
)

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evals" / "corpus.json"
VALIDATOR = ROOT / "scripts" / "validate_audit_report.py"
SCHEMA = "apva.eval.telemetry.v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def event(
    run_id: str,
    name: str,
    *,
    case: dict[str, Any] | None = None,
    condition: str | None = None,
    repeat: int | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "event": name,
        "timestamp": now(),
        "eval_run_id": run_id,
        "case_id": case.get("id") if case else None,
        "suite": case.get("suite") if case else None,
        "condition": condition,
        "repeat": repeat,
        "data": data or {},
    }


class _Collector:
    def __init__(self) -> None:
        self.payloads: list[Any] = []
        self.raw_requests: list[dict[str, Any]] = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0") or 0)
                body = self.rfile.read(length)
                if self.headers.get("content-encoding", "").lower() == "gzip":
                    try:
                        body = gzip.decompress(body)
                    except OSError:
                        pass
                content_type = self.headers.get("content-type", "")
                rec: dict[str, Any] = {
                    "path": self.path,
                    "content_type": content_type,
                    "bytes": len(body),
                }
                try:
                    payload = json.loads(body.decode("utf-8"))
                    collector.payloads.append(payload)
                    rec["json"] = True
                except Exception:
                    rec["json"] = False
                    rec["sha256"] = hashlib.sha256(body).hexdigest()
                collector.raw_requests.append(rec)
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1/metrics"

    def __enter__(self) -> "_Collector":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def production_paths() -> list[Path]:
    """Files staged for the evaluated skill; exclude eval answers/telemetry machinery."""
    paths: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if rel.parts[0] in {"evals", "telemetry"}:
            continue
        if rel.as_posix() == "references/evaluation-protocol.md":
            continue
        if rel.as_posix() in {
            "scripts/check_package.py",
            "scripts/run_skill_evals.py",
            "scripts/score_eval_run.py",
            "scripts/eval_telemetry.py",
            "scripts/test_eval_telemetry.py",
        }:
            continue
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        paths.append(path)
    return sorted(paths)


def production_fingerprint() -> str:
    h = hashlib.sha256()
    for path in production_paths():
        rel = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        h.update(rel.encode())
        h.update(b"\0")
        h.update(hashlib.sha256(data).hexdigest().encode())
        h.update(b"\n")
    return h.hexdigest()


def stage_skill(workspace: Path, enabled: bool) -> Path | None:
    if not enabled:
        return None
    dest = workspace / ".agents" / "skills" / TARGET_SKILL
    for src in production_paths():
        rel = src.relative_to(ROOT)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    return dest


def materialize_case(workspace: Path, case: dict[str, Any]) -> None:
    for rel, content in case.get("workspace_files", {}).items():
        path = workspace / str(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")


def codex_version(codex: str) -> str | None:
    try:
        proc = subprocess.run([codex, "--version"], text=True, capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or proc.stderr.strip() or None


def env_for_eval(temp_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    original_home = Path.home()
    original_codex_home = Path(env.get("CODEX_HOME") or (original_home / ".codex"))
    temp_home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(temp_home)
    if os.name == "nt":
        env["USERPROFILE"] = str(temp_home)
    env["CODEX_HOME"] = str(original_codex_home)
    return env


def init_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    git = shutil.which("git")
    if git:
        subprocess.run(
            [git, "init", "-q"], cwd=workspace, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )


def run_validator(report: Path) -> tuple[int | None, str]:
    if not report.exists():
        return None, "report missing"
    try:
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR), str(report)],
            text=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def case_conditions(case: dict[str, Any], requested: str) -> list[str]:
    if case["suite"] == "routing":
        return ["skill"]
    if requested == "both":
        return ["skill", "baseline"]
    return [requested]


def repeats_for(case: dict[str, Any], corpus: dict[str, Any], args: argparse.Namespace) -> int:
    if args.repeats is not None:
        return args.repeats
    if not args.protocol_repeats:
        return 1
    key = f"{case['suite']}_repeats"
    return int(corpus.get("protocol", {}).get(key, 1))


def prompt_for_condition(case: dict[str, Any], condition: str) -> str:
    """Keep routing implicit; explicitly apply the skill only in value/recovery treatment arms."""
    prompt = str(case["prompt"])
    if case["suite"] in {"task", "recovery"} and condition == "skill":
        return f"${TARGET_SKILL}\n\n{prompt}"
    return prompt


def routing_for_condition(
    case: dict[str, Any], condition: str, otel: dict[str, Any]
) -> dict[str, Any]:
    """Classify routing only when target activation is part of the experimental condition."""
    if case["suite"] == "routing":
        return routing_classification(bool(case.get("expected_activation")), otel)
    if condition == "skill":
        # Task/recovery treatment is explicit. A missing injection means the treatment did not apply.
        return routing_classification(True, otel)
    if not otel.get("metrics_live"):
        return {
            "known": False,
            "actual_activation": None,
            "classification": "baseline_routing_unknown",
        }
    if otel.get("target_injected"):
        return {
            "known": True,
            "actual_activation": True,
            "classification": "baseline_contamination",
        }
    return {"known": True, "actual_activation": False, "classification": "baseline_no_target_skill"}


def config_overrides(endpoint: str) -> list[str]:
    # Current Codex config supports OTLP/HTTP JSON metrics export. Keep logs/traces disabled;
    # this harness needs native metric liveness + skill.injected only.
    metric = (
        f'otel.metrics_exporter={{ otlp-http = {{ endpoint = "{endpoint}", protocol = "json" }} }}'
    )
    return [
        "-c",
        metric,
        "-c",
        'otel.exporter="none"',
        "-c",
        'otel.trace_exporter="none"',
        "-c",
        "otel.log_user_prompt=false",
    ]


def run_one(
    case: dict[str, Any],
    condition: str,
    repeat: int,
    run_dir: Path,
    codex: str,
    model: str | None,
    eval_home: Path,
) -> dict[str, Any]:
    leaf = run_dir / "cases" / case["id"] / condition / f"repeat-{repeat:02d}"
    leaf.mkdir(parents=True, exist_ok=True)
    workspace = leaf / "workspace"
    init_workspace(workspace)
    staged = stage_skill(workspace, enabled=condition == "skill")
    materialize_case(workspace, case)

    trace_path = leaf / "trace.jsonl"
    final_path = leaf / "final.txt"
    stderr_path = leaf / "stderr.txt"
    report_path = workspace / "report.md"

    env = env_for_eval(eval_home)
    with _Collector() as collector:
        cmd = [
            codex,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--cd",
            str(workspace),
            "--output-last-message",
            str(final_path),
        ]
        if case["suite"] == "routing":
            cmd += ["--sandbox", "read-only"]
        else:
            cmd += ["--sandbox", "workspace-write"]
        if model:
            cmd += ["--model", model]
        cmd += config_overrides(collector.endpoint)
        cmd += [prompt_for_condition(case, condition)]

        started = time.perf_counter()
        try:
            with (
                trace_path.open("w", encoding="utf-8", newline="\n") as out,
                stderr_path.open("w", encoding="utf-8", newline="\n") as err,
            ):
                proc = subprocess.run(
                    cmd, cwd=workspace, env=env, text=True, stdout=out, stderr=err
                )
            exit_code = proc.returncode
        except OSError as exc:
            exit_code = 127
            stderr_path.write_text(str(exc), encoding="utf-8")
        wall_ms = (time.perf_counter() - started) * 1000.0
        # Codex flushes exporters on shutdown; allow the local HTTP handler a brief scheduling window.
        time.sleep(0.15)
        otel_payloads = copy.deepcopy(collector.payloads)
        otel_requests = copy.deepcopy(collector.raw_requests)

    (leaf / "otel-metrics.json").write_text(
        json.dumps({"requests": otel_requests, "payloads": otel_payloads}, indent=2) + "\n",
        encoding="utf-8",
    )
    trace = parse_codex_trace(trace_path)
    otel = parse_otel_metrics(otel_payloads)
    routing = routing_for_condition(case, condition, otel)
    cost = {
        **trace,
        "wall_time_ms": round(wall_ms, 3),
        "codex_exit_code": exit_code,
    }

    validator_exit: int | None = None
    validator_output = ""
    grade: dict[str, Any] | None = None
    if case["suite"] in {"task", "recovery"}:
        validator_exit, validator_output = run_validator(report_path)
        grade = grade_report(report_path, case.get("grader", {}), validator_exit)
        # A skill-arm result is not valid evidence of skill benefit unless native Codex telemetry
        # proves the target skill was actually injected. A contaminated baseline is likewise invalid.
        if condition == "skill" and routing.get("classification") != "true_activation":
            grade["success"] = False
            grade.setdefault("failures", []).append("skill_treatment_not_observed")
        if condition == "baseline" and routing.get("classification") == "baseline_contamination":
            grade["success"] = False
            grade.setdefault("failures", []).append("baseline_contaminated_by_target_skill")
        (leaf / "validator.txt").write_text(validator_output + "\n", encoding="utf-8")
        if report_path.exists():
            shutil.copy2(report_path, leaf / "report.md")
    else:
        grade = {
            "success": routing.get("known")
            and routing.get("classification") in {"true_activation", "true_non_activation"},
            "failures": [],
        }
        if not routing.get("known"):
            grade["failures"] = ["native_routing_evidence_unavailable"]
        elif routing.get("classification") == "routing_miss":
            grade["failures"] = ["routing_miss"]
        elif routing.get("classification") == "false_activation":
            grade["failures"] = ["false_activation"]

    result = {
        "schema": "apva.eval.case-result.v1",
        "case_id": case["id"],
        "suite": case["suite"],
        "routing_group": case.get("routing_group"),
        "condition": condition,
        "repeat": repeat,
        "expected_activation": case.get("expected_activation"),
        "routing": routing,
        "native_otel": otel,
        "grade": grade,
        "cost": cost,
        "recovery_trigger": case.get("recovery_trigger"),
        "staged_skill_path": str(staged.relative_to(workspace)) if staged else None,
        "artifact_paths": {
            "trace": "trace.jsonl",
            "final": "final.txt",
            "stderr": "stderr.txt",
            "report": "report.md" if (leaf / "report.md").exists() else None,
            "otel": "otel-metrics.json",
        },
    }
    (leaf / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    shutil.rmtree(workspace, ignore_errors=True)
    return result


def select_cases(corpus: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    cases = list(corpus.get("cases", []))
    if args.suite != "all":
        cases = [c for c in cases if c.get("suite") == args.suite]
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c.get("id") in wanted]
    if args.max_cases is not None:
        cases = cases[: args.max_cases]
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run packaged skill evals and record routing/task/recovery/cost telemetry."
    )
    parser.add_argument("--suite", choices=["routing", "task", "recovery", "all"], default="all")
    parser.add_argument("--condition", choices=["skill", "baseline", "both"], default="skill")
    parser.add_argument("--case", action="append", help="Run only this corpus case id; repeatable.")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument(
        "--repeats", type=int, help="Override repeat count for every selected case."
    )
    parser.add_argument(
        "--protocol-repeats",
        action="store_true",
        help="Use corpus protocol repeat counts (routing=3, task=5, recovery=3).",
    )
    parser.add_argument("--model", help="Optional Codex model override.")
    parser.add_argument("--codex", default="codex", help="Codex executable path/name.")
    parser.add_argument(
        "--out", type=Path, help="Output directory. Default: telemetry/runs/<run-id>."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate selection/staging without invoking Codex."
    )
    args = parser.parse_args()

    corpus = read_json(CORPUS)
    cases = select_cases(corpus, args)
    if not cases:
        print("No cases selected.")
        return 2

    version = codex_version(args.codex)
    if not args.dry_run and version is None:
        print(f"ERROR: Codex executable not available: {args.codex}")
        return 2

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run_dir = (args.out or (ROOT / "telemetry" / "runs" / run_id)).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    events = run_dir / "events.jsonl"
    manifest = {
        "schema": "apva.eval.run.v1",
        "eval_run_id": run_id,
        "created_at": now(),
        "skill": TARGET_SKILL,
        "production_fingerprint": production_fingerprint(),
        "corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
        "codex_version": version,
        "requested_model": args.model,
        "suite": args.suite,
        "condition": args.condition,
        "protocol_repeats": args.protocol_repeats,
        "repeats_override": args.repeats,
        "case_ids": [c["id"] for c in cases],
        "routing_evidence": "native_otel_codex.skill.injected",
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    append_jsonl(events, event(run_id, "eval.run.started", data=manifest))

    if args.dry_run:
        with tempfile.TemporaryDirectory(prefix="apva-eval-stage-") as td:
            workspace = Path(td) / "workspace"
            init_workspace(workspace)
            staged = stage_skill(workspace, enabled=True)
            manifest["dry_run_staged_files"] = len(list(staged.rglob("*"))) if staged else 0
            manifest["dry_run_staged_root"] = str(staged) if staged else None
        (run_dir / "summary.json").write_text(
            json.dumps({"dry_run": True, "manifest": manifest}, indent=2) + "\n", encoding="utf-8"
        )
        append_jsonl(events, event(run_id, "eval.run.finished", data={"dry_run": True}))
        print(run_dir)
        return 0

    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="apva-eval-home-") as home_td:
        eval_home = Path(home_td)
        for case in cases:
            for condition in case_conditions(case, args.condition):
                for repeat in range(1, repeats_for(case, corpus, args) + 1):
                    append_jsonl(
                        events,
                        event(
                            run_id,
                            "eval.case.started",
                            case=case,
                            condition=condition,
                            repeat=repeat,
                        ),
                    )
                    result = run_one(
                        case, condition, repeat, run_dir, args.codex, args.model, eval_home
                    )
                    results.append(result)
                    append_jsonl(
                        events,
                        event(
                            run_id,
                            "routing.observed",
                            case=case,
                            condition=condition,
                            repeat=repeat,
                            data=result["routing"],
                        ),
                    )
                    classification = result["routing"].get("classification")
                    if case["suite"] == "routing" and classification == "routing_miss":
                        append_jsonl(
                            events,
                            event(
                                run_id,
                                "routing.miss",
                                case=case,
                                condition=condition,
                                repeat=repeat,
                                data=result["routing"],
                            ),
                        )
                    elif case["suite"] == "routing" and classification == "false_activation":
                        append_jsonl(
                            events,
                            event(
                                run_id,
                                "routing.false_activation",
                                case=case,
                                condition=condition,
                                repeat=repeat,
                                data=result["routing"],
                            ),
                        )
                    elif classification == "baseline_contamination":
                        append_jsonl(
                            events,
                            event(
                                run_id,
                                "evidence.invalid",
                                case=case,
                                condition=condition,
                                repeat=repeat,
                                data={"kind": "baseline_contamination", **result["routing"]},
                            ),
                        )
                    elif not result["routing"].get("known"):
                        append_jsonl(
                            events,
                            event(
                                run_id,
                                "evidence.unavailable",
                                case=case,
                                condition=condition,
                                repeat=repeat,
                                data={"kind": "native_routing_otel"},
                            ),
                        )
                    append_jsonl(
                        events,
                        event(
                            run_id,
                            "cost.observed",
                            case=case,
                            condition=condition,
                            repeat=repeat,
                            data=result["cost"],
                        ),
                    )
                    if case["suite"] == "task":
                        append_jsonl(
                            events,
                            event(
                                run_id,
                                "task.result",
                                case=case,
                                condition=condition,
                                repeat=repeat,
                                data=result["grade"],
                            ),
                        )
                        if not result["grade"].get("success"):
                            append_jsonl(
                                events,
                                event(
                                    run_id,
                                    "task.failure",
                                    case=case,
                                    condition=condition,
                                    repeat=repeat,
                                    data=result["grade"],
                                ),
                            )
                    elif case["suite"] == "recovery":
                        append_jsonl(
                            events,
                            event(
                                run_id,
                                "recovery.result",
                                case=case,
                                condition=condition,
                                repeat=repeat,
                                data=result["grade"],
                            ),
                        )
                        if not result["grade"].get("success"):
                            append_jsonl(
                                events,
                                event(
                                    run_id,
                                    "recovery.failure",
                                    case=case,
                                    condition=condition,
                                    repeat=repeat,
                                    data={
                                        "trigger": case.get("recovery_trigger"),
                                        **result["grade"],
                                    },
                                ),
                            )
                    append_jsonl(
                        events,
                        event(
                            run_id,
                            "eval.case.finished",
                            case=case,
                            condition=condition,
                            repeat=repeat,
                            data={"success": result["grade"].get("success")},
                        ),
                    )
                    print(
                        f"{case['id']} {condition} r{repeat}: {'PASS' if result['grade'].get('success') else 'FAIL'}"
                    )

    summary = summarize_case_results(results, corpus.get("routing_targets", {}))
    summary["eval_run_id"] = run_id
    summary["codex_version"] = version
    summary["production_fingerprint"] = manifest["production_fingerprint"]
    summary["results"] = len(results)
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    append_jsonl(events, event(run_id, "eval.run.finished", data=summary))
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"Telemetry: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
