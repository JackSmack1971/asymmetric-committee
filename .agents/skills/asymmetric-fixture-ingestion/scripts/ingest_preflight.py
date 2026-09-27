#!/usr/bin/env python3
"""Read-only structural preflight for fixture-backed ingestion work."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


def cmd(repo: Path, *argv: str) -> dict[str, object]:
    try:
        cp = subprocess.run(argv, cwd=repo, text=True, capture_output=True, check=False)
        return {
            "command": " ".join(argv),
            "returncode": cp.returncode,
            "stdout": cp.stdout.strip(),
            "stderr": cp.stderr.strip(),
        }
    except OSError as exc:
        return {
            "command": " ".join(argv),
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
        }


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def has(src: str, pat: str) -> bool:
    return bool(re.search(pat, src, re.M))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", help="Repository root (default: cwd)")
    ns = parser.parse_args()
    repo = Path(ns.repo).resolve()

    paths = {
        "http": repo / "ingest" / "http.py",
        "edgar_client": repo / "ingest" / "edgar_client.py",
        "backfill": repo / "ingest" / "backfill.py",
        "tests_ingest": repo / "tests" / "ingest",
        "fixtures_http": repo / "tests" / "fixtures" / "http",
        "backfill_smoke": repo / "tests" / "ingest" / "test_backfill_smoke.py",
        "makefile": repo / "Makefile",
    }

    http = read(paths["http"])
    edgar = read(paths["edgar_client"])
    backfill = read(paths["backfill"])
    smoke = read(paths["backfill_smoke"])

    fixture_contract = (
        has(http, r"class\s+ReplayTransport")
        and has(http, r"class\s+RecordingTransport")
        and has(http, r"FixtureMissingError")
        and "SECRET_PARAMS" in http
    )
    limiter_contract = (
        has(edgar, r"class\s+RedisSlidingWindowLimiter")
        and has(edgar, r"class\s+LocalSlidingWindowLimiter")
        and "Retry-After" in edgar
    )
    replay_contract = (
        '"--replay"' in backfill
        and "ReplayTransport" in backfill
        and "REDIS_URL is required" in backfill
    )
    smoke_contract = (
        "test_backfill_is_idempotent" in smoke
        and "available_at" in smoke
        and "feed_health" in smoke
    )

    fixture_files = []
    if paths["fixtures_http"].is_dir():
        fixture_files = sorted(
            str(p.relative_to(paths["fixtures_http"]))
            for p in paths["fixtures_http"].rglob("*")
            if p.is_file()
        )

    status = cmd(repo, "git", "status", "--short")
    head = cmd(repo, "git", "rev-parse", "HEAD")

    required = {
        k: p.exists()
        for k, p in paths.items()
    }

    report = {
        "repo": str(repo),
        "required_paths": required,
        "contracts": {
            "record_replay_transport_detected": fixture_contract,
            "shared_edgar_limiter_and_retry_detected": limiter_contract,
            "offline_replay_path_detected": replay_contract,
            "smoke_idempotency_and_temporal_checks_detected": smoke_contract,
        },
        "fixture_file_count": len(fixture_files),
        "git": {
            "head": head,
            "status": status,
            "dirty": bool(status.get("stdout")),
        },
    }

    ok = all(required.values()) and all(report["contracts"].values())
    report["structural_preflight"] = "PASS" if ok else "FAIL"
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
