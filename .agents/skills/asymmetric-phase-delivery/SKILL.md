---
name: asymmetric-phase-delivery
description: >
  Deliver or continue a numbered Asymmetric Committee phase using the repository's
  plan-first workflow: inspect current Git and progress state, reconcile the phase
  plan with the authoritative blueprint, identify invariants and tests first,
  implement in small coherent slices, run the exact phase gate, and record
  evidence in docs/PROGRESS.md. Use when starting, resuming, implementing,
  reconciling, validating, or closing a P<n> phase. Do not use for an isolated
  bugfix that is not phase work, PR-only review, or general repository exploration.
---

# Asymmetric Committee Phase Delivery

Use this skill for the repository's repeatable phase workflow:

`phase plan -> specification reconciliation -> implementation -> gate -> status/evidence record`

This is a procedural skill. It does not grant filesystem, shell, Git, network,
credential, PR, or merge authority. Use only capabilities already available and
authorized by the active Codex runtime/policy.

## 1. Establish the evidence baseline

Before proposing edits:

1. Read, in this order:
   - `docs/asymmetric-committee-blueprint.md`
   - `docs/PROGRESS.md`
   - `docs/plans/README.md`
   - `docs/plans/P<n>.md` when it exists
   - applicable repository instructions (`AGENTS.md`, `AGENTS.override.md`, or
     other active project instructions)
   - `Makefile`
2. Inspect:
   - current branch / HEAD
   - `git status --short`
   - relevant committed diff/history for the active phase
   - existing tests and nearby implementation patterns
3. Separate evidence into:
   - **committed/resolved**
   - **working-tree only**
   - **reported in docs but not independently re-run**
   - **executed in this session**
4. Preserve unrelated dirty state. Never absorb, revert, reformat, or overwrite it.

If a required source is absent, say so. Do not silently substitute memory for
repository state.

For a quick structural check, run when shell execution is authorized:

`python .agents/skills/asymmetric-phase-delivery/scripts/phase_preflight.py P<n>`

If the skill is being used outside its installed repository path, run the script
from this skill directory and pass `--repo <path>`.

## 2. Reconcile the specification before implementation

Treat the blueprint as repository design authority unless the active repository
instructions establish a different precedence.

For the requested phase:

1. Identify the phase deliverable and exact blueprint sections.
2. Compare the blueprint, phase plan, progress record, existing code, tests,
   config, migrations, and gate definition.
3. Classify each mismatch:
   - **implementation gap** — spec is clear; code is missing/wrong.
   - **plan gap** — plan omits required work.
   - **spec ambiguity/conflict** — two reasonable implementations exist or
     repository sources disagree.
   - **status/evidence gap** — docs claim more or less than current evidence.
4. For a spec ambiguity/conflict, do not silently choose a product or methodology
   decision. Record the decision required and its consequences. If the correct
   resolution is already mechanically established by stronger repository
   evidence, update the plan/spec coherently before coding.
5. Keep plan and blueprint edits separate from implementation when practical so
   provenance is obvious in the diff.

Read `references/phase-contract.md` for the repository-specific contract.

## 3. Compile the phase plan

Create or update `docs/plans/P<n>.md` before phase implementation.

The plan must contain:

1. **Scope**
   - phase deliverable
   - blueprint section references
   - explicit non-goals
2. **Invariants touched**
   - invariant name/number
   - boundary it protects
   - test that proves it
3. **Steps**
   - ordered smallest coherent slices
   - expected files/areas
   - intended conventional commit subject for each slice
4. **Gate**
   - exact `make gate-P<n>` behavior expected after this phase
   - service/environment prerequisites
   - what each check proves
5. **Spec issues**
   - resolved decisions with rationale/provenance
   - unresolved decisions/blockers

Do not mark a plan step complete merely because code exists; completion requires
the evidence defined by that step.

## 4. Implement invariant-first

For every slice:

1. Inspect the closest existing pattern before adding abstractions.
2. Add or strengthen the test that expresses the touched invariant before, or in
   the same coherent slice as, production code.
3. Make only the minimum implementation needed for the planned slice.
4. Preserve architectural boundaries already enforced by import rules, typed
   contracts, store APIs, anonymization, replay semantics, or other repository
   invariants.
5. Use recorded fixtures/mocks rather than live paid APIs in tests.
6. Do not modify generated artifacts manually when a repository generator owns
   them; run the generator and freshness check instead.
7. After each slice, inspect the actual diff before proceeding.

One writer should own overlapping files. Parallel/subagent work, when explicitly
requested and available, should be read-only for exploration/review unless write
regions are independent.

## 5. Verify from narrow to broad

Use an evidence ladder:

1. focused test(s) for the changed invariant
2. affected subsystem tests
3. lint/type/import/schema freshness checks required by the plan
4. exact `make gate-P<n>`

Do not substitute a nearby command for the documented gate without saying so.
If the gate requires Postgres, Redis, TimescaleDB, network, credentials, or other
environment capabilities that are unavailable, report the gate as **not run** or
**blocked**, not passed.

A phase label, commit subject, plan checkbox, or `docs/PROGRESS.md` statement is
not proof that a current gate passed.

## 6. Close the evidence loop

Only after the exact phase gate actually passes in the current execution:

1. update `docs/PROGRESS.md` with:
   - phase status
   - exact gate command
   - observed result
   - branch/commit state if known
   - design decisions/deviations
   - unresolved issues
2. ensure the plan reflects final implementation and decisions.
3. inspect `git diff` / `git status` for:
   - unrelated changes
   - missing generated files
   - weakened assertions
   - accidental artifacts
4. distinguish:
   - local verification
   - committed history
   - CI verification
   - PR/merge state

Never claim CI, PR, merge, or remote status without inspecting it.

## 7. Completion output

Report only:

- what changed or was determined
- files changed
- verification commands and outcomes
- blockers, unresolved decisions, or unverified behavior

When phase work is incomplete, say exactly which workflow stage remains:
`PLAN`, `SPEC_RECONCILIATION`, `IMPLEMENTATION`, `GATE`, or `STATUS_RECORD`.

Do not create commits, push branches, open PRs, merge, or publish unless the user
explicitly asks and the active policy/runtime authorizes the action.
