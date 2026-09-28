---
name: asymmetric-fixture-ingestion
description: >
  Build, modify, or review Asymmetric Committee external data ingestion using the
  repository's provider-isolation workflow: shared throttling for live providers,
  deterministic recorded fixtures for tests, replay/backfill smoke coverage,
  explicit availability timestamps, feed-health evidence, and idempotent writes.
  Use for SEC EDGAR, Alpaca bars/news, news providers, parsers, HTTP transports,
  backfill/replay paths, provider rate limits, or ingestion fixture changes. Do
  not use for downstream feature/model logic that does not contact or parse an
  external source.
---

# Asymmetric Committee Fixture-Backed Ingestion Workflow

Use this skill for:

`provider contract -> shared client/throttle -> parser -> typed records -> recorded fixture -> replay smoke -> feed-health evidence`

The workflow has four distinct concerns:

1. provider/network behavior;
2. parsing and timestamp semantics;
3. deterministic offline evidence;
4. persistence/replay behavior.

This skill is procedural guidance only. It does not grant network access,
credentials, filesystem writes, database authority, or source-control authority.

## 1. Establish the live ingestion baseline

Before editing:

1. Read applicable repository instructions.
2. Read:
   - `docs/asymmetric-committee-blueprint.md`
   - `docs/PROGRESS.md`
   - active phase plan
   - `config/pipeline.yaml` and provider-related config
   - `ingest/http.py`
   - relevant provider client/parser under `ingest/`
   - `ingest/backfill.py`
   - relevant typed contracts
   - relevant `store/write.py` helpers
   - relevant tests under `tests/ingest/`
   - `tests/fixtures/` structure
   - active phase gate in `Makefile`
3. Inspect branch/HEAD/status/diff and preserve unrelated dirty state.
4. Classify the requested change:
   - provider HTTP/client behavior
   - throttling/retry
   - authentication/header/configuration
   - parsing
   - source timestamp/availability semantics
   - recording/replay
   - backfill orchestration
   - feed-health reporting
   - idempotency/resume behavior

Run the structural helper when shell execution is authorized:

`python .agents/skills/asymmetric-fixture-ingestion/scripts/ingest_preflight.py`

Use `--repo <path>` when necessary.

## 2. Define the source contract before code

For the provider/endpoint, document:

- endpoint and operation
- required credentials or headers
- rate-limit policy
- retryable status/error classes
- retry/backoff bounds
- pagination/cursor behavior
- source event timestamp
- source availability/revision timestamp
- stable source identity/version
- expected typed output
- fixture-redaction constraints
- whether the operation is safe to replay
- whether writes are idempotent

Do not infer provider reliability from fixture success.

If timestamp or revision semantics are not verified from real provider data,
preserve that uncertainty explicitly in progress/evidence records.

## 3. Keep network behavior behind shared provider plumbing

Do not scatter raw HTTP calls across parsers or downstream packages.

Prefer the repository's existing client/transport abstractions so that:

- live calls and replay calls exercise the same parser path;
- retry behavior is centralized;
- headers and authentication are centralized;
- throttling is shared at the correct scope;
- recording can be inserted beneath the client without rewriting business logic.

For EDGAR specifically, verify the live repository rule before editing. The
established architecture uses a process-shared Redis sliding-window limiter for
live traffic and permits an in-process limiter only for offline fixture replay.

Do not weaken a global provider limit into one independent limiter per worker.

## 4. Treat retry and throttling as separate mechanisms

A retry policy does not replace a rate limiter.

For each change, test independently:

- admission/rate behavior;
- retryable versus non-retryable status handling;
- maximum retry count;
- backoff calculation/cap;
- `Retry-After` handling when supported;
- required headers/configuration;
- failure propagation after exhaustion.

Never make tests wait through production-scale backoff. Inject clocks/sleep or
use deterministic fakes/mocks where the implementation supports it.

## 5. Record fixtures safely

The repository recording/replay model is:

`real/synthetic HTTP response -> deterministic fixture path -> ReplayTransport`

When recording:

1. use the repository transport rather than ad-hoc curl dumps;
2. ensure request identity used for fixture naming excludes credential-bearing
   query parameters;
3. inspect recorded payloads for secrets, tokens, personal data, or accidental
   environment material before committing;
4. record enough metadata/content to reproduce parser behavior;
5. keep fixture identity deterministic for the same safe request parameters.

A fixture is test evidence, not proof that the provider is universally stable or
that timestamp semantics are correct.

Live recording requires explicit network/credential authorization. Never turn a
unit-test task into live-provider access implicitly.

## 6. Replay must be offline and fail closed

Replay tests should not silently fall back to the network.

Required behavior:

- recorded request -> fixture response;
- unrecorded request -> explicit failure;
- no credentials needed for replay where the repository supports that;
- local/replay-only limiter may replace live shared infrastructure only when it
  cannot accidentally be used for live traffic.

When changing request construction, expect fixture paths to change. Inspect this
as an intentional compatibility change rather than simply regenerating fixtures
until tests pass.

## 7. Preserve timestamp and revision semantics

Parser tests must prove the timestamp that controls availability, not merely that
a date parses.

For each source record distinguish:

- business/event time;
- provider publication/acceptance time;
- revision/update time;
- system ingestion time.

Then map the correct source timestamp into the repository's `available_at`
semantics.

Do not use an earlier business date when the data was only published later.
Do not overwrite original as-filed/revision history when the repository requires
versioned facts.

If provider timezone metadata is absent or suspicious, encode the current
decision and its uncertainty explicitly.

## 8. Build parser tests from fixtures

For every parser/client change, include tests for applicable cases:

- representative successful response;
- empty response;
- malformed/missing field;
- pagination boundary;
- duplicate/revised item;
- timezone edge;
- provider error;
- retry behavior;
- redaction/fixture-path behavior.

Prefer recorded fixture content or small deterministic derivatives of it.

Do not call paid/live APIs from tests.

## 9. Verify the replay/backfill path

A provider unit test is not sufficient. Exercise the integration path that the
repository actually uses.

For backfill/replay changes verify:

1. replay uses recorded fixtures with no network;
2. expected feeds produce rows;
3. point-in-time readers never expose future `available_at` values;
4. feed-health success/error evidence is updated correctly;
5. rerunning the same replay is idempotent;
6. savepoint/error behavior permits intended continuation while surfacing a
   failing exit/result;
7. universe/snapshot side effects remain correct when implicated.

Use the repository's smoke path rather than inventing a second integration
harness.

## 10. Verify from narrow to broad

Evidence ladder:

1. transport/fixture-path tests
2. limiter/retry tests
3. parser/client tests
4. provider-specific integration tests
5. `tests/ingest/test_backfill_smoke.py` or current equivalent
6. affected store/as-of tests
7. config/universe tests if implicated
8. lint/type/import checks
9. exact relevant phase gate

Service-backed gate requirements remain real requirements. If Redis,
Postgres/TimescaleDB, credentials, or network are unavailable, distinguish:

- fixture replay tests that passed;
- service-backed tests that skipped/blocked;
- live recording/provider verification that was not attempted.

Do not convert one evidence class into another.

## 11. Review the final diff

Confirm:

- network logic remains isolated under ingestion/provider plumbing;
- live provider throttling scope is not weakened;
- retries remain bounded;
- fixture naming excludes secret query parameters;
- replay cannot fall through to network;
- timestamp/availability semantics are tested;
- new fixtures contain no credentials/secrets;
- writes/replay remain idempotent where required;
- feed failures remain observable;
- unresolved provider-quality issues remain documented;
- unrelated dirty work is untouched.

## 12. Completion output

Report only:

- provider/ingestion behavior changed or determined
- files and fixtures changed
- rate/retry/timestamp decisions
- verification commands and actual outcomes
- live-provider checks not run
- service/environment blockers
- unresolved source-quality issues

Do not record live fixtures, use credentials, commit, push, open PRs, or publish
unless explicitly requested and authorized.
