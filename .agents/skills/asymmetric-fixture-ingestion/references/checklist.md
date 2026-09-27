# Fixture-backed ingestion checklist

## Baseline
- [ ] Active instructions/spec/plan/progress read.
- [ ] Provider client/parser/transport/backfill/tests inspected.
- [ ] Git dirty state inspected and unrelated work preserved.
- [ ] Provider operation and typed output identified.

## Provider contract
- [ ] Credentials/headers identified.
- [ ] Rate-limit scope identified.
- [ ] Retryable errors/statuses identified.
- [ ] Retry count/backoff bounded.
- [ ] Pagination behavior identified.
- [ ] Event time identified.
- [ ] Availability/revision time identified.
- [ ] Source identity/version identified.

## Network/throttle
- [ ] Shared live limiter preserved where required.
- [ ] Retry and rate-limit tests are separate.
- [ ] No raw HTTP path introduced outside provider plumbing.
- [ ] Replay-only fallback cannot be used accidentally for live traffic.

## Fixtures
- [ ] Recording uses canonical transport/path logic.
- [ ] Secret query params excluded from fixture identity.
- [ ] Recorded body/diff inspected for secrets.
- [ ] Replay fails on unrecorded request.
- [ ] Tests do not fall through to network.

## Parsing
- [ ] Success case.
- [ ] Empty/malformed case.
- [ ] Timestamp/timezone case.
- [ ] Revision/duplicate case if applicable.
- [ ] Provider error/retry case.
- [ ] Typed record validation.

## Replay/backfill
- [ ] Replay is offline.
- [ ] Expected feed rows produced.
- [ ] Point-in-time visibility checked.
- [ ] Feed-health outcome checked.
- [ ] Second replay is idempotent.
- [ ] Failure path remains observable.

## Verification
- [ ] Focused transport/limiter tests.
- [ ] Parser/client tests.
- [ ] Backfill smoke.
- [ ] Relevant store/as-of tests.
- [ ] Lint/type/import checks.
- [ ] Exact phase gate if applicable.
- [ ] Live/provider checks explicitly marked run or not run.

## Closure
- [ ] No credential-bearing fixture/artifact added.
- [ ] Provider-quality uncertainty preserved.
- [ ] Final diff reviewed for unrelated changes.
