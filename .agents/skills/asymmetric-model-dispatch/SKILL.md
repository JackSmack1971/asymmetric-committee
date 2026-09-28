---
name: asymmetric-model-dispatch
description: >
  Build, modify, or review Asymmetric Committee LLM dispatch while preserving
  bounded cost, provider rate limits, configuration-hash caching, retries,
  validation/repair/discard behavior, DLQ routing, and resumable task outcomes.
  Use when changing OpenRouter dispatch, model metadata/config, rate limiting,
  cache keys, budget breakers, verdict validation, batch runners, prompts, DLQ
  handling, task status, or run completion semantics. Fail closed on missing
  cost/model metadata, never treat non-completed tasks as resumable success, and
  test with HTTP mocks/fixtures rather than live paid model calls.
  This skill owns LLM provider calls, budgets, retries, cache identity, and
  per-task dispatch outcomes; use asymmetric-decision-persistence for atomic
  committee/risk decision records and run-level replay. Combine them only when
  a change crosses from dispatch outcomes into persisted decision units.
---

# Asymmetric Committee Reliable Model Dispatch Workflow

Use this skill for:

`validated model config -> budget/rate admission -> provider call -> parse/validate -> cache/task outcome -> resume`

The model boundary must be centralized and operationally bounded. Reliability is
not "retry until success"; it is controlled dispatch with explicit budgets,
idempotent task identity, safe retries, durable outcomes, and partial-run
semantics.

This Skill is procedural guidance only. It does not authorize model-provider
network access, API keys, Redis, filesystem writes, Git actions, or spend.

## 1. Establish the live dispatch baseline

Before editing:

1. Read applicable repository instructions.
2. Read:
   - `docs/asymmetric-committee-blueprint.md`
   - `docs/PROGRESS.md`
   - active phase plan
   - `config/models.yaml`
   - model-related sections of `config/loader.py`
   - `contracts/models.py`
   - `agents/base.py`
   - when present:
     - `agents/llm/openrouter.py`
     - `agents/llm/ratelimit.py`
     - `agents/llm/store.py`
     - `agents/runner.py`
     - `prompts/agents/*`
     - `tests/agents/test_openrouter.py`
     - `tests/agents/test_llm_redis.py`
     - `tests/agents/test_base.py`
     - `tests/agents/test_runner.py`
     - `tests/agents/test_p3_e2e.py`
     - `tests/agents/test_prompts.py`
3. If historically referenced P3 paths are absent in the active checkout, report
   the checkout as earlier/partial rather than inventing behavior.
4. Inspect branch/HEAD/status/diff and preserve unrelated dirty state.
5. Classify the requested change:
   - model metadata/config
   - provider request/response
   - rate limiting
   - retry/backoff
   - structured-output validation/repair/discard
   - cost accounting/budget breaker
   - cache key/value
   - task state/DLQ
   - batch concurrency
   - resume semantics
   - run completion/partial status

When shell execution is authorized:

`python .agents/skills/asymmetric-model-dispatch/scripts/dispatch_preflight.py`

Use `--repo <path>` when required.

## 2. Define the dispatch contract first

For every dispatch path, state:

- requested model tier/slug;
- fallback sequence;
- model metadata required before dispatch;
- training cutoff / validity metadata where applicable;
- structured-output capability;
- pricing/rate metadata;
- token/cost budget;
- provider timeout/retry policy;
- request identity/cache key;
- task identity;
- terminal and retriable task outcomes;
- DLQ behavior;
- run-level completion semantics.

Unknown required cost, model, or validity metadata must fail closed before a paid
provider call.

Read `references/dispatch-state-machine.md` before modifying retries, task state,
or resume logic.

## 3. Keep provider calls centralized

Do not scatter direct OpenRouter/LLM HTTP calls across agents or prompts.

Centralized dispatch should own:

- request construction;
- authentication boundary;
- rate admission;
- retry/backoff;
- provider response metadata;
- served-model capture;
- token/cost accounting;
- structured-output parsing;
- provider error classification.

Agent code should reason over typed inputs/outputs, not implement its own
provider transport policy.

## 4. Separate model intent from served-model evidence

Requested model and actually served model are distinct facts.

When the provider returns a model identity:

1. persist/log the served model from the provider response;
2. do not infer it from the requested slug;
3. validate any compatibility/cutoff consequences required by the spec;
4. preserve the distinction in task/run evidence.

Fallback success must not erase which model actually served the response.

## 5. Fail closed on budget and pricing uncertainty

Before dispatch, prove the call is admissible under the configured run budget.

Required behavior:

- missing price metadata -> no dispatch;
- invalid/placeholder production model metadata -> no dispatch;
- exhausted run budget -> no new provider calls;
- cost accounting uses actual or conservatively bounded token usage according to
  the repository contract;
- fallback calls consume the same run-level budget;
- tasks not yet called after budget stop receive an explicit non-success outcome
  such as DLQ/blocked according to active contracts.

Do not "best effort" a paid call when cost cannot be bounded.

## 6. Rate-limit globally at the intended scope

A concurrency limit is not a provider rate limiter.

When modifying rate behavior, identify:

- key namespace;
- unit (requests/tokens/etc.);
- refill/window algorithm;
- scope across processes/workers;
- burst allowance;
- clock source;
- retry interaction.

Use Redis/shared state when the repository requires cross-worker coordination.

Test exact burst/refill boundary behavior deterministically. Do not weaken a
shared limiter into one independent limiter per worker.

## 7. Retry only safe/retriable failures

Classify failures explicitly:

- transport/transient provider failure;
- provider throttling;
- invalid structured output;
- repairable validation failure;
- non-repairable/discarded verdict;
- configuration error;
- budget error;
- programming/invariant error.

Bound retry count and delay.

A validation repair attempt is not the same thing as a transport retry. Keep
these budgets/counters conceptually separate if the active implementation does.

After retries/repair are exhausted, produce a durable non-success task outcome;
do not loop indefinitely.

## 8. Validate structured outputs before acceptance

Provider success is not task success.

For every verdict/output:

1. parse against the expected LLM-facing schema;
2. apply Pydantic/domain validation;
3. run required semantic checks;
4. repair only when the active policy allows;
5. discard/DLQ when safe recovery is not established;
6. build the system-owned envelope separately;
7. record the actual served-model metadata.

Never let malformed output become a completed task merely because HTTP returned
2xx.

## 9. Make cache identity configuration-sensitive

Cache hits must only be reused when the effective inputs/configuration are
compatible.

The history records configuration-hash cache keys. Preserve that principle.

Include all state that materially changes model output semantics, such as
applicable:

- normalized prompt/template version;
- model/tier selection;
- reasoning/temperature settings;
- structured-output schema/version;
- relevant agent configuration;
- anonymized input payload/evidence identity.

Do not key only on entity/run name if configuration can change.

Treat cache value as derived evidence with provenance, not timeless truth.

## 10. Model task outcomes for safe resume

The history records the rule:

> only completed task keys short-circuit on resume.

Preserve it.

For task identity such as `(run_id, stage, security_id)`:

- `COMPLETED` may short-circuit;
- retriable failure may run again;
- discarded/repair-exhausted work may run again only according to active policy;
- queued/pending/in-progress from an interrupted run must not be mistaken for
  completed;
- DLQ is explicit evidence, not success.

Resume must inspect durable task outcome, not infer completion from cache
presence or partial side effects.

## 11. Preserve partial-run semantics

Run status must distinguish:

- complete run;
- partial/interrupted run;
- failed/blocked run, if active contracts separate them.

If a budget breaker, unrecoverable task, or interrupted batch prevents full
completion, do not emit the same status as a complete run.

Completion should be derived from task outcomes and required work, not from the
runner merely reaching the end of a function.

## 12. Test without live paid calls

Use HTTP mocks, deterministic fake provider responses, Redis test fixtures, and
recorded/synthetic inputs.

Cover applicable cases:

- primary model success;
- fallback served;
- served-model differs from requested;
- missing model/pricing metadata;
- budget exhaustion before dispatch;
- exact budget boundary;
- provider 429/5xx/timeout;
- retry exhaustion;
- malformed JSON/schema;
- repair succeeds;
- repair/discard path;
- cache hit/miss;
- configuration hash changes cache identity;
- shared rate-limit burst/refill;
- task completed resume short-circuit;
- non-completed task reruns;
- tasks not called after breaker get explicit outcome;
- partial run status.

Do not call live paid model APIs in tests.

## 13. Verify from narrow to broad

Evidence ladder:

1. model config/loader tests
2. provider client tests
3. rate-limit/Redis tests
4. structured-output/base-agent tests
5. cache/store tests
6. runner/resume/DLQ tests
7. prompt tests
8. model-dispatch e2e with mocked HTTP
9. lint/type/import checks
10. exact phase gate

If Redis or another service is unavailable, report service-backed checks as
blocked/skipped rather than translating unit-test success into a full pass.

## 14. Review the final diff

Confirm:

- no direct provider calls escaped the centralized boundary;
- required model/cost metadata fails closed;
- budget breaker blocks new calls deterministically;
- limiter scope is not weakened;
- retries are bounded;
- malformed outputs cannot complete tasks;
- cache key covers effective configuration;
- only completed task outcomes short-circuit resume;
- partial runs cannot masquerade as complete;
- served-model evidence is retained;
- tests make no live paid calls;
- unrelated dirty state is untouched.

## 15. Completion output

Report only:

- dispatch/state boundary changed or determined
- files/config changed
- budget/rate/retry/cache decisions
- task/DLQ/resume behavior
- verification commands and actual outcomes
- service-backed checks not run
- live-provider checks not run
- blockers or unresolved model metadata

Do not invoke live paid providers, commit, push, open PRs, merge, or publish
unless explicitly requested and authorized.
