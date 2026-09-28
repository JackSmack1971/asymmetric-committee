---
name: asymmetric-decision-persistence
description: >
  Build, modify, or review Asymmetric Committee decision-pipeline persistence
  while preserving atomicity, idempotency, deterministic replay, and explicit
  run/task state. Use when changing committee decisions, risk sizing, decision
  records, run records, persistence transactions, replay/rebuild behavior,
  duplicate suppression, stage writes, or recovery after interruption. Require
  one coherent commit boundary for a decision unit, stable identities/keys,
  idempotent reruns, replay from durable inputs, and tests that prove partial or
  failed writes cannot masquerade as completed decisions.
  This skill owns durable committee/risk decision units and their replay; use
  asymmetric-point-in-time-store for fact-table/as-of storage boundaries and
  asymmetric-model-dispatch for provider-call, retry, cache, and task-outcome
  behavior. Use those alongside this skill only when one change crosses both
  boundaries.
---

# Asymmetric Committee Atomic Decision Persistence Workflow

Use this skill for:

`durable inputs -> deterministic decision -> atomic persistence -> replay/resume -> verification`

The workflow protects four separate properties:

1. **atomicity** — a decision unit is persisted completely or not at all;
2. **idempotency** — replaying the same logical work does not duplicate effects;
3. **replayability** — durable inputs plus versioned logic are sufficient to
   reconstruct or verify the decision;
4. **state fidelity** — partial/failed work cannot be reported as completed.

This Skill is procedural guidance only. It does not authorize database writes,
filesystem effects, Git actions, network calls, or production operations.

## 1. Establish the live decision-persistence baseline

Before editing:

1. Read applicable repository instructions.
2. Read:
   - `docs/asymmetric-committee-blueprint.md`
   - `docs/PROGRESS.md`
   - active phase plan
   - shared decision/run contracts under `contracts/`
   - decision logic under `committee/`
   - risk/sizing logic under `risk/`
   - persistence APIs under `store/`
   - orchestration/run state under `orchestration/`
   - evaluation/replay code when implicated
   - tests covering decision persistence, idempotency, replay, and recovery
3. If the historical workflow references files not present in the current
   checkout, report the checkout as partial/earlier rather than assuming them.
4. Inspect branch/HEAD/status/diff and preserve unrelated dirty state.
5. Identify the logical decision unit:
   - run
   - stage
   - security/entity
   - portfolio/committee batch
   - order/proposal
   - another explicitly defined unit

When shell execution is authorized:

`python .agents/skills/asymmetric-decision-persistence/scripts/decision_preflight.py`

Use `--repo <path>` when required.

## 2. Define the unit of atomic work

Before changing persistence, write down:

- durable identity/key;
- input records consumed;
- deterministic configuration/version inputs;
- records produced;
- transaction boundary;
- completion marker;
- retry/resume behavior;
- duplicate-detection rule;
- replay source;
- recovery behavior after process interruption.

Do not let "whatever the function happens to do" define the transaction.

Read `references/atomic-replay-contract.md` before adding a new persisted
decision type.

## 3. Keep decision computation separate from persistence effects

Prefer a structure where:

1. durable inputs are loaded;
2. pure/deterministic decision logic computes an output;
3. validation/risk checks finish;
4. persistence writes the coherent result atomically.

Avoid interleaving externally visible writes throughout the reasoning path if a
later validation failure could leave an impossible partial state.

Where the repository intentionally persists intermediate stages, each stage
needs its own explicit identity, state, and atomic completion semantics.

## 4. Make identities stable and explicit

Idempotency requires a logical identity stronger than "this function ran once."

Use the repository's established keys where present, such as combinations of:

- run ID;
- stage;
- security/entity ID;
- decision/proposal kind;
- as-of timestamp/version.

A replay of the same logical unit must resolve to the same identity or to an
explicitly versioned successor identity.

Do not use incidental process IDs, wall-clock timing, or random retry identifiers
as the sole idempotency key.

## 5. Persist atomically

For one atomic decision unit:

- all required records should commit together;
- completion/status should only become durable after required records are valid;
- an exception before commit should leave no completed-looking partial unit;
- side tables/evidence records must follow the same transaction contract when
  they are required for correctness.

Test rollback behavior deliberately.

Do not mark a unit `COMPLETED` before its required durable evidence exists.

## 6. Make writes idempotent

For each write path, define what happens when the same logical unit is written
again.

Acceptable patterns depend on the active schema, for example:

- conflict-ignore on immutable identity;
- compare-and-return-existing;
- deterministic upsert where mutation is explicitly part of the contract;
- reject conflicting second content for the same identity.

Tests should cover:

1. first write;
2. identical replay;
3. conflicting replay;
4. interrupted attempt followed by retry.

A second execution that silently creates duplicate decisions is not resumable.

## 7. Preserve deterministic decision ownership

The repository architecture keeps deterministic portfolio/risk sizing outside the
LLM boundary. Preserve that separation.

When decision persistence includes model verdicts plus deterministic sizing:

- persist the model verdict as evidence;
- compute deterministic weights/sizing in the deterministic layer;
- do not permit model output to become the authoritative weight merely because
  it is persisted nearby;
- record the inputs/version necessary to explain the deterministic result.

Persistence proximity does not change authority ownership.

## 8. Design replay from durable evidence

Replay should consume recorded/durable inputs rather than silently reaching live
providers.

Define whether replay means:

- re-reading already persisted source/feature/verdict records;
- reconstructing a decision at historical `as_of`;
- rebuilding derived decision records after code changes;
- validating that stored output matches deterministic recomputation.

Replay mode must make its provenance explicit.

Do not label a fresh live-provider run as replay.

## 9. Version behavior-changing inputs

A replay result is only interpretable if behavior-changing state is identifiable.

Preserve applicable versions/hashes for:

- prompt/model output source;
- feature-set version;
- risk configuration;
- committee/decision policy;
- contract/schema version;
- model served;
- code/build/repository state when the repository records it.

If the active schema lacks a required version signal, surface that as a replay
integrity gap rather than pretending outputs are comparable.

## 10. Model partial and failed states explicitly

Persistence state must distinguish:

- not started;
- in progress/pending;
- completed;
- failed/retriable;
- discarded/DLQ where applicable;
- partial run when the run cannot complete all required units.

Only the active contract's true completion state may short-circuit a resume.

A process that wrote some child records and then crashed must not be inferred
complete from record existence alone.

## 11. Test concurrency and duplicate races where relevant

Idempotency that passes sequentially may fail under concurrent workers.

When the execution model allows duplicate scheduling/concurrent attempts, test:

- two writers for the same logical key;
- unique constraint/conflict behavior;
- one winner / deterministic existing result;
- no duplicate child/evidence rows;
- completion state remains coherent.

Prefer database constraints plus transactional behavior over process-local
"already seen" sets.

## 12. Verify replay invariants

For each affected pipeline, verify applicable cases:

- identical durable inputs reproduce the expected decision;
- replay does not duplicate persisted rows;
- later live/source changes do not alter a historical replay unless explicitly
  selected;
- failed/partial units rerun safely;
- completed units short-circuit only according to active policy;
- conflicting duplicate content is detected;
- run-level status reflects child outcomes accurately.

## 13. Verify from narrow to broad

Evidence ladder:

1. pure decision/risk unit tests
2. persistence/write tests
3. transaction rollback tests
4. duplicate/idempotency tests
5. concurrent duplicate tests where relevant
6. replay/rebuild tests
7. run/task state tests
8. committee/risk integration tests
9. service-backed Postgres tests
10. exact phase gate

If Postgres/Redis or other required services are unavailable, report those
checks as blocked/skipped. Do not infer transactional/concurrency behavior from
mock-only tests.

## 14. Review the final diff

Confirm:

- one explicit identity exists per logical unit;
- completion follows successful durable writes;
- rollback cannot leave completed-looking partial state;
- retries are idempotent;
- conflicting duplicates are handled intentionally;
- replay consumes durable inputs;
- deterministic sizing remains outside model authority;
- behavior-changing versions/provenance are preserved;
- partial/failed run state cannot masquerade as complete;
- no unrelated dirty state was absorbed.

## 15. Completion output

Report only:

- decision/persistence boundary changed or determined
- files/schema/migrations changed
- atomic unit and idempotency key
- replay source and version/provenance behavior
- verification commands and actual outcomes
- service-backed checks not run
- blockers or unresolved replay/transaction semantics

Do not perform production writes, commit, push, open PRs, merge, or publish
unless explicitly requested and authorized.
