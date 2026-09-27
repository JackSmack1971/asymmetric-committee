# Model-input leak classes

This reference is derived from the repository-history workflow. Verify live
implementation details in the active checkout before changing code.

## Identity leakage

Forbidden examples may include ticker, legal/company name, CIK, named
executive/person, colloquial short name, or trivially reversible identity token.

The history records alias contracts/config, PERSON aliases, hashed executive
tokens, and short-name/stop-word coverage.

## Numeric/date leakage

The history records explicit scrubbing of currency values, share counts, dates,
and other raw numeric values. Do not generalize this into "all numbers are
forbidden" unless the active spec says so.

## Partition leakage

An agent can receive only its intended partition. Test partition absence
separately from renderer sanitization.

## Coverage leakage

Correct rendering for known aliases does not cover missing short names or newly
introduced entities. Coverage guards are therefore part of anonymization.

## Point-in-time aliases

When alias history is modeled, resolve aliases at decision `as_of`; do not leak
future identity knowledge into historical prompts.

## Transformation properties

Evaluate determinism, non-obviousness, collision behavior, stability scope,
evidence-link compatibility, and whether raw identifiers are embedded.

## Negative-test pattern

Maintain an explicit forbidden set of raw values. Render/partition the input,
assert none appear, and assert the safe replacement appears when a reference is
still required.

## Manual dependency

The history notes colloquial short names can require manual configuration.
Automated tests prove current configured coverage only.
