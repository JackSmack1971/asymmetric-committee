# AGENTS and Subagent Pass

**Date:** 2026-09-27
**Baseline:** `docs/harness-improvement/T0-baseline.md`
**Scope:** repository instruction cascade and explicit custom agent contracts. This pass does not own whole-context pruning, skill routing redesign, or runtime integration.

## Outcome

No T0 finding is assigned to `AGENTS_PASS`. F1 (runtime discovery) and F4 (invariant-to-check traceability) belong to `FINAL_INTEGRATION`; F2 (AGENTS/CLAUDE duplication) and F5 (generic task-state and progress scope) belong to `CONTEXT_PASS`; F3 belongs to `SKILLS_PASS`; F6 and F7 belong to `FINAL_INTEGRATION`. Current inspection found no separate agent-system defect that justifies changing the instruction cascade or adding/removing an agent. The smallest coherent change is this durable pass report; agent contracts and persistent instructions are unchanged.

## Inventory and effective scope

The T0 inventory was checked against the current worktree. The repository contains one root `AGENTS.md`, applying to the repository, with no nested `AGENTS.md` or `AGENTS.override.md`. An inherited parent-directory `AGENTS.md` also exists. It describes a different writing-framework repository and directs work toward that project, so it conflicts with this repository's purpose; the explicit user task and repository-local instructions govern this pass. This cross-repository parent-scope collision is recorded for `FINAL_INTEGRATION` to determine whether the parent file's placement/scope is intentional; it was not edited. Root `CLAUDE.md` is a parallel instruction surface for a different runtime and substantially duplicates the repository rules; its reconciliation is handed to `CONTEXT_PASS`. No `.codex/` directory or project `.codex/config.toml` was found. There are no `.codex/agents/*.toml` contracts.

The sole explicit custom agent contract is `.agents/skills/code-auditor/agents/audit-reviewer.yaml`, reachable only through the `code-auditor` skill's audit workflow. The `agents/openai.yaml` files in skills are skill metadata, not subagent contracts. Codex discovery and effective loading of either the skill-local reviewer or project instructions were not behaviorally verified. No parent-directory instruction file was found in the repository inventory; this pass did not inspect user-level runtime configuration.

### Contract ledger

| Scope / agent | Job and activation | Inputs | Authority and prohibited scope | Outputs and verification | Failure and handoff |
|---|---|---|---|---|---|
| Root `AGENTS.md` | Set repository-wide product authority, invariants, phase workflow, and verification expectations for repository work. | Blueprint, progress record, phase plan, and relevant code/tests. | Repository policy and local development guidance; it does not supersede higher-level instructions. It gives no agent delegation authority. Detailed scope interactions with read-only tasks and parallel instruction files are not resolved here (CONTEXT_PASS). | Requires phase verification through `scripts/verify_local.py P<n>` before phase completion. It asserts each invariant has a test but provides no test map (F4, FINAL_INTEGRATION). | Requires stopping and proposing a spec edit for ambiguous/wrong spec; missing service/tool outcomes are not fully specified here. Progress update is unconditional in wording and needs scope reconciliation (F5). |
| `.agents/skills/code-auditor/agents/audit-reviewer.yaml` | Independently adjudicate proposed consequential code-audit conclusions when a bounded fresh-context challenge materially improves evidence quality. | Required: audit mode, scope, proposed conclusions, evidence bundle. Optional: baseline, requirements, verification results, framework claims. | Read-only review; may inspect supplied evidence and challenge conclusions. Must not edit source/tests/config/Git/external systems, request broader permissions, accept confidence/majority as evidence, or substitute a broad audit absent a concrete scope defect. | Returns per-finding status (`CONFIRMED`, `DOWNGRADE`, `REJECT`, `INSUFFICIENT_EVIDENCE`) with rationale/evidence/assumptions, plus verdict review status and evidence. The returned claims are falsifiable against cited supplied evidence; the YAML does not itself enforce schema or runtime invocation. | Missing/contradictory evidence reduces confidence or yields `INSUFFICIENT_EVIDENCE`; returns the smallest adjudication for the primary auditor. Runtime discovery and execution remain UNVALIDATED. |

The reviewer contract is sufficiently bounded on static inspection: distinct independent-verification value, explicit read-only authority, required inputs, structured output, prohibited scope, and conservative missing-evidence behavior are present. No delegation tier or new agent is warranted by current evidence. Handoffs are structured in the YAML contract; runtime conformance is unvalidated.

## T0 findings addressed, rejected, or reclassified

| Finding | Disposition | Evidence / owner |
|---|---|---|
| F1 — Runtime discovery and precedence assumed | Carried, not addressed here. | T0 assigns it to `FINAL_INTEGRATION`. Current static inventory confirms absent project `.codex` configuration and the skill-local reviewer path, but cannot prove runtime loading. |
| F2 — AGENTS/CLAUDE duplication | Reclassified to `CONTEXT_PASS`, not duplicated in this pass. | Current `AGENTS.md` and `CLAUDE.md` repeat the same core rules. The baseline assigns reconciliation to CONTEXT_PASS. |
| F3 — Skill boundary overlap | Carried to `SKILLS_PASS`. | Outside the agent contract surface. |
| F4 — Invariant-to-check traceability | Carried to `FINAL_INTEGRATION`. | Root AGENTS asserts eight invariants each have a test, without naming checks. This pass did not trace product tests or rewrite product policy. |
| F5 — Generic task checkpoint / progress history scope | Carried to `CONTEXT_PASS`. | Root AGENTS says to read and update progress each session; the scope for read-only and non-phase tasks needs whole-context reconciliation. |
| F6 — No whole-harness evaluation | Carried to `FINAL_INTEGRATION`. | No runtime routing or task-quality evaluation was added or inferred from static inspection. |
| F7 — Phase evidence caveats | Carried to `FINAL_INTEGRATION`. | Historical progress and pre-existing worktree notes are not current gate runs; no phase verification was run in this pass. |

No finding was rejected as factually false. Findings outside this pass are carried under their existing single-owner assignments, not silently transferred.

## Exact changes

- Added this pass report only.
- No `AGENTS.md`, `CLAUDE.md`, agent YAML, `.codex` configuration, skill, or application file was edited. No agent was added or removed.
- The worktree was already dirty at pass start. Pre-existing changes in `docs/PROGRESS.md`, `docs/claude-session-friction-report.md`, `docs/plans/README.md`, `tests/store/test_import_rules.py`, `docs/plans/HARNESS-001.md`, `scripts/check_python_edits.py`, `scripts/preflight_goal.py`, `tests/scripts/`, and `tests/store/import_linter_fixtures.py` were preserved and not attributed to this pass.

## Validation evidence

- `python <installed-harness-agents-engineer-skill>/scripts/inventory_agent_surface.py --repo . --json` — PASS; reports one AGENTS document, zero subagent TOMLs, no Codex config. The skill-local YAML was separately found and read because it is outside that inventory script's `AGENTS`/`.codex/agents` counts.
- `python <installed-harness-agents-engineer-skill>/scripts/validate_agent_surface.py --repo .` — PASS (0 errors), with one warning: `AGENTS.md` reference `docs/plans/P` does not exist. This is the literal `P<n>` placeholder in the existing instruction, not a newly introduced path. It is reported rather than changed because the instruction-scope cleanup belongs to CONTEXT_PASS.
- No tests, local phase gate, runtime agent invocation, or routing probe was run. Static inspection is not empirical proof of runtime discovery or improved agent outcomes.

## Remaining risks and downstream handoffs

- **SKILLS_PASS:** keep the `code-auditor` skill's use of the reviewer aligned with this bounded adjudication contract; skill-level routing/description changes belong to that pass.
- **CONTEXT_PASS:** reconcile AGENTS/CLAUDE duplication, unconditional progress-update wording, broad phase-only rules, and the literal plan placeholder warning without changing product authority or exact approval semantics.
- **FINAL_INTEGRATION:** verify actual Codex discovery/precedence and, if feasible, invoke a representative bounded reviewer workflow; map the eight asserted invariants to current falsifiable checks; report phase state using current PASS/FAIL/BLOCKED/unknown evidence. Resolve whether the inherited parent-directory `AGENTS.md` should apply to this repository: its writing-framework purpose conflicts with this repo's local purpose and instructions. No parent-file edit was made because it is outside the repository target and its intended shared scope is unknown.
- Runtime discovery of `AGENTS.md`, skill-local agents, and the reviewer YAML, plus compliance of a runtime invocation with the YAML contract, remain **UNVALIDATED**. No measured improvement claim is made.
