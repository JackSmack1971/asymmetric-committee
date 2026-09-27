# Atomic decision and replay contract

This reference is derived from repository-history Workflow 7. Verify concrete
class/table/status names in the active checkout.

## Atomic unit

Every durable decision unit should have:

- stable logical identity;
- known required inputs;
- known required outputs;
- one transaction/completion boundary;
- explicit replay/resume rule.

## Atomicity

A unit is complete only when every record required to represent that decision
and its evidence is durable.

A completion marker written before required children/evidence creates a false
state and must be treated as a design defect.

## Idempotency

Idempotency is defined over the logical decision identity, not process execution.

Test four cases:

1. first application;
2. same input repeated;
3. conflicting content under same key;
4. interrupted first attempt followed by retry.

Use database-enforced uniqueness/constraints where feasible.

## Replay

Replay must state its source:

- durable persisted inputs;
- historical point-in-time store views;
- stored model verdicts;
- stored features/config.

A live provider refresh is a new execution, not deterministic replay.

## Provenance

Replay comparison requires behavior-changing identity such as:

- run/as-of;
- feature/config version;
- risk/committee policy version;
- prompt/model metadata where model evidence is reused;
- relevant code/repository identity when available.

Without this, "same input" is ambiguous.

## State fidelity

Only a true terminal completion state may mean "already done."

Partial children, cache presence, an initiated transaction, or an in-progress
task do not equal completion.

## Model versus deterministic authority

Persisted LLM verdicts are evidence/inputs to deterministic decision logic.
Persistence does not grant the model authority over deterministic portfolio
weights or risk constraints.

## Strong evidence

For transaction/idempotency behavior:

`service-backed database test > mocked expectation`

For replay:

`recomputed result from pinned durable inputs > prose claim`

For change scope:

`Git diff > agent summary`
