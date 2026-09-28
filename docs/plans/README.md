# Phase plans

Write or update `P<n>.md` before implementation. Keep the plan in `docs/plans/` and reconcile it against the blueprint (§17), current code, `docs/PROGRESS.md`, repository instructions, and import-linter contracts.

Every plan must contain these sections:

1. **Scope** — the phase deliverable, explicit exclusions, and blueprint section references.
2. **Inspected contracts** — relevant public models, persistence/API shapes, module boundaries, import-linter contracts, and existing behavior inspected before proposing changes. Name the files or symbols.
3. **Invariants touched** — applicable repository invariants and how each will be preserved.
4. **Boundary checks** — upstream/downstream consumers, forbidden imports or writes, generated/source-of-truth artifacts, and compatibility or migration implications checked.
5. **Owner decisions** — every unresolved product or architecture choice as a concrete question. If none, say `None`; do not silently choose on the owner's behalf.
6. **Steps** — ordered, reviewable implementation steps; include contract/schema changes before consumers where relevant.
7. **Exact verification** — commands or the `scripts/verify_local.py P<n>` entrypoint, exact test scope, required services, and what each check proves. State known skips and external prerequisites.
8. **Spec issues** — ambiguities or contradictions, with the proposed blueprint resolution; if none, say `None`.
9. **Requirement traceability** — for each explicit requirement, identify its source, design choice, failure behavior, and verification. List unresolved owner decisions separately from settled choices.

## Revising a plan

Revise the existing plan in place. Do not append a revision block or retain superseded instructions as if they were current. After each revision, reconcile the whole document: search for every removed, renamed, or changed concept and update dependent sections, examples, schemas, tests, and verification commands. Remove obsolete wording, then read the complete revised plan and check that scope, steps, boundaries, decisions, and verification agree. Record material resolved decisions in `docs/PROGRESS.md` when implementation proceeds.

Before resubmitting a revised plan, state the revision delta during review, identify which sections changed and which concepts were superseded, then reread the complete plan and check it for stale or contradictory wording. Do not treat an owner correction as approval; the exact approval rule below still applies.

When a change adds or renames a configured module or scheduled task, update its inventory-bearing fixture or required-task assertion in the same implementation slice. Keep synthetic import-linter trees derived from `pyproject.toml` rather than maintaining a second package/module inventory.

For goal text, run `python scripts/preflight_goal.py` with the proposed text on stdin before submission. The script reports only the character count and status. Keep goals at or below 3,500 characters; submissions above 4,000 are blocked. After complex scripted Python edits, run `python scripts/check_python_edits.py <file.py> [...]` on the explicit targets before broader verification.

If memory is constrained, run memory-heavy validation commands serially. A terminated or skipped check is unknown or partial evidence until the required check completes. Claim TimescaleDB coverage only when the TimescaleDB-backed check ran; the P5 verifier requires the Timescale service.

## Approval and implementation

Implementation begins only when the owner's entire response is exactly `APPROVED`, apart from surrounding whitespace. Corrections, qualifications, conditions, questions, or additional requests mean the plan must be revised in place and resubmitted; they are not approval. Do not start implementation while a revised plan is awaiting approval.

Phase completion requires a passing result from `python scripts/verify_local.py P<n>` (or its Make compatibility alias) and an update to `docs/PROGRESS.md`. A blocked or failed result is not a passing gate. Never describe an unimplemented gate as passing.
