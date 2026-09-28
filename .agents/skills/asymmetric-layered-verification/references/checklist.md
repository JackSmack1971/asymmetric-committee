# Layered verification checklist

## Baseline
- [ ] Active instructions/spec/plan/progress read.
- [ ] Exact Makefile/phase gate inspected.
- [ ] Relevant tests and services identified.
- [ ] Dirty state preserved.

## Acceptance question
- [ ] Each test maps to an invariant/behavior.
- [ ] What it proves is explicit.
- [ ] What it does not prove is explicit.
- [ ] Environment/service dependencies explicit.
- [ ] Exact command explicit.

## Fast layer
- [ ] Focused unit/invariant test.
- [ ] Ruff/format where applicable.
- [ ] mypy where applicable.
- [ ] import-linter where applicable.
- [ ] schema/config freshness where applicable.

## Service-backed layer
- [ ] Real service used for service semantics.
- [ ] Missing required service fails acceptance.
- [ ] Mock-only evidence not overstated.
- [ ] Transaction/concurrency behavior tested where relevant.

## Smoke/e2e
- [ ] Cross-boundary outcomes asserted.
- [ ] Exit-zero-only smoke avoided.
- [ ] Idempotency/replay checked where relevant.

## Gate
- [ ] Child failure propagates.
- [ ] Required scopes included.
- [ ] Unimplemented gate fails closed.
- [ ] Exact phase gate executed before claiming phase completion.

## Evidence
- [ ] PASS/FAIL/SKIP/BLOCKED/NOT RUN distinguished.
- [ ] Exact commands recorded.
- [ ] Counts/exit codes captured where available.
- [ ] Historical/CI evidence marked separately.
- [ ] Current dirty state not conflated with prior commit evidence.

## Closure
- [ ] Remaining unverified behavior reported.
- [ ] Final diff reviewed.
