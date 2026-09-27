---
name: asymmetric-point-in-time-store
description: >
  Change, extend, or review Asymmetric Committee persistence and decision-side data
  access while preserving point-in-time correctness and the store API boundary.
  Use when modifying store/as_of.py, store writes or migrations, fact-table
  schemas, available_at/source_version semantics, historical replay behavior, or
  downstream code that needs persisted data. Require decision code to read facts
  only through store/as_of.py, prove no-look-ahead with as-of tests, preserve
  import-linter boundaries, and verify migration/schema behavior. Do not use for
  storage-independent application changes.
---

# Asymmetric Committee Point-in-Time Store Workflow

Use this skill for the repository's repeatable workflow:

`typed persisted record -> migration/write path -> point-in-time read API -> downstream consumer`

with two independent invariants:

1. **temporal correctness** — no information unavailable at `as_of` reaches a
   decision;
2. **architectural isolation** — decision code does not bypass `store/as_of.py`
   or reach raw persistence internals.

This skill is procedural guidance only. It does not authorize database access,
filesystem writes, shell execution, credentials, network access, or Git actions.

## 1. Establish the persistence baseline

Before editing:

1. Read applicable repository instructions.
2. Read:
   - `docs/asymmetric-committee-blueprint.md`
   - `docs/PROGRESS.md`
   - active phase plan, if any
   - `contracts/data.py`
   - `store/as_of.py`
   - `store/write.py`
   - `store/_tables.py`
   - `store/migrate.py`
   - relevant migrations under `store/migrations/versions/`
   - `tests/store/test_as_of.py`
   - `tests/store/test_schema.py`
   - `tests/store/test_import_rules.py`
   - `pyproject.toml` import-linter/Ruff boundary configuration
3. Inspect current branch/HEAD/status/diff and preserve unrelated dirty state.
4. Identify:
   - fact/table being changed
   - natural key
   - `event_time` meaning, if present
   - `available_at` meaning
   - `source_version` ordering/identity
   - write/idempotency semantics
   - point-in-time reader
   - downstream decision consumers

Run the structural helper when shell execution is authorized:

`python .agents/skills/asymmetric-point-in-time-store/scripts/store_preflight.py`

Use `--repo <path>` when necessary.

## 2. Define the temporal contract before implementation

For every persisted fact or derived record, write down:

- **event time** — when the underlying event occurred;
- **availability time** — when the system could legitimately know it;
- **source version** — how two versions with the same natural key are ordered;
- **natural key** — what identifies the same fact across versions;
- **as-of selection rule** — which version is visible at decision time;
- **write semantics** — insert-only, upsert, conflict-ignore, replacement, etc.;
- **restatement/revision semantics** — whether later knowledge supersedes earlier
  knowledge only for later `as_of` values.

Do not conflate `event_time` with `available_at`.

If source timestamps are ambiguous or provider-specific, surface that as a data
quality/specification issue rather than inventing temporal semantics.

Read `references/temporal-boundary.md` before adding a new fact family.

## 3. Preserve the read boundary

Decision-side packages must consume typed records through `store/as_of.py`.

Do not introduce raw fact-table access in decision code through:

- SQLAlchemy
- psycopg
- `store._tables`
- migration internals
- direct connection-opening helpers
- `store.write`
- ingestion modules

when the repository's import contracts forbid it.

If a consumer needs data that `store/as_of.py` does not expose:

1. add or extend a typed as-of API;
2. prove its temporal semantics in store tests;
3. then update the consumer.

Do not bypass the boundary "temporarily."

Static import rules are defense in depth, not a substitute for correct runtime
as-of logic.

## 4. Implement schema and write changes coherently

Preferred order for a new persisted fact path:

1. typed contract in `contracts/data.py`
2. table metadata/schema definition
3. migration
4. write helper
5. write/schema tests
6. as-of read function
7. temporal property/revision tests
8. downstream consumer
9. consumer/integration tests

For an existing fact family, make the smallest subset required.

Migration and metadata must agree. Do not patch only the ORM/table metadata while
leaving migrations stale, or vice versa.

Preserve established idempotency/insert-only semantics unless the specification
explicitly changes them.

## 5. Prove no-look-ahead mechanically

Every new or materially changed point-in-time reader needs failure/replay tests,
not only current-value tests.

At minimum prove:

1. rows with `available_at > as_of` are invisible;
2. the visible row is the intended latest version per natural key;
3. a later restatement/revision does not alter an earlier historical read;
4. the later version becomes visible at or after its availability time;
5. naïve timestamps are rejected where timezone awareness is required;
6. filtering/lookback logic cannot reintroduce future knowledge.

Prefer property-based tests when the invariant is naturally expressed over many
version/order combinations.

For provider facts, test the actual availability boundary relevant to the source
(e.g. filing acceptance, revision time), not a convenient business/event date.

## 6. Keep typed data above persistence details

`store/as_of.py` should return repository contract objects, not persistence rows
that leak SQLAlchemy/table semantics into decision code.

When changing a return model:

- change the shared contract deliberately;
- test model validation;
- test the as-of conversion;
- inspect downstream compatibility;
- avoid exposing ingestion-only or storage-only columns unless required by the
  domain contract.

If a field such as `ingested_at` is operational provenance rather than decision
information, do not expose it merely because it exists in the table.

## 7. Verify architectural isolation

After changes:

1. run focused import-rule tests;
2. run `uv run lint-imports` or the repository lint target;
3. run Ruff checks that enforce banned persistence internals;
4. search changed downstream modules for forbidden imports;
5. inspect the final diff for a second persistence path.

A passing as-of unit test does not prove the architecture boundary.
A passing import-linter check does not prove temporal correctness.
Both are required.

## 8. Verify schema and services

Use an evidence ladder:

1. focused schema/write tests
2. focused `test_as_of` tests
3. property/restatement/revision tests
4. import-rule tests
5. migration/schema parity checks
6. affected subsystem tests
7. repository lint/import checks
8. relevant phase gate

If Postgres/TimescaleDB or another required service is unavailable, report the
service-backed checks as skipped/blocked/not run. Do not infer they passed from
SQLite/unit coverage or prior progress notes.

## 9. Review the final change

Confirm:

- temporal semantics are explicit;
- natural key/version ordering is deterministic;
- future rows are excluded by the canonical reader;
- historical reads are stable under later restatements;
- decision code uses typed `store/as_of.py` APIs;
- no forbidden persistence imports were added;
- table metadata and migrations agree;
- write semantics are tested;
- unrelated dirty state is untouched;
- current execution evidence is distinguished from historical docs/CI.

## 10. Completion output

Report only:

- temporal/persistence boundary changed or determined
- files changed
- migration/schema effects
- verification commands and actual outcomes
- service-dependent checks not run
- compatibility/data-migration consequences
- blockers or unresolved temporal semantics

Do not create commits, push, open PRs, merge, or perform database/network
operations beyond what the user requested and active policy authorizes.
