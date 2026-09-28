# Verification evidence layers

This reference is derived from repository-history Workflow 8 and the active
Makefile structure. Verify commands in the active checkout.

## Layer 1: static / fast deterministic

Examples:

- Ruff
- format check
- mypy
- import-linter
- contract/config tests
- generated-schema freshness

Strength: fast structural/type/config evidence.

Does not prove: runtime service semantics, database transactions, Redis sharing,
or full integration.

## Layer 2: focused invariant tests

Examples:

- contract validation
- property-based as-of selection
- parser behavior
- risk formulas
- anonymization leak tests

Strength: precise behavioral evidence for one subsystem/invariant.

Does not prove: all wiring across services/stages.

## Layer 3: service-backed subsystem

Examples:

- Postgres/TimescaleDB queries and migrations;
- Redis global limiter/cache/task state;
- transaction/concurrency behavior.

Strength: runtime semantics of the real service boundary.

Missing required services should be BLOCKED/FAIL at acceptance, not silently
translated into PASS.

## Layer 4: replay/smoke/e2e

Examples:

- fixture-backed backfill;
- multi-stage decision pipeline;
- idempotent rerun;
- point-in-time cross-component assertions.

Strength: wiring and cross-boundary behavior.

Does not replace precise lower-level invariant tests.

## Layer 5: phase gate

The phase gate is the repository acceptance contract for that phase. It should
compose all evidence required by the phase plan and fail closed.

Passing a subset is not equivalent to passing the phase gate.

## Layer 6: CI

CI can reproduce/extend the local gate at a named commit.

A historical CI result is evidence about that commit, not automatically about
the current dirty working tree.

## Evidence vocabulary

- PASS = executed and successful
- FAIL = executed and failed
- SKIP = runner intentionally skipped
- BLOCKED = required environment prevented execution
- NOT RUN = not executed
- REPORTED = stated in existing docs/CI/prose, not freshly reproduced

## Fail-closed patterns

Good:

- stale schema exits 1;
- missing required service exits nonzero;
- unimplemented phase gate exits 1;
- child command failure propagates.

Bad:

- `|| true`;
- unconditional `echo success`;
- optional service path used for a required gate;
- blanket `xfail`/skip masking an acceptance requirement;
- stale historical results copied into current completion evidence.
