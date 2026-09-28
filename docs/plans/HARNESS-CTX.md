# Harness Context Pass Plan

## Objective

Complete the context-engineering specialist pass after the agents and skills passes. Reduce context loading and state-recovery risk only where repository evidence shows that a decision changes, preserve established agent/skill contracts, write `docs/harness-improvement/04-context-pass.md`, and leave whole-harness/runtime questions for FINAL_INTEGRATION.

## Requirements

1. Reconcile the T0 baseline and reports `02-agents-pass.md` and `03-skills-pass.md` with current repository/runtime evidence.
2. Preserve product authority, exact approval semantics, phase gates, all eight invariants, and established skill and reviewer-agent contracts.
3. Address the unconditional `docs/PROGRESS.md` read/update wording only if the change retains sufficient phase-state recovery while preventing unrelated tasks from creating misleading history.
4. Resolve AGENTS/CLAUDE duplication only if the cross-runtime loading dependency can be stated and validated without losing policy for either runtime; otherwise document the evidence and leave the duplication for FINAL_INTEGRATION.
5. Inspect skill references and task-state placement for hidden or fragile context dependencies; make no speculative runtime/config/MCP changes.
6. Create the requested context-pass report with before/after topology, dispositions, evidence, remaining risk, and FINAL_INTEGRATION handoffs.
7. Update `docs/PROGRESS.md` only if this pass changes a durable project decision/status worth recording; keep the entry concise.

## Planned scope

- Possible edits: `AGENTS.md`, `CLAUDE.md`, `docs/PROGRESS.md` (only if warranted), and `docs/harness-improvement/04-context-pass.md`.
- Do not modify skills, agent definitions, project configuration, MCP/tool exposure, application source, tests, or pre-existing dirty files unless inspection reveals a direct context correctness defect that cannot be repaired in the planned surfaces; document such a finding instead.
- Preserve existing user changes and do not commit, push, create a PR, or perform external side effects.

## Validation

- Run the context skill's `audit_harness.py` before and after changes.
- Run any applicable AGENTS and skill validators again only if their files/loading boundaries change.
- Check edited Markdown references and instructions, compare before/after instruction-chain and topology evidence, run `git diff --check`, and inspect the final diff for dropped requirements, stale references, authority changes, and unrelated edits.
- Do not claim observed Codex discovery, cross-runtime loading, routing, or compaction behavior without direct runtime evidence.

## Approval gate

Implementation starts only after the owner's entire response is exactly `APPROVED`, apart from surrounding whitespace. Corrections, questions, conditions, or other text require revising this plan in place and resubmitting it.
