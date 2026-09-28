# Reliable model dispatch checklist

## Baseline
- [ ] Active instructions/spec/plan/progress read.
- [ ] Current dispatch files detected rather than assumed from history.
- [ ] Git dirty state inspected and preserved.

## Model/config admission
- [ ] Requested tier/model identified.
- [ ] Fallbacks explicit.
- [ ] Training-cutoff/validity metadata present where required.
- [ ] Structured-output capability present.
- [ ] Price/rate metadata present.
- [ ] Placeholders/unknown required metadata fail closed.

## Budget/rate
- [ ] Run budget defined.
- [ ] Pre-dispatch admission tested.
- [ ] Fallback/retry consumption included.
- [ ] Shared limiter scope preserved.
- [ ] Burst/refill boundary tested.

## Provider/output
- [ ] Centralized provider call path.
- [ ] Served model captured from response.
- [ ] Retryable failures classified.
- [ ] Retry count/backoff bounded.
- [ ] Schema/domain validation required.
- [ ] Repair bounded.
- [ ] Exhausted/invalid outputs become explicit non-success.

## Cache
- [ ] Configuration hash/equivalent included.
- [ ] Prompt/model/schema/input semantics represented.
- [ ] Cache cannot masquerade as task completion.

## Resume/task state
- [ ] Task identity explicit.
- [ ] Only COMPLETED short-circuits.
- [ ] Retriable/non-completed tasks can rerun per policy.
- [ ] Not-called tasks after breaker receive explicit outcome.
- [ ] DLQ is not success.
- [ ] Partial run differs from complete.

## Verification
- [ ] Config/loader tests.
- [ ] Provider client tests.
- [ ] Redis/rate tests.
- [ ] Base/output validation tests.
- [ ] Cache/store tests.
- [ ] Runner/resume/DLQ tests.
- [ ] Prompt tests.
- [ ] Mocked e2e.
- [ ] Phase gate.
- [ ] No live paid model calls in tests.

## Closure
- [ ] Service-backed checks accurately reported.
- [ ] Live-provider behavior not inferred from mocks.
- [ ] Final diff reviewed.
