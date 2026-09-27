---
name: asymmetric-contract-schema
description: >
  Change or review Asymmetric Committee inter-stage contracts using the repository's
  contracts-first workflow. Use when adding or modifying message models, enums,
  LLM-facing output models, serialized fields, downstream consumers, or generated
  JSON schemas. Establish the contract before consumers, add compatibility and
  validation tests, regenerate derived schemas through contracts.schema_export,
  and prove schema freshness with --check. Do not use for changes that do not
  affect contracts, serialized shapes, or generated schemas.
---

# Asymmetric Committee Contract and Schema Workflow

Use this skill for the repository's repeatable workflow:

`contract authority -> invariant tests -> contract change -> generated schema -> consumers -> freshness gate`

This is procedural guidance, not authorization. It does not grant filesystem,
shell, Git, network, credential, PR, or merge authority. Runtime policy remains
authoritative for effects.

## 1. Establish the live contract baseline

Before editing:

1. Read applicable repository instructions.
2. Read:
   - `docs/asymmetric-committee-blueprint.md`
   - `docs/PROGRESS.md`
   - the active phase plan, if any
   - `contracts/enums.py`
   - `contracts/models.py`
   - `contracts/data.py` when data records are implicated
   - `contracts/schema_export.py`
   - relevant tests under `tests/contracts/`
   - affected downstream consumers
3. Inspect current Git status/diff and preserve unrelated dirty state.
4. Identify the authoritative source for each shape:
   - Python contract model / enum = source
   - generated JSON schema = derived artifact
   - consumer-local duplicate shape = architecture violation unless explicitly
     established by stronger repository authority.
5. Classify the change:
   - new contract
   - compatible extension
   - breaking serialized change
   - enum/category change
   - LLM-facing schema change
   - system-envelope-only change
   - data/storage record change

If the repository state conflicts with the plan or blueprint, reconcile the
specification before silently choosing a shape.

Run the read-only helper when shell execution is authorized:

`python .agents/skills/asymmetric-contract-schema/scripts/contract_preflight.py`

Use `--repo <path>` when the repository is elsewhere.

## 2. Define the boundary before the consumer

The contract must exist before new downstream code depends on it.

For each proposed field/type:

1. State:
   - producer
   - consumer(s)
   - persistence/serialization boundary
   - whether the LLM may emit it
   - whether the system fills it
   - compatibility expectation
2. Reuse repository types and enums before creating parallel strings or models.
3. Keep categorical values in enums where the repository contract requires them.
4. Preserve strict model behavior (`extra="forbid"` and repository immutability
   conventions) unless stronger authority explicitly changes it.
5. Do not let an LLM-facing model acquire system-owned fields such as runtime
   identifiers, served-model evidence, or deterministic portfolio weights.
6. Do not add a consumer-local TypedDict/dict shape as a shortcut around a shared
   contract.

Read `references/contract-boundaries.md` before changing an LLM/system-envelope
split.

## 3. Write invariant and compatibility tests first

Before, or in the same coherent slice as, the source-model change, add tests for
the behavior that must remain true.

Depending on the change, test:

- required vs optional fields
- unknown-field rejection
- numeric/range constraints
- enum serialization
- invalid categorical values
- model immutability when applicable
- model validators
- round-trip serialization
- system-owned fields excluded from LLM-facing schemas
- source-to-envelope conversion
- compatibility with existing fixtures/consumers
- generated schema strictness and expected properties
- removed/renamed fields fail as intended

A test that merely instantiates the happy path is insufficient for a boundary
change. Include at least one failure-mode assertion for the invariant being
changed.

## 4. Change the canonical source

Make the smallest coherent edit in the canonical contract source.

Preferred order:

1. enums/shared aliases
2. contract model
3. source-model tests
4. generated artifacts, if applicable
5. downstream consumers
6. consumer/integration tests
7. phase/gate documentation if required

Do not change generated schema JSON manually.

If the shape change requires a database migration or stored-data compatibility
decision, stop treating it as "just a contract edit." Surface the migration and
backward-compatibility consequences explicitly and follow the repository's store
workflow.

## 5. Handle LLM-facing schemas as derived artifacts

The repository's generator is:

`uv run python -m contracts.schema_export`

Freshness check:

`uv run python -m contracts.schema_export --check`

For an LLM-facing change:

1. Confirm the intended model is in the repository's LLM output model set.
2. Change the Python source model first.
3. Run the generator.
4. Inspect the generated diff.
5. Run `--check`; it must return zero.
6. Run contract/schema tests.
7. Confirm the generated schema does not expose system-owned envelope fields.

The generator may deliberately transform Pydantic's raw schema for provider
compatibility. Do not "restore" removed constraints or `$ref` structures by
hand. Runtime Pydantic validation remains a separate boundary.

If generation creates an orphaned schema, treat that as a failure requiring an
intentional source-model/model-set decision.

## 6. Update consumers only after the boundary is stable

For each affected consumer:

1. Import the canonical contract rather than restating its shape.
2. Adapt construction/parsing at the boundary.
3. Preserve producer/system ownership of fields.
4. Add a consumer test that would fail if the contract drifts.
5. Search for stale field names, enum literals, duplicate shapes, or old fixture
   payloads.

Do not broaden the change into unrelated refactors.

## 7. Verify from narrow to broad

Use this evidence ladder:

1. focused contract/model tests
2. generated-schema tests
3. `uv run python -m contracts.schema_export --check`
4. affected consumer tests
5. required lint/type/import checks
6. active phase gate when this change belongs to a phase

Never report schema freshness from a prior progress note as a current pass.
Execute the check in the current working state.

If dependencies or services prevent the phase gate, report that separately;
schema freshness can pass while the broader phase remains unverified.

## 8. Review the final diff as a source/derived change

Before completion, confirm:

- canonical Python source changed intentionally
- generated schema diff matches the source-model change
- no generated JSON was hand-edited independently
- no stale/orphaned generated schema remains
- no duplicate consumer-local message shape was introduced
- tests cover both valid and invalid behavior
- unrelated dirty state remains untouched
- documentation claims do not exceed executed evidence

## 9. Completion output

Report only:

- contract/boundary changed or determined
- files changed
- generated artifacts changed
- verification commands and actual outcomes
- compatibility/migration consequences
- blockers or unverified behavior

Do not create commits, push, open PRs, merge, or publish unless explicitly
requested and authorized.
