# Provider and fixture boundary reference

Verify these details against the current repository before relying on them.

## Shared HTTP fixture model

The repository's HTTP fixture plumbing separates:

- `RecordingTransport`: obtains a response from an underlying transport and writes
  successful response bytes to a deterministic fixture location;
- `ReplayTransport`: serves fixture bytes and raises when no fixture exists.

Fixture identity is derived from host/path plus a hash of safe query parameters.
Credential-like query parameters such as API keys/tokens are excluded from that
hash. That reduces secret propagation into filenames but does not eliminate the
need to inspect recorded response bodies and repository diffs.

## EDGAR limiter model

The current EDGAR client models a provider-wide rate limit shared across workers.

Live:
`workers -> shared Redis sliding window -> EDGAR`

Replay:
`test/replay -> local limiter -> recorded transport`

The local limiter is a replay/testing convenience, not evidence that independent
per-process live limiting is safe.

The client also has bounded retry behavior and recognizes `Retry-After`.

## Backfill model

The current backfill path:

1. builds provider clients/transports;
2. seeds securities;
3. loads each feed per ticker;
4. wraps each feed in a savepoint;
5. records feed-health success/error;
6. commits resumable work;
7. builds month-end snapshots;
8. returns a summary/error outcome.

Writes are intended to be idempotent so replay/backfill can be rerun.

## Fixture evidence strength

A recorded fixture proves that the parser can handle that recording.

It does not prove:

- the provider always returns that shape;
- the provider's timestamps are reliable;
- revision behavior is complete;
- pagination has no unseen edge cases;
- a synthetic fixture matches production exactly.

Keep those claims separate.

## Timestamp evidence

For external data, the critical question is not only "when did the event happen?"
but "when was this exact information knowable?"

For filings, news revisions, trades, and bars, tests should preserve the
repository's point-in-time `available_at` invariant.

## Live-call boundary

Live provider calls can involve:

- network authority;
- credentials;
- paid API usage;
- rate-limit consequences;
- external audit effects.

A Skill can request or recommend such a check. It cannot authorize it.
