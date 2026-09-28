# Context Engineering Pass

**Date:** 2026-09-27
**Inputs:** `T0-baseline.md`, `02-agents-pass.md`, `03-skills-pass.md`, current root instructions and worktree
**Scope:** final specialist mutation pass for context placement; whole-harness runtime verification remains with FINAL_INTEGRATION.

## Outcome

The pass made one scoped context/state change: phase progress is read and updated when phase evidence or a project decision needs it, rather than read and written on every task/session. The matching instruction was updated in both Codex (`AGENTS.md`) and Claude (`CLAUDE.md`) entrypoints to prevent cadence drift. Historical phase details and product invariants remain intact.

AGENTS/CLAUDE policy consolidation was not attempted. They are separate runtime entrypoints, and this environment did not establish that either runtime reliably loads the other file. Replacing one with a pointer could trade visible duplication for a hidden loading dependency. They remain semantically aligned on the updated progress rule; broader consolidation is reserved for FINAL_INTEGRATION if runtime evidence supports it.

No behavioral improvement is claimed from static edits. No runtime routing, compaction, or cross-runtime loading trial was run.

## 1. Before/after context topology

| Layer | Before | After | Placement rationale / evidence |
|---|---|---|---|
| Always-loaded repository instructions | `AGENTS.md` (3,574 bytes at baseline); product authority, eight invariants, phase workflow, stack and commands. `CLAUDE.md` (~3,564 bytes) repeats the same policy for a separate runtime. | Same surfaces and policy coverage. Both now say to read current status/relevant decisions before phase work and update progress only for phase status, gate evidence, open issues or project decisions. | Progress navigation is persistent policy because it affects phase recovery. Per-session history writes were not decision-bearing and could create misleading churn. Other content remains until runtime evidence supports a safe cross-runtime single source. |
| Directory-scoped instructions / project Codex config | No nested `AGENTS.md`/override or repository `.codex` config/rules. | Unchanged. | No local evidence justifies adding narrower policy/config. |
| Skills and references | 12 project skills, conditional bodies and branch references; changed skill pass refined six route boundaries. | Unchanged by this pass. | The skills pass established these contracts; references remain conditionally loaded. No stale or broken local reference was found by the available package validators. |
| Agent/delegation context | One read-only code-auditor reviewer contract; no project `.codex/agents` definitions. | Unchanged. | Agents pass found no static contract defect. Runtime discovery remains unverified. |
| Tools/MCP | No repository-local MCP declarations; active user/global tool exposure not inspected. | Unchanged. | No demonstrated need for more tools. User-level config stays outside scope. |
| Durable state | `docs/PROGRESS.md` is 57,503 bytes and combines a current phase table with extensive decision/history detail; phase plans and `docs/plans/README.md` carry phase execution state. Root instruction directed every session to read and update the full progress artifact. | Current phase table and history are preserved. Instructions point phase work to current status/relevant decisions and updates only when phase evidence or decisions change. No generic checkpoint was introduced. | Existing plan convention is sufficient for phase work. Reading the full historical log every task was not required to recover current state; no compaction experiment was performed. |
| Deterministic checks and feedback | Phase verifier and skill-specific preflights/validators/evaluations exist. No repository-wide routing evaluation; pass reports list static and runtime limitations. | Unchanged. | No new script is justified for the semantic task-state scope rule. Existing exact checks should remain deterministic; mapping all eight product invariants to current checks is FINAL_INTEGRATION work. |

The audit script reports a 3,574-byte effective project instruction chain before this edit and no configured project-doc limit. Actual context injection, effective context window, and loaded skill/tool schemas were not measured. Its simple frontmatter parser reports `description_chars: 1` for most folded YAML descriptions; this is a known parser limitation recorded by `03-skills-pass.md`, not evidence of one-character routing descriptions.

## 2. CONTEXT_PASS findings and dispositions

| Finding | Disposition | Decision affected / evidence class |
|---|---|---|
| T0 F2 — AGENTS/CLAUDE duplicate policy | **Partially addressed; duplication retained.** Both instructions now share the narrowed progress rule; other policy remains duplicated. | **Observed repository fact:** separate entrypoint files contain nearly the same policy; prior agents report identify CLAUDE as a separate runtime surface. **Unverified runtime:** whether one can reliably load/reference the other. Avoided a pointer-only edit that could hide mandatory policy. |
| T0 F5 — progress read/update scope and generic task state | **Addressed at instruction scope; no new state file.** Only phase work requires status/relevant-decision review; progress updates track changed phase evidence/issues/decisions. Ordinary tasks consult relevant current-state entries as needed. | **Observed repository fact:** progress is 57,503 bytes, combines a current phase table and lengthy dated history, and existing plans are the durable phase workflow. **Source-derived heuristic:** update only when state changes prevents attempted-action/session-history churn. No behavioral metric inferred. |
| 02-agents-pass: literal plan placeholder warning | **Not changed.** The `docs/plans/P<n>.md` path is an intentional pattern in a phase instruction, not a broken concrete reference. | **Observed repository fact:** validator warning is for literal placeholder. No path repair is needed. |
| 02-agents-pass: inherited parent-directory `AGENTS.md` scope | **Reserved for FINAL_INTEGRATION.** | The prior report describes conflicting repository purpose. Parent file is outside the workspace and its shared-scope intent is unknown; this pass did not alter it. |
| 03-skills-pass: skill-level repetition of policy | **Preserved.** No skill body was edited. | Skills provide workflow-local boundaries and report that domain invariants and phase-specific handoffs remain relevant when activated. Removing repeated lines without a reliable shared-policy load path could weaken workflow contracts. The six routing edits from the previous pass were left intact. |
| T0 F1/F4/F6/F7 — runtime discovery, invariant/check traceability, whole-harness evaluation, phase evidence | **Reserved for FINAL_INTEGRATION.** | Static inventory cannot establish runtime injection, empirical outcomes, or current product gate results. No new pass changed product gates or status. |

## 3. Placement changes and rationale

- **Persistent instructions:** Kept the product authority, eight invariants, exact approval rule, phase gate definition, and project boundaries in both current runtime entrypoints. Their absence can change consequential work; they are not safe to move behind an optional skill route.
- **Progress navigation:** Narrowed the always-on instruction to retrieval of current phase state/relevant decisions at the point of phase work and state updates only when tracked state changes. This preserves recovery while avoiding an instruction to load/write a long history for unrelated tasks.
- **CLAUDE runtime entrypoint:** Retained its policy copy because replacing it with an `AGENTS.md` pointer would create a runtime loading dependency not established here. Cross-runtime duplication remains a maintenance cost, not a proven always-loaded context cost.
- **Skills/references/agent-specific context:** No placement change. The preceding skills/agents passes own these surfaces and no evidence showed a defect requiring a contract change.
- **Durable state:** Kept existing phase plans and progress table/history; did not add a generic task checkpoint. This task is bounded and the requested report is its durable handoff.
- **Deterministic enforcement/tool exposure:** No change. No prose-only exact operation was found in the narrow task-state finding that warranted a new script. No MCP/tool exposure changes were supported by repository-local evidence.

## 4. Deleted duplication or stale material

No instruction or historical entry was deleted. The obsolete unconditional session read/update clause was replaced in both runtime entrypoints. This removes the rule that encouraged unrelated session-history writes, while retaining a current-state lookup at the phase decision point. Cross-file policy duplication itself remains pending runtime evidence.

## 5. Durable/deterministic mechanisms

No new mechanism was added. Existing `docs/plans/README.md`, numbered plans, `docs/PROGRESS.md`, `scripts/verify_local.py`, and skill-specific validators remain the state and feedback mechanisms. The phase verifier's PASS/BLOCKED/FAIL semantics and historical gate caveats were not modified. The report itself records this pass and its unresolved dependencies for handoff.

## 6. Validation evidence

- Context skill `audit_harness.py --root . --cwd . --format markdown` — run before and after. Pre-edit: effective project instruction chain **3,574 bytes**, one root AGENTS file, no repository Codex config, rules, or MCP servers. Post-edit: same topology; instruction chain **3,792 bytes** (**+218 bytes**). This is the cost of clarifying state scope, not a token-minimization result.
- `codex --version` — `codex-cli 0.157.1`. This establishes installed CLI version only; it does not show runtime discovery/injection.
- `python .agents/skills/repository-agents-md/scripts/validate_agents_md.py AGENTS.md` — **PASS**, no heuristic findings.
- `python <installed-harness-skill-engineer-skill>/scripts/inspect_skill.py <skill-dir>` for all 12 project skill packages — **12/12 exit 0**, no errors, missing references, or suspicious artifacts. The simple parser's folded-description length limitation remains as documented by the skills pass; this run does not validate runtime routing.
- `git diff --check` — **PASS**; Git printed pre-existing LF/CRLF conversion notices for dirty files.
- Markdown reference review: both instruction files still point to existing `docs/PROGRESS.md`; plan `docs/plans/HARNESS-CTX.md` and pass reports exist. The literal `P<n>` template path remains intentional.
- Pre-existing worktree changes were present before edits. This pass did not modify prior skill files, application code, tests, or unrelated HARNESS-001 files.

No product phase verification was run: this pass changes context guidance, not product behavior or a numbered phase. No claim is made that P6–P8 gates pass.

## 7. Context intentionally left larger

- The eight invariants stay in always-available repository policy because a skill may not activate before a violating implementation decision.
- Exact phase approval and phase-gate policy remain persistent. They change whether implementation starts and what qualifies as done.
- CLAUDE.md remains a nearly complete parallel instruction file until runtime tests establish a reliable bridge path. Static byte counts do not establish that removing it would reduce effective context.
- `docs/PROGRESS.md` history remains intact for provenance. Only its unconditional per-session retrieval/update instruction was narrowed; history compaction would need a separate evidence-backed decision and archival policy.

## 8. Unresolved runtime assumptions

- Whether Codex CLI 0.157.1 loads this repository's `AGENTS.md` and skill metadata as expected in the active profile.
- Whether any other runtime reliably loads `AGENTS.md`, and whether `CLAUDE.md` is the only relevant project entrypoint for Claude.
- Whether the installed runtime loads `CLAUDE.md` and would reliably follow a pointer to `AGENTS.md`; not tested, so policy consolidation remains unsafe to claim.
- Effective user-level skills, plugins, MCP servers, tools, hooks, and context limits; intentionally not inspected.
- Whether narrowing progress navigation measurably improves task outcomes or recovery after compaction. This was reasoned from repository structure, not tested behaviorally.

## 9. FINAL_INTEGRATION handoff

1. Probe effective instruction and skill discovery under the actual Codex profile, and document which context is injected versus merely present in the repository.
2. Determine Claude/other runtime instruction loading before deciding whether the duplicate `CLAUDE.md` policy can become a reliable pointer to shared policy.
3. Resolve the inherited parent-directory `AGENTS.md` scope with the workspace owner; no parent edit is in scope here.
4. Map each of the eight persistent invariants to a named falsifiable test/check or explicitly state where coverage is not proven.
5. Keep the skills pass's static G1–G4 separate from unvalidated G5; perform representative positive/negative/neighbor routing probes if the runtime can expose injection evidence.
6. Reconcile phase preflight/history evidence with current verifier behavior; this pass ran no phase gate and does not change P6–P8 status.

No commits, pushes, PRs, external communications, live API calls, or other external side effects were performed.
