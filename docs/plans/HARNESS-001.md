# HARNESS-001 — Claude Code Harness Friction Controls

## Scope

Add narrowly scoped local controls for the recurring friction documented in `docs/claude-session-friction-report.md`: oversized goal submissions, plan correction churn, brittle synthetic import-linter fixtures, unsafe multiline edit validation, and inaccurate reporting of interrupted or skipped checks. This is standalone repository harness maintenance, not P6.5 or another blueprint phase. Keep `CLAUDE.md` unchanged because the retrospective finds its current goal-length and safe-edit guidance adequate.

## Inspected contracts

- `CLAUDE.md` and `AGENTS.md`: repository authority, phase planning, exact approval rule, goal-length guidance, source-edit guidance, and verification expectations.
- `docs/plans/README.md`: canonical phase-plan structure and in-place revision rules.
- `docs/PROGRESS.md`: current phase state; P6.5 is the next evaluation slice.
- `scripts/verify_local.py`: authoritative phase checks; P5 sets `REQUIRE_TIMESCALE=1` and starts `db`/`redis`.
- `pyproject.toml`: import-linter root packages, contract modules, and Ruff configuration.
- `tests/store/test_import_rules.py`: duplicated synthetic import-linter trees.
- `tests/orchestration/test_tasks.py` and `tests/orchestration/test_ingest_beat.py`: explicit Celery task and feed schedule assertions.
- Retrospective findings F1–F4 and recommendations, including the existing controls for scratch edits and goal length.

## Invariants touched

No application or persisted-data invariant changes. The goal preflight must not echo or persist submitted text. New harness helpers must preserve current import-linter contracts and explicit required-task assertions. Verification results must distinguish PASS, FAIL, BLOCKED, skipped, and interrupted/unknown outcomes.

## Boundary checks

- Keep the preflight and edit validator as standalone standard-library Python scripts; do not add runtime dependencies or alter application APIs.
- Derive synthetic import-linter package and module scaffolding from `pyproject.toml`; retain the existing actual import-linter checks and assertions.
- Keep schedule tests explicit about required task names, with the workflow requiring registration and assertion changes in the same slice.
- Reuse the existing P5 Timescale requirement. Do not broaden service startup or claim Timescale coverage when that service is unavailable.
- Do not modify the user-edited retrospective or `CLAUDE.md`.

## Owner decisions

None. The selected approach is a repository-local goal preflight, a plan-template update, fixture/test maintenance, and a scratch-edit validation command.

## Steps

1. Add `scripts/preflight_goal.py`: read goal text from stdin, report only its character count and status, warn above 3,500 characters, and return a blocking nonzero status above 4,000. Never print or save the text.
2. Extend `docs/plans/README.md` with a compact requirement/source → design choice → failure behavior → verification table and a revision-delta/full-document consistency check.
3. Refactor `tests/store/test_import_rules.py` to use a shared synthetic-tree helper that reads configured root packages and contract modules from `pyproject.toml`. Keep schedule tests’ explicit required-task assertions and add the same-slice update rule to the repository workflow guidance.
4. Add `scripts/check_python_edits.py` accepting explicit Python paths and running `py_compile` plus Ruff against each target, returning failure if either check fails.
5. Add focused tests for both scripts and the fixture helper. Update `docs/PROGRESS.md` with the completed harness decision and actual verification evidence after implementation.

## Exact verification

- Focused pytest for the goal preflight, scratch-edit validator, and import-linter fixture helper, using subprocesses/temp directories where needed.
- Test goal counts below, at, and above 3,500 and 4,000; assert stdin content is never echoed or written.
- Test valid and invalid Python targets, Ruff failure propagation, and synthetic trees created from the live configured package/module lists.
- Run Ruff check, Ruff format check, mypy, import-linter, schema freshness, and `git diff --check` for the resulting change.
- Run `python scripts/verify_local.py P5` only when Docker Compose and the TimescaleDB/Redis services are available; otherwise report service-backed coverage as blocked/partial, not passing. Do not run memory-heavy full-suite and simulation validations concurrently when resources are constrained.

## Spec issues

None. This work changes repository harness guidance and developer tooling only; it does not change the authoritative system blueprint.

## Autonomy decision

- **A1 — Prepare plan:** local, reversible documentation edit required before implementation. Authorized by the user’s request to implement the plan; verified with `git status` and a new-file check. Rollback is removal of this plan file. Stop before source edits until exact owner approval.
- **A2 — Implement after approval:** local repository scripts, tests, and plan-template updates. Reversible through ordinary file changes; verify with focused tests and configured static checks. No external services are mutated. Stop if implementation requires changing application contracts, production configuration, or an unresolved product decision.
- **A2 — Progress/evidence update after implementation:** update only after actual checks are observed; preserve existing user changes and report unavailable service-backed checks accurately.

