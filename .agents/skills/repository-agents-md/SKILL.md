---
name: repository-agents-md
description: Analyze a software repository and create or update its root AGENTS.md with durable rules that should generally apply across repository work. Use when asked to establish, audit, regenerate, or improve root-level Codex repository guidance. Do not use for task-specific implementation plans, one-off operating procedures, or narrow conditional workflows that belong in a separate skill or deeper scoped AGENTS.md.
---

# Repository AGENTS.md

## Purpose

Create or update the repository-root `AGENTS.md` so it answers one question:

> **What rules should generally apply whenever an agent works in this repository?**

The output is persistent repository guidance, not an operations manual. Preserve useful agent judgment and keep conditional workflows out of the always-loaded root context.

## Mandatory workflow

### 1. Establish scope and instruction authority

- Work on the repository-root `AGENTS.md` only unless the user explicitly requests scoped files too.
- Before editing, inspect the repository's applicable instruction chain, including existing `AGENTS.md`, `AGENTS.override.md`, user instructions, and repository policies.
- Preserve unrelated working-tree changes.
- If no repository root can be established, stop and report the blocker rather than creating guidance in an arbitrary directory.

### 2. Gather evidence before writing rules

Inspect only enough repository evidence to support durable rules. Prefer authoritative and executable sources over inference.

At minimum, check when present:

- repository structure and major architectural boundaries;
- build, test, lint, type-check, formatting, packaging, and generation commands;
- dependency and runtime manifests;
- CI workflows and required gates;
- architecture, contribution, security, ownership, and development documentation;
- generated-file markers and source-of-truth relationships;
- existing root or scoped agent guidance;
- Git status/diff so pre-existing work is not misattributed.

Use `references/evidence-model.md` when evidence conflicts, is incomplete, or comes from multiple sources.

### 3. Classify candidate guidance

For every candidate instruction, decide whether it belongs in the root `AGENTS.md`.

**Include** guidance that is broadly and durably true across repository work, especially:

- architecture invariants;
- repository structure and source-of-truth locations;
- required or canonical validation commands;
- coding and formatting conventions that are not already enforced transparently by tooling;
- dependency restrictions or package-manager rules;
- review expectations that apply broadly;
- safety, security, privacy, or external-side-effect constraints;
- ownership or escalation information supported by repository evidence;
- repository-level definition of done.

**Do not include**:

- task-specific plans or current implementation status;
- transient branch, issue, incident, or release details;
- generic software-engineering advice;
- rules already guaranteed mechanically when restating them adds no decision value;
- speculative conventions inferred from a few files;
- long procedural workflows that activate only under a recognizable condition.

For a conditional procedure, apply the skill-extraction test in `references/skill-boundary.md`. If it belongs in a skill, keep at most a short routing pointer in `AGENTS.md` when that pointer is broadly useful.

### 4. Resolve update behavior

If root `AGENTS.md` does not exist, create it from repository evidence.

If it exists:

- retain still-valid, evidence-backed rules;
- remove or revise stale, contradicted, duplicated, generic, or overly procedural guidance;
- preserve intentional project policy even when it differs from common practice, unless repository authority clearly supersedes it;
- do not silently weaken safety, approval, architectural, or validation constraints;
- do not copy nested `AGENTS.md` rules upward unless they truly apply repository-wide.

When a consequential existing rule conflicts with current repository evidence and authority is unclear, report the conflict rather than guessing.

### 5. Write the root file for high signal density

Prefer a compact structure such as:

1. **Repository map / architecture**
2. **Always-applicable engineering rules**
3. **Validation / definition of done**
4. **Safety / dependency / external-action constraints**
5. **Pointers to deeper authoritative docs or skills**

Adapt headings to the repository; do not force empty sections.

Rules should be specific enough to change behavior. Prefer:

- `Run uv run pytest tests/unit/... for Python changes; broaden when shared contracts are touched.`

over:

- `Test your work thoroughly.`

Prefer contextual pointers:

- `Use docs/architecture.md when changing service boundaries.`

over:

- `Read docs/architecture.md before every task.`

Do not target an arbitrary line count. The file must be short enough to earn its always-on context cost. If a section becomes a procedure, extract or recommend a skill.

### 6. Verify before declaring completion

After editing:

- reread the final root `AGENTS.md` in full;
- verify every non-obvious factual claim against repository evidence;
- verify commands, paths, and filenames exist or are explicitly documented;
- check that no task-specific or transient state leaked into persistent guidance;
- check that detailed conditional workflows were removed or converted to skill recommendations;
- inspect the diff and confirm only intended files changed;
- run `python scripts/validate_agents_md.py <repo-root>/AGENTS.md` when Python is available.

The validator is a linting aid, not repository authority. A validator pass does not prove factual correctness.

## Decision envelope

### Mandatory

- Base durable rules on repository evidence.
- Keep root guidance broadly applicable.
- Preserve higher-priority instructions and unrelated user changes.
- Verify commands, paths, and claims before completion.
- Distinguish persistent policy from conditional workflow.

### Prohibited

- Do not invent repository facts, commands, ownership, policies, or architecture.
- Do not turn root `AGENTS.md` into a comprehensive runbook.
- Do not encode temporary task state as persistent policy.
- Do not weaken consequential existing constraints without authoritative evidence.
- Do not perform unrelated repository cleanup or implementation work.

### Judgment

Use judgment to decide how much evidence to inspect, how to group rules, and whether a candidate rule earns always-on context. Favor deletion or deferral when a rule is generic, redundant, low-value, or conditional.

## Failure handling

- **Missing or contradictory docs:** inspect executable configuration and CI; report unresolved authority conflicts.
- **Pre-existing failing checks:** distinguish them from failures introduced by the `AGENTS.md` change; do not claim repository health.
- **No clear canonical command:** describe only what is supported by evidence, or omit the command and report the gap.
- **Monorepo with subsystem-specific rules:** keep root invariants at root and recommend scoped `AGENTS.md` files for rules that apply only below a directory.
- **Workflow discovered during analysis:** recommend a separate skill if it passes the skill-extraction test; do not embed the full procedure.
- **Sensitive or destructive operations:** document durable permission/safety boundaries only when supported; never execute those operations merely to validate guidance.

## Completion evidence

Completion requires:

- a created or updated root `AGENTS.md`;
- a cleanly reviewed diff limited to intended guidance changes;
- evidence that non-obvious rules, commands, paths, and ownership claims are supported;
- no unresolved authority conflict affecting a consequential rule;
- conditional workflows either excluded or explicitly identified as skill candidates.

Report unresolved or unverifiable items instead of presenting them as settled policy.
