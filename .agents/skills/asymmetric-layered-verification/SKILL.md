---
name: asymmetric-layered-verification
description: >
  Build, modify, or review Asymmetric Committee verification using layered,
  evidence-strength-aware gates. Use when changing tests, Makefile phase gates,
  service-backed checks, CI acceptance, smoke tests, import/static validation,
  schema freshness, or phase completion criteria. Keep fast deterministic checks
  separate from service-backed gates, fail closed on required-service absence,
  preserve exact executed evidence, and never treat a partial test slice or
  historical result as proof that the full phase gate passed.
---

# Asymmetric Committee Layered Verification Workflow

Use this skill for:

`fast local checks -> focused subsystem tests -> service-backed integration -> exact phase gate -> recorded evidence`

Verification layers answer different questions. Do not collapse them.

This Skill is procedural guidance only. It does not authorize shell execution,
service startup, network access, credentials, CI mutation, Git actions, or
production effects.

## 1. Establish the live verification baseline

Before editing:

1. Read applicable repository instructions.
2. Read:
   - `docs/asymmetric-committee-blueprint.md`
   - `docs/PROGRESS.md`
   - active phase plan
   - `Makefile`
   - `pyproject.toml`
   - relevant `tests/` directories
   - `docker-compose.yml` / service bootstrap files
   - current CI workflows, when CI acceptance is implicated
3. Inspect the exact phase gate rather than inferring it from prose.
4. Inspect branch/HEAD/status/diff and preserve unrelated dirty state.
5. Classify the verification change:
   - fast unit/static
   - schema/config freshness
   - property/invariant
   - subsystem integration
   - service-backed database/Redis
   - replay/smoke
   - e2e
   - phase acceptance
   - CI-only evidence

When shell execution is authorized:

`python .agents/skills/asymmetric-layered-verification/scripts/verification_preflight.py`

Use `--repo <path>` when required.

## 2. Define the acceptance question before the test

For every proposed check, state:

- what invariant/behavior it proves;
- what it does not prove;
- required environment/services;
- whether it is deterministic/offline;
- expected runtime/cost;
- exact command;
- whether it is advisory or phase-blocking;
- how skip/block/failure is represented.

Do not add tests simply because more tests seem safer.

Read `references/evidence-layers.md` before changing a phase gate.

## 3. Keep fast feedback fast

The fast layer should catch cheap deterministic failures early.

Typical examples:

- Ruff
- formatting check
- mypy
- import-linter
- contract/config tests
- schema freshness
- focused pure unit tests

Do not force every edit through expensive service-backed setup when a narrower
deterministic check can catch the same class of defect.

Fast checks are triage/feedback, not substitutes for stronger required evidence.

## 4. Put invariants at the narrowest effective layer

Test a rule where it is easiest to make failures precise.

Examples:

- serialized contract shape -> contract test;
- forbidden import -> import-linter/static rule;
- point-in-time selection -> store property test;
- replay idempotency -> integration smoke;
- Postgres transaction behavior -> service-backed DB test.

Do not rely only on a large e2e suite for an invariant that can fail with a
clearer targeted test.

## 5. Require real services where semantics depend on them

Mocks cannot prove database, Redis, concurrency, transaction, or provider-shared
state semantics.

For checks whose correctness depends on Postgres/TimescaleDB, Redis, or another
runtime service:

1. identify that dependency explicitly;
2. make the phase gate require it;
3. configure missing required services to fail rather than skip;
4. still allow narrower local suites to skip when appropriate;
5. report which class of evidence actually ran.

The current repository's P1 pattern of `REQUIRE_SERVICES=1` is the model: a
service-backed phase gate must not silently degrade into a weaker local pass.

## 6. Preserve fail-closed gate semantics

A phase gate is an acceptance contract.

Required behavior:

- implemented gate returns nonzero on failure;
- missing required services return nonzero;
- stale generated artifacts return nonzero;
- unimplemented gates fail explicitly;
- no `|| true`, swallowed exit status, or blanket skip converts failure to pass.

If a gate is not yet implemented, leave it visibly failing until the phase owns
a real acceptance check.

Do not replace a fail-closed stub with a green no-op.

## 7. Distinguish PASS, FAIL, SKIP, BLOCKED, and NOT RUN

Use evidence states precisely:

- **PASS** — command actually executed and returned success;
- **FAIL** — command executed and acceptance condition failed;
- **SKIP** — test runner intentionally skipped a check;
- **BLOCKED** — required environment/dependency prevented execution;
- **NOT RUN** — command was not executed;
- **REPORTED** — historical/prose/CI claim not freshly reproduced here.

Do not turn SKIP/BLOCKED/REPORTED into PASS.

## 8. Keep phase gates exact and reproducible

A phase plan should name the exact gate command.

When changing a phase gate:

1. use repository-local commands;
2. pin the intended test scopes;
3. include all required static/freshness checks;
4. include required service-backed tests;
5. avoid hidden manual steps unless explicitly documented;
6. make the command runnable from a known repository state.

Completion evidence should name the exact command that ran.

## 9. Use layered escalation during implementation

During development, prefer:

1. failing focused test;
2. smallest implementation fix;
3. focused test pass;
4. neighboring subsystem tests;
5. static/lint/type checks;
6. service-backed integration;
7. exact phase gate.

This keeps iteration fast while still ending on the required acceptance level.

Do not run only the phase gate repeatedly when a focused failing test can shorten
the loop.

## 10. Treat smoke tests as cross-boundary evidence

Smoke/replay tests are valuable because they prove wiring across components.

A smoke test should assert meaningful outcomes, not just process exit zero.

Applicable evidence may include:

- expected rows/artifacts created;
- no future-data leakage;
- feed/task health;
- idempotent second run;
- replay uses fixtures rather than network;
- durable state matches the intended stage.

Keep the lower-level invariant tests too; smoke success does not explain every
failure mode.

## 11. Test the gate itself where useful

Acceptance machinery is production infrastructure for engineering quality.

For nontrivial gate changes, test or mechanically inspect:

- missing-service behavior;
- stale artifact behavior;
- failing child command propagates;
- intended scopes are present;
- unimplemented phases fail;
- environment flags are not accidentally optionalized.

A green gate that omits a required suite is a verification defect.

## 12. Record mechanical evidence

At completion capture:

- exact command;
- exit code;
- pass/fail/skip counts where available;
- relevant service/environment state;
- commit/working-tree identity when needed;
- which gate/phase the evidence belongs to.

Prefer executed output over agent prose.

Do not say "all tests pass" if only a focused subset ran.

## 13. Verify from narrow to broad

Use this evidence ladder:

1. changed test itself
2. focused module/package tests
3. static/lint/type/import checks
4. schema/config freshness
5. subsystem integration
6. required service-backed checks
7. replay/smoke/e2e
8. exact phase gate
9. CI at the intended commit, when required

Later layers strengthen evidence; they do not erase failures or skips from earlier
layers.

## 14. Review the final diff

Confirm:

- each new test maps to a named invariant/behavior;
- fast checks remain appropriately scoped;
- service-dependent semantics are tested with services;
- missing required services fail the acceptance gate;
- unimplemented gates fail closed;
- no required command is hidden behind a non-blocking path;
- exact phase command remains reproducible;
- evidence language matches what actually ran;
- unrelated dirty state remains untouched.

## 15. Completion output

Report only:

- verification/gate boundary changed or determined
- files/commands changed
- evidence layers exercised
- exact commands and actual outcomes
- skips/blocked services
- phase gate result
- CI result only if actually checked
- remaining unverified behavior

Do not start services, mutate CI, commit, push, open PRs, merge, or publish unless
explicitly requested and authorized.
