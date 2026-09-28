#!/usr/bin/env python3
"""Heuristic lint for repository-root AGENTS.md files.

This script checks structural risks that are mechanically detectable. It does not
validate repository facts or determine whether a rule is actually authoritative.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

GENERIC = [
    r"\bwrite clean code\b",
    r"\bfollow best practices\b",
    r"\btest (your work|thoroughly)\b",
    r"\bbe careful\b",
]
TRANSIENT = [
    r"\bthis (branch|issue|ticket|sprint|incident)\b",
    r"\bcurrent (branch|issue|ticket|sprint|incident)\b",
    r"\btoday(?:'s)?\b",
    r"\btemporary(?:ly)?\b",
]
PROCEDURAL = [
    r"\bstep\s+1\b",
    r"\bfirst,?\s+.*\bthen\b",
    r"\bif .* then .* then\b",
]


def lint(text: str) -> list[str]:
    findings: list[str] = []
    lower = text.lower()

    if len(text) > 16000:
        findings.append("WARN: file exceeds 16,000 characters; review always-on context cost")

    for pattern in GENERIC:
        if re.search(pattern, lower):
            findings.append(f"WARN: generic low-information guidance matched: {pattern}")

    for pattern in TRANSIENT:
        if re.search(pattern, lower):
            findings.append(f"WARN: possible transient state matched: {pattern}")

    procedural_hits = sum(bool(re.search(pattern, lower, re.S)) for pattern in PROCEDURAL)
    if procedural_hits >= 2:
        findings.append("WARN: document appears procedure-heavy; consider extracting conditional workflows into skills")

    headings = re.findall(r"(?m)^#{1,6}\s+(.+)$", text)
    if len(headings) > 18:
        findings.append("WARN: unusually many headings for a root guidance file; review for manual/runbook sprawl")

    if "agents.md" not in lower and not headings:
        findings.append("INFO: no headings detected; verify the file remains scannable")

    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()

    if not args.path.is_file():
        print(f"ERROR: file not found: {args.path}", file=sys.stderr)
        return 2

    text = args.path.read_text(encoding="utf-8")
    findings = lint(text)
    if findings:
        print("\n".join(findings))
    else:
        print("PASS: no heuristic AGENTS.md lint findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
