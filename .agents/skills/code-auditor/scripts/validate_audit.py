#!/usr/bin/env python3
"""Validate code-auditor's durable audit.json evidence contract.

This validates structure and internal verdict consistency only. It does not verify
that cited evidence is true or that the audited code is correct.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PLACEHOLDERS = {"tbd", "todo", "n/a", "na", "unknown", "placeholder"}
SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}
CATEGORIES = {
    "correctness", "security", "maintainability", "performance",
    "testing", "operability", "architecture", "compliance",
}
VERIFICATION_STATUS = {"PASSED", "FAILED", "NOT_RUN"}
FRAMEWORK_STATUS = {"SUPPORTED", "PARTIAL", "UNSUPPORTED", "NOT_ASSESSED"}
VERDICTS = {"PASS", "RED", "INCONCLUSIVE"}
MODES = {"change", "repository", "claim"}
SCHEMA_VERSIONS = {"1.0", "1.1"}
REVIEW_STATUS = {"CONFIRMED", "DOWNGRADE", "REJECT", "INSUFFICIENT_EVIDENCE"}


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.strip().lower() not in PLACEHOLDERS


def string_list(value: Any) -> bool:
    return isinstance(value, list) and all(nonempty_string(v) for v in value)


def validate(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["root must be a JSON object"]

    schema_version = data.get("schema_version")
    if schema_version not in SCHEMA_VERSIONS:
        fail(errors, f"schema_version must be one of {sorted(SCHEMA_VERSIONS)}")

    scope = data.get("scope")
    if not isinstance(scope, dict):
        fail(errors, "scope must be an object")
    else:
        if scope.get("mode") not in MODES:
            fail(errors, "scope.mode must be change, repository, or claim")
        if not string_list(scope.get("targets")) or not scope.get("targets"):
            fail(errors, "scope.targets must be a non-empty list of concrete strings")
        baseline = scope.get("baseline")
        if baseline is not None and not nonempty_string(baseline):
            fail(errors, "scope.baseline must be null or a concrete string")

    findings = data.get("findings")
    if not isinstance(findings, list):
        fail(errors, "findings must be a list")
        findings = []
    seen_ids: set[str] = set()
    blocking = False
    for idx, finding in enumerate(findings):
        p = f"findings[{idx}]"
        if not isinstance(finding, dict):
            fail(errors, f"{p} must be an object")
            continue
        fid = finding.get("id")
        if not nonempty_string(fid):
            fail(errors, f"{p}.id must be a concrete string")
        elif fid in seen_ids:
            fail(errors, f"{p}.id duplicates {fid}")
        else:
            seen_ids.add(fid)
        severity = finding.get("severity")
        if severity not in SEVERITIES:
            fail(errors, f"{p}.severity must be one of {sorted(SEVERITIES)}")
        if severity in {"CRITICAL", "HIGH"}:
            blocking = True
        if finding.get("confidence") not in CONFIDENCE:
            fail(errors, f"{p}.confidence must be one of {sorted(CONFIDENCE)}")
        if finding.get("category") not in CATEGORIES:
            fail(errors, f"{p}.category must be one of {sorted(CATEGORIES)}")
        for field in ("location", "claim", "impact", "remediation"):
            if not nonempty_string(finding.get(field)):
                fail(errors, f"{p}.{field} must be a concrete non-placeholder string")
        evidence = finding.get("evidence")
        if not string_list(evidence) or not evidence:
            fail(errors, f"{p}.evidence must contain at least one concrete evidence reference")

    verification = data.get("verification")
    if not isinstance(verification, list):
        fail(errors, "verification must be a list")
        verification = []
    for idx, check in enumerate(verification):
        p = f"verification[{idx}]"
        if not isinstance(check, dict):
            fail(errors, f"{p} must be an object")
            continue
        if not nonempty_string(check.get("command")):
            fail(errors, f"{p}.command must be a concrete string")
        status = check.get("status")
        if status not in VERIFICATION_STATUS:
            fail(errors, f"{p}.status must be one of {sorted(VERIFICATION_STATUS)}")
        exit_code = check.get("exit_code")
        if status == "NOT_RUN":
            if exit_code is not None:
                fail(errors, f"{p}.exit_code must be null when status is NOT_RUN")
        elif not isinstance(exit_code, int):
            fail(errors, f"{p}.exit_code must be an integer for executed checks")
        notes = check.get("notes")
        if notes is not None and not nonempty_string(notes):
            fail(errors, f"{p}.notes must be null or a concrete string")

    framework_claims = data.get("framework_claims")
    if not isinstance(framework_claims, list):
        fail(errors, "framework_claims must be a list")
        framework_claims = []
    for idx, claim in enumerate(framework_claims):
        p = f"framework_claims[{idx}]"
        if not isinstance(claim, dict):
            fail(errors, f"{p} must be an object")
            continue
        for field in ("framework", "control"):
            if not nonempty_string(claim.get(field)):
                fail(errors, f"{p}.{field} must be a concrete string")
        if claim.get("status") not in FRAMEWORK_STATUS:
            fail(errors, f"{p}.status must be one of {sorted(FRAMEWORK_STATUS)}")
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not all(nonempty_string(v) for v in evidence):
            fail(errors, f"{p}.evidence must be a list of concrete strings")

    finding_reviews = data.get("finding_reviews")
    if finding_reviews is not None:
        if schema_version != "1.1":
            fail(errors, "finding_reviews requires schema_version '1.1'")
        if not isinstance(finding_reviews, list):
            fail(errors, "finding_reviews must be a list when present")
        else:
            for idx, review in enumerate(finding_reviews):
                p = f"finding_reviews[{idx}]"
                if not isinstance(review, dict):
                    fail(errors, f"{p} must be an object")
                    continue
                finding_id = review.get("finding_id")
                if not nonempty_string(finding_id):
                    fail(errors, f"{p}.finding_id must be a concrete string")
                elif finding_id not in seen_ids:
                    fail(errors, f"{p}.finding_id must reference an existing finding")
                if not nonempty_string(review.get("reviewer_role")):
                    fail(errors, f"{p}.reviewer_role must be a concrete string")
                if review.get("status") not in REVIEW_STATUS:
                    fail(errors, f"{p}.status must be one of {sorted(REVIEW_STATUS)}")
                if not nonempty_string(review.get("rationale")):
                    fail(errors, f"{p}.rationale must be a concrete string")
                evidence = review.get("evidence")
                if not string_list(evidence) or not evidence:
                    fail(errors, f"{p}.evidence must contain at least one concrete evidence reference")
                unresolved = review.get("unresolved_assumptions", [])
                if not string_list(unresolved):
                    fail(errors, f"{p}.unresolved_assumptions must be a list of concrete strings")

    blockers = data.get("blockers")
    if not string_list(blockers):
        fail(errors, "blockers must be a list of concrete strings")
        blockers = []

    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        fail(errors, f"verdict must be one of {sorted(VERDICTS)}")
    else:
        if verdict == "PASS" and blocking:
            fail(errors, "PASS is inconsistent with CRITICAL/HIGH findings")
        if verdict == "PASS" and blockers:
            fail(errors, "PASS is inconsistent with non-empty blockers")
        if verdict == "RED" and not blocking and not any(
            isinstance(c, dict) and c.get("status") == "UNSUPPORTED" for c in framework_claims
        ) and not any(
            isinstance(v, dict) and v.get("status") == "FAILED" for v in verification
        ):
            fail(errors, "RED requires a blocking finding, failed verification, or unsupported framework claim")

    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: validate_audit.py AUDIT_JSON", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    errors = validate(data)
    if errors:
        print("INVALID")
        for error in errors:
            print(f"- {error}")
        return 1
    print("VALID: audit evidence contract satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
