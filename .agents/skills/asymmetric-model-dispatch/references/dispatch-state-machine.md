# Dispatch state-machine reference

This reference is grounded in the repository-history Workflow 6. Verify the live
checkout before assuming the historical P3 implementation is present.

## Admission sequence

A safe dispatch sequence is:

`config valid -> metadata present -> budget admissible -> rate admission -> provider call`

Required model/cost metadata failures stop before provider dispatch.

## Response sequence

A successful HTTP call still requires:

`provider response -> structured parse -> domain validation -> optional bounded repair -> accepted/discarded outcome`

Only accepted validated output may become a completed task.

## Task outcomes

The history establishes one key resume invariant:

**Only completed task keys short-circuit on resume.**

Conceptually distinguish:

- pending/not-called;
- running/in-flight;
- completed;
- retriable failure;
- discarded/validation exhausted;
- DLQ;
- interrupted/unknown if represented by the active contracts.

Do not collapse non-completed states into success.

## Run outcomes

A run that could not execute every required task because of budget exhaustion,
unrecoverable dispatch, interruption, or DLQ must remain distinguishable from a
complete run.

The history specifically records a partial status for interrupted/failed runs.

## Cache identity

The history records a config-hash cache key. Cache identity therefore must track
effective behavior-changing configuration rather than only business entity
identity.

Review at least:

- prompt/template version;
- model/tier;
- reasoning/temperature;
- schema/contract version;
- effective agent configuration;
- anonymized input identity.

## Budget breaker

A run-level breaker should be monotonic:

- once remaining budget cannot admit a required call, do not make additional
  paid calls that violate the budget;
- tasks not yet called need explicit durable outcomes;
- fallback/retry consumption belongs to the same run budget.

Unknown pricing is not permission to estimate optimistically.

## Served-model evidence

Requested and served model are not interchangeable. Preserve provider-reported
served-model identity in evidence and system-owned envelopes.

## Retry classes

Keep separate reasoning for:

- transport/provider retries;
- structured-output repair;
- task-level rerun on resume.

They may have different safety and budget implications.

## Evidence strength

Mocks prove local handling of modeled provider behavior. They do not prove live
provider availability, billing correctness, or production routing.

Redis-backed tests prove shared-state behavior only when Redis actually ran.
