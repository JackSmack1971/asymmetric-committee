#!/usr/bin/env python3
"""Recompute aggregate telemetry from saved per-case result.json artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval_telemetry import read_json, summarize_case_results

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evals" / "corpus.json"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_dir", type=Path)
    p.add_argument(
        "--write", action="store_true", help="Overwrite summary.json with the recomputed summary."
    )
    args = p.parse_args()
    results = [read_json(path) for path in sorted(args.run_dir.rglob("result.json"))]
    if not results:
        print("No result.json files found.")
        return 2
    corpus = read_json(CORPUS)
    summary = summarize_case_results(results, corpus.get("routing_targets", {}))
    manifest = args.run_dir / "manifest.json"
    if manifest.exists():
        meta = read_json(manifest)
        summary["eval_run_id"] = meta.get("eval_run_id")
        summary["codex_version"] = meta.get("codex_version")
        summary["production_fingerprint"] = meta.get("production_fingerprint")
    summary["results"] = len(results)
    text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.write:
        (args.run_dir / "summary.json").write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
