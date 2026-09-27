#!/usr/bin/env python3
"""Validate structural and verdict invariants for an audit report.

This deliberately checks only mechanically decidable properties. It cannot validate
scientific truth, evidence quality, or whether the chosen statistical method is sound.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HEADINGS = [
    "Claim Under Review",
    "Protocol Reconstruction",
    "Claim-Ladder Verdict",
    "Information Boundary",
    "Mechanism Identification and Controls",
    "Statistical Validity",
    "Benchmark and Economic Integrity",
    "Contamination and Provenance",
    "Calibration and Adaptation",
    "Forward-Test Readiness",
    "Degrees-of-Freedom Freeze Matrix",
    "Findings Register",
    "Required Changes Before Start",
    "Falsification Conditions",
    "Final Verdict",
]

VERDICTS = {
    "READY TO PREREGISTER",
    "CONDITIONALLY READY",
    "NOT IDENTIFIABLE",
    "NOT FALSIFIABLE",
    "BLOCKED BY MISSING EVIDENCE",
}

STATUS_TOKENS = {"DOCUMENTED", "INFERRED", "MISSING"}
SEVERITY_TOKENS = {"BLOCKER", "MAJOR", "MINOR", "NOTE"}
DISPOSITION_TOKENS = {"FROZEN", "RANDOMIZED", "GOVERNED", "OPEN"}


def section(text: str, heading: str) -> str:
    match = re.search(rf"(?ms)^#+\s+{re.escape(heading)}\s*$\n(.*?)(?=^#+\s+|\Z)", text)
    return match.group(1).strip() if match else ""


def error(message: str, errors: list[str]) -> None:
    errors.append(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()

    try:
        text = args.report.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read {args.report}: {exc}", file=sys.stderr)
        return 2

    errors: list[str] = []

    positions = []
    for heading in HEADINGS:
        m = re.search(rf"(?m)^#+\s+{re.escape(heading)}\s*$", text)
        if not m:
            error(f"missing required heading: {heading}", errors)
        else:
            positions.append((heading, m.start()))
    if positions and [p for _, p in positions] != sorted(p for _, p in positions):
        error("required headings are not in contract order", errors)

    final = section(text, "Final Verdict")
    found_verdicts = [v for v in VERDICTS if re.search(rf"\b{re.escape(v)}\b", final)]
    if len(found_verdicts) != 1:
        error("Final Verdict must contain exactly one allowed verdict token", errors)
        verdict = None
    else:
        verdict = found_verdicts[0]

    findings = section(text, "Findings Register")
    if not re.search(r"\bID-[0-9]{2,}\b", findings):
        error("Findings Register must contain at least one stable ID such as ID-01", errors)
    if not any(token in findings for token in STATUS_TOKENS):
        error("Findings Register lacks DOCUMENTED/INFERRED/MISSING status", errors)
    if not any(token in findings for token in SEVERITY_TOKENS):
        error("Findings Register lacks BLOCKER/MAJOR/MINOR/NOTE severity", errors)

    freeze = section(text, "Degrees-of-Freedom Freeze Matrix")
    if not any(token in freeze for token in DISPOSITION_TOKENS):
        error("Freeze Matrix lacks FROZEN/RANDOMIZED/GOVERNED/OPEN dispositions", errors)

    falsification = section(text, "Falsification Conditions")
    if len(re.findall(r"\w+", falsification)) < 8:
        error(
            "Falsification Conditions is empty or too short to state a concrete failure rule",
            errors,
        )

    blockers = bool(re.search(r"\bBLOCKER\b", findings))
    open_items = bool(re.search(r"\bOPEN\b", freeze))
    missing = bool(re.search(r"\bMISSING\b", findings))

    if verdict == "READY TO PREREGISTER":
        if blockers:
            error("READY TO PREREGISTER is inconsistent with a BLOCKER finding", errors)
        if open_items:
            error("READY TO PREREGISTER is inconsistent with an OPEN freeze item", errors)
        if missing:
            error(
                "READY TO PREREGISTER is inconsistent with a MISSING load-bearing finding", errors
            )

    if errors:
        for item in errors:
            print(f"FAIL: {item}")
        return 1

    print("PASS: report satisfies mechanical structure/verdict checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
