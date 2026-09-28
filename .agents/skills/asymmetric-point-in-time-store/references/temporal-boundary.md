# Temporal and persistence boundary reference

Verify all details against the live repository before implementation.

## Two independent boundaries

### Temporal boundary

A decision at time `T` may use only data knowable at `T`.

The repository's established point-in-time rule for versioned facts is:

- filter to rows with `available_at <= as_of`;
- group by the fact's natural key;
- choose the latest `(available_at, source_version)` among eligible rows.

This means a later restatement can change later decisions without rewriting what
an earlier decision should have seen.

### Architectural boundary

Decision packages consume persisted facts through `store/as_of.py`. Raw database
and table internals remain in the persistence/ingestion side of the architecture.

These boundaries solve different problems:

- import isolation prevents architectural leakage;
- as-of filtering prevents look-ahead.

Neither substitutes for the other.

## Time vocabulary

Use explicit names:

- `event_time`: when the represented market/business event occurred;
- `available_at`: when the information became knowable to the system;
- `ingested_at`: when this system happened to persist it;
- `as_of`: decision-time visibility boundary.

Do not substitute ingestion time for source availability unless the repository
spec explicitly defines that behavior.

## New fact-family questions

Before adding a table/read path, answer:

1. What is the natural key?
2. Can the source revise/restates a fact?
3. How is a source version identified and ordered?
4. What timestamp proves public/legitimate availability?
5. Can two source versions have the same availability timestamp?
6. What tie-break is deterministic?
7. Is the write append-only, conflict-ignore, or mutable?
8. Which downstream packages need the data?
9. Does replay reproduce the same view at a historical `as_of`?
10. Which service-backed test proves the migration and query behavior?

## Tests that matter

Strong tests include:

- property-based version selection against a simple oracle;
- before/at/after availability boundary assertions;
- restatement/revision replay;
- source-specific delayed availability (for example, filing date vs transaction
  date);
- deterministic source-version tie-breaking;
- timezone rejection;
- import-linter negative tests that inject a forbidden raw DB import.

## Evidence language

Use:

- `PASS` only for a command actually executed successfully;
- `SKIPPED` for an intentionally skipped test reported by the runner;
- `BLOCKED` when required environment/services prevent execution;
- `REPORTED` for a claim found only in repository documentation.

Do not collapse these states.
