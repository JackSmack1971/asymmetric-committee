# Contract/schema checklist

## Baseline
- [ ] Active instructions/spec/plan/progress read.
- [ ] Git dirty state inspected and unrelated work preserved.
- [ ] Canonical producer and all relevant consumers identified.
- [ ] Change classified: new / compatible / breaking / enum / LLM / envelope / data.

## Boundary
- [ ] Producer/consumer/serialization ownership written down.
- [ ] LLM-owned versus system-owned fields explicit.
- [ ] Existing shared type/enum reused where appropriate.
- [ ] No duplicate consumer-local message shape planned.

## Tests
- [ ] Happy-path validation.
- [ ] Failure-path invariant.
- [ ] Unknown-field behavior.
- [ ] Range/enum/validator behavior as applicable.
- [ ] Serialization/round-trip behavior as applicable.
- [ ] LLM schema excludes system-owned fields.
- [ ] Consumer compatibility test.

## Source + generated artifacts
- [ ] Canonical Python model changed first.
- [ ] Generator run instead of editing JSON manually.
- [ ] Generated diff inspected.
- [ ] `contracts.schema_export --check` passes.
- [ ] No orphaned schema remains.

## Consumers + verification
- [ ] Consumers import canonical contract.
- [ ] Stale field names/literals/fixtures searched.
- [ ] Focused contract/schema tests pass.
- [ ] Consumer tests pass.
- [ ] Required lint/type/import checks pass.
- [ ] Phase gate run if applicable.

## Closure
- [ ] Final diff contains only intended source + derived + consumer/test changes.
- [ ] Compatibility/migration consequences reported.
- [ ] Current execution evidence is not conflated with old progress/CI claims.
