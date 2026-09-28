---
name: asymmetric-model-anonymization
description: >
  Build, modify, or review Asymmetric Committee model-input anonymization while
  preserving the repository's identity and numeric-leak invariants. Use when
  changing aliases, entity-token generation, prompt rendering, per-agent
  partitions, identity masking, numeric/date/currency scrubbing, alias coverage,
  executive/person handling, or prompt leak tests. Require point-in-time alias
  resolution, explicit leak classes, fail-closed coverage guards, and tests that
  assert forbidden identities/numbers do not appear in rendered model inputs.
  Do not use for model dispatch, provider retry/cost controls, or downstream
  committee logic that does not construct model input.
---

# Asymmetric Committee Model Anonymization Workflow

Use this skill for:

`point-in-time identity mapping -> partition -> render/scrub -> leak harness -> model input`

The central invariant is negative: forbidden identity and sensitive numeric
information must not appear in model-facing input. Successful rendering alone is
not evidence of anonymization.

This Skill is procedural guidance only. It does not authorize model calls,
database access, filesystem writes, network access, credentials, or Git actions.

## 1. Establish the baseline

Before editing:

1. Read applicable repository instructions.
2. Read the blueprint, `docs/PROGRESS.md`, active phase plan, and—when present—
   `config/aliases.yaml`, `config/aliases.py`, `contracts/data.py`,
   `contracts/enums.py`, `store/aliases.py`, `store/as_of.py`,
   `features/renderer.py`, `agents/partitioner.py`, `agents/partitions.py`,
   and relevant tests under `tests/agents/`, `tests/features/`,
   `tests/universe/`, `tests/store/`, `tests/config/`, and `tests/contracts/`.
3. If a historically named path is absent in the active checkout, report that
   rather than assuming its implementation.
4. Inspect branch/HEAD/status/diff and preserve unrelated dirty state.
5. Classify the leak class: ticker/name/CIK, person/executive, short alias,
   numeric/currency/share/date, evidence identifier, cross-partition data, or
   alias coverage.

When shell execution is authorized:

`python .agents/skills/asymmetric-model-anonymization/scripts/anonymization_preflight.py`

Use `--repo <path>` when required.

## 2. Define the threat model

For each change, specify:

- protected model-facing surface;
- forbidden raw source values;
- allowed transformed representation;
- point-in-time alias source;
- owning agent partition;
- whether exact value preservation is necessary;
- transformation stability scope;
- failure behavior when no safe representation exists.

Do not model "anonymized" as one boolean. Maintain explicit leak classes.

Read `references/leak-classes.md` before adding a new transformation.

## 3. Resolve aliases point-in-time

When aliases are historical or availability-dependent:

1. resolve them at the relevant `as_of`;
2. never use a later/current alias to rewrite an earlier historical prompt;
3. test unavailable aliases cannot silently appear historically;
4. preserve stable within-run identity linking where required;
5. ensure tokens do not themselves reveal ticker/name information.

Do not bypass the established store/as-of boundary for alias lookup.

## 4. Partition before rendering

Partitioning controls which rows/fields an agent may receive.
Rendering controls how permitted fields are represented.

Do not rely on renderer scrubbing to repair an over-broad partition.

For each partition, test both expected presence and forbidden absence.

## 5. Use explicit transformations

The repository history records transformations for identity aliases, PERSON
aliases, hashed executive tokens, numeric/date scrubbing, currency/share
scrubbing, and evidence IDs.

For every sensitive field class define deterministic behavior. Avoid broad regex
stripping unless tests prove it preserves required prompt/evidence structure.

Do not remove information required by downstream reasoning unless the active
specification explicitly changes that requirement.

## 6. Build a negative leak harness

Tests must assert forbidden content is absent.

For each relevant change, cover applicable classes:

- ticker;
- company/legal name;
- CIK;
- executive/person names;
- configured short/colloquial aliases;
- prohibited raw numeric/date/currency/share values;
- data from another agent's partition.

Use distinctive sentinel values. Prefer a reusable leak harness over scattered
one-off checks.

## 7. Treat alias coverage as correctness

1. enumerate entities/names the system may encounter;
2. prove required entities have safe aliases/tokens;
3. fail closed or emit a blocker when coverage is missing;
4. test stop words and short-name collisions;
5. document manual configuration dependencies.

Passing current alias tests proves configured coverage, not universal future
coverage.

## 8. Preserve evidence traceability

Anonymization must preserve safe evidence identifiers that map model citations
back to source records without exposing forbidden prompt content.

Test that evidence IDs survive rendering, remain sufficiently unique, avoid raw
sensitive values, and can be resolved outside the model-facing surface.

## 9. Re-review on upstream shape changes

Trigger anonymization review when contracts, ingestion records, features, alias
schemas, prompt templates, partitions, or evidence formats add model-visible
fields.

A new field can bypass scrub logic even when anonymization code itself is
unchanged.

## 10. Verify from narrow to broad

Use this ladder:

1. alias/config tests
2. renderer transformation tests
3. partition tests
4. identity leak tests
5. numeric/date/currency/share leak tests
6. alias-coverage/short-name tests
7. prompt/template tests
8. agent integration/e2e tests
9. lint/type/import checks
10. exact phase gate when applicable

For each touched leak class include at least one test that fails if the raw value
reappears. Do not claim full anonymization from a subset of leak classes.

## 11. Final review

Confirm point-in-time aliasing, partition scope, deterministic transformations,
review of all new model-visible fields, separate identity/numeric leak coverage,
explicit unresolved coverage dependencies, preserved evidence traceability, no
unnecessary raw sensitive fixtures, and untouched unrelated dirty state.

## 12. Completion output

Report only:

- leak class/boundary changed or determined
- files/config changed
- transformations added or modified
- verification commands and outcomes
- uncovered/manual alias dependencies
- leak classes not verified
- blockers or unresolved anonymization semantics

Do not invoke live model providers, commit, push, open PRs, merge, or publish
unless explicitly requested and authorized.
