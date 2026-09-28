# Phase delivery checklist

Use this as a compact execution checklist after activating the skill.

## Baseline
- [ ] Blueprint read for active phase.
- [ ] `docs/PROGRESS.md` read.
- [ ] Phase plan read or absence confirmed.
- [ ] Active instructions read.
- [ ] `Makefile` gate inspected.
- [ ] Branch/HEAD/status inspected.
- [ ] Unrelated dirty changes identified and preserved.

## Reconciliation
- [ ] Deliverable mapped to exact spec sections.
- [ ] Existing code/tests/config compared with plan/spec.
- [ ] Mismatches classified: implementation / plan / spec / evidence.
- [ ] Ambiguous product or methodology decisions surfaced, not guessed.
- [ ] Plan/spec updated before dependent implementation when required.

## Plan
- [ ] Scope + non-goals.
- [ ] Invariants + tests.
- [ ] Ordered coherent slices.
- [ ] Exact gate + prerequisites + proof.
- [ ] Spec issues / decisions / blockers.

## Implementation
- [ ] Closest existing pattern inspected.
- [ ] Invariant tests added/strengthened.
- [ ] Minimal production delta.
- [ ] Generated artifacts regenerated, not hand-edited.
- [ ] No live paid APIs in tests.
- [ ] Diff reviewed after each slice.

## Verification
- [ ] Focused tests.
- [ ] Subsystem tests.
- [ ] Required lint/type/import/schema checks.
- [ ] Exact `make gate-P<n>` executed.
- [ ] Missing service/tooling is reported as blocked/not-run.

## Closure
- [ ] `docs/PROGRESS.md` matches observed evidence.
- [ ] Plan reflects final implementation.
- [ ] Final diff/status reviewed.
- [ ] Local / committed / CI / PR / merge states kept distinct.
