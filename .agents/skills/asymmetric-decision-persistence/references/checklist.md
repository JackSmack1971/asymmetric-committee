# Decision persistence checklist

## Baseline
- [ ] Active instructions/spec/plan/progress read.
- [ ] Current decision/risk/store/orchestration files detected.
- [ ] Missing historical pipeline files reported.
- [ ] Dirty state preserved.

## Atomic unit
- [ ] Logical identity/key explicit.
- [ ] Required durable inputs explicit.
- [ ] Required outputs/evidence explicit.
- [ ] Transaction boundary explicit.
- [ ] Completion marker explicit.
- [ ] Resume/retry behavior explicit.

## Atomicity
- [ ] Required records commit together.
- [ ] Exception rolls back completed-looking state.
- [ ] Completion occurs after required writes.
- [ ] Child/evidence records follow same correctness boundary.

## Idempotency
- [ ] First write tested.
- [ ] Identical replay tested.
- [ ] Conflicting replay tested.
- [ ] Interrupted retry tested.
- [ ] Concurrent duplicate race tested if relevant.
- [ ] Database uniqueness/constraints used where appropriate.

## Replay/provenance
- [ ] Replay source is durable/offline.
- [ ] Historical `as_of` preserved.
- [ ] Behavior-changing versions/hashes retained.
- [ ] Live refresh is not called replay.
- [ ] Deterministic sizing remains outside model authority.

## Run/task state
- [ ] Completed is distinct from pending/in-progress.
- [ ] Failed/retriable behavior explicit.
- [ ] Partial run cannot masquerade as complete.
- [ ] Resume short-circuit uses true completion state only.

## Verification
- [ ] Pure decision/risk tests.
- [ ] Persistence/write tests.
- [ ] Rollback tests.
- [ ] Duplicate/idempotency tests.
- [ ] Replay tests.
- [ ] Run/state tests.
- [ ] Postgres/service-backed tests.
- [ ] Exact phase gate.

## Closure
- [ ] Unverified service behavior reported.
- [ ] Replay integrity gaps reported.
- [ ] Final diff reviewed.
