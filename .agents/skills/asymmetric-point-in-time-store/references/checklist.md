# Point-in-time store checklist

## Baseline
- [ ] Active instructions/spec/plan/progress read.
- [ ] Git status/diff inspected; unrelated changes preserved.
- [ ] Contract/table/migration/write/as-of paths identified.
- [ ] Decision consumers identified.

## Temporal contract
- [ ] Natural key explicit.
- [ ] `event_time` meaning explicit.
- [ ] `available_at` meaning explicit.
- [ ] `source_version` identity/order explicit.
- [ ] Restatement/revision behavior explicit.
- [ ] Write/idempotency behavior explicit.

## Architecture
- [ ] Decision code reads via `store/as_of.py`.
- [ ] No raw SQLAlchemy/psycopg/table access added downstream.
- [ ] No decision-side write/connection shortcut added.
- [ ] Typed contracts returned above persistence boundary.

## Tests
- [ ] Future `available_at` is invisible.
- [ ] Correct latest eligible version is selected.
- [ ] Earlier historical read survives later restatement.
- [ ] Later version becomes visible at correct boundary.
- [ ] Timezone behavior tested where applicable.
- [ ] Source-specific availability behavior tested.
- [ ] Import-linter negative/positive coverage remains valid.

## Schema/write
- [ ] Contract updated first when shape changes.
- [ ] Table metadata and migration agree.
- [ ] Write semantics tested.
- [ ] Migration/service test run when environment permits.

## Verification
- [ ] Focused store/schema tests.
- [ ] `test_as_of` / property tests.
- [ ] Import-rule tests.
- [ ] `uv run lint-imports` / repository lint.
- [ ] Affected subsystem tests.
- [ ] Relevant phase gate.
- [ ] Service-blocked checks explicitly identified.

## Closure
- [ ] Final diff reviewed.
- [ ] No second persistence/read path introduced.
- [ ] Historical docs are not reported as fresh execution evidence.
