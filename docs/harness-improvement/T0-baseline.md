# T0 Harness Baseline

**Audit date:** 2026-09-27
**Scope:** opening, read-only whole-harness inventory and campaign plan
**Baseline identity:** worktree at audit time; pre-existing dirty state is itemized below. The later specialist passes should record their own starting revisions and compare mechanisms and outcomes against this snapshot.

## 1. Harness topology

### Observed surfaces and authority

| Surface | Observed T0 role and authority | Evidence / status |
|---|---|---|
| Root instructions | `AGENTS.md` is the only repository `AGENTS.md` or `AGENTS.override.md` found. It sets the blueprint as product authority, points to `docs/PROGRESS.md`, lists invariants and workflows. | 3,574 bytes; instructions apply repository-wide if recognized by the active Codex runtime. No nested scope overrides. |
| Parallel tool instructions | `CLAUDE.md` repeats the project instructions and appears intended for Claude Code. | 3,564 bytes. Substantial policy duplication with `AGENTS.md`; intended runtime scope differs, but cross-agent drift is possible. |
| Repository Codex config | No `.codex/` files/directories found in the repository. | Explicitly absent at T0. No project-local Codex config, rules, MCP declarations, or command wrappers found. |
| Repository skills | `.agents/skills/` contains 12 `SKILL.md` packages. Each has an `agents/openai.yaml` display/default-prompt/implicit-invocation metadata file. | Ten `asymmetric-*` project skills plus `code-auditor` and `repository-agents-md`. Exact names are listed below. |
| Skill routing and content | Descriptions give positive triggers and often exclusions. Several packages provide preflight scripts and selective references; predictive-validity audit includes evals and telemetry. | Skills are selected by runtime routing; repo does not contain a central skill router/index or repository-owned routing trial. Implicit invocation is enabled in package metadata. Runtime selection itself was not tested. |
| Agent definitions | One repository agent definition: `.agents/skills/code-auditor/agents/audit-reviewer.yaml`. | It defines a read-only adjudicator with input/output contracts. Other `agents/openai.yaml` files are skill UI metadata, not subagents. No `.codex/agents` definitions. |
| Plans and state | `docs/plans/README.md` defines the phase-plan workflow; numbered plans and `docs/PROGRESS.md` store execution plans and phase decisions. | Progress says read/update each session. The present task explicitly bars state changes, so no progress update was made. `docs/PROGRESS.md` is a large historical log as well as current status. |
| Deterministic tooling | `scripts/verify_local.py` is identified by AGENTS as phase gate authority; `scripts/preflight_goal.py` and `scripts/check_python_edits.py` exist in pre-existing dirty state. Skills contain focused `*_preflight.py` scripts, package/eval validators and `eval_telemetry.py`. | Inventory found one root harness-tooling item (`tests/scripts/test_harness_tools.py`); additional pre-existing untracked files may not appear in tracked-file inventory. No checks were run in this pass beyond the inventory and CLI inspection. |
| Evaluation/telemetry | The predictive-validity skill has an eval corpus, runner/scorer, report validator and event/run schemas. Code-auditor has a validator and tests. | These evaluate particular skills/audits, not overall Codex outcomes or routing. No repository-wide harness feedback loop was found. |
| Tools/MCP | No repository-local MCP configuration. | Codex CLI 0.157.1 is installed and exposes `codex mcp`, `exec`, `features`, and `doctor` commands. User-level Codex home contains configuration and MCP state, but credentials/config contents were not inspected; effective MCP servers, hooks, plugins, profiles, and tool set remain unverified. |
| Product authority and verification | `docs/asymmetric-committee-blueprint.md` is explicitly authoritative. `scripts/verify_local.py` is documented as phase verification entrypoint; Make targets are aliases. | `docs/PROGRESS.md` records P6 gate blocked, P7/P8 gates stubs, and historical results with service caveats. CI exists at `.github/workflows/ci.yml`; this is product CI, not evidence of harness validation by itself. |

The 12 skill names at T0 are: `asymmetric-contract-schema`, `asymmetric-decision-persistence`, `asymmetric-fixture-ingestion`, `asymmetric-layered-verification`, `asymmetric-model-anonymization`, `asymmetric-model-dispatch`, `asymmetric-phase-delivery`, `asymmetric-phase-source-control`, `asymmetric-point-in-time-store`, `asymmetric-predictive-validity-audit`, `code-auditor`, and `repository-agents-md`.

### Effective path (observed versus assumed)

1. User/developer/session instructions and the active Codex installation govern this conversation. The repository `AGENTS.md` is the intended project-wide instruction authority. The task-provided copy matches the checked-in root file in relevant content.
2. No nested AGENTS cascade, repository Codex config, local rules, or local MCP declarations were found to refine or override that root policy.
3. `.agents/skills/*/SKILL.md` descriptions and `agents/openai.yaml` metadata are the repository's workflow-routing surfaces. Codex 0.157.1 is present, but automatic discovery, precedence, and implicit invocation were not behaviorally exercised; directory convention alone is not proof of effective loading.
4. A routed skill may refer to its local references/scripts. The code-auditor adjudicator is the only explicit custom subagent contract found. Whether/how Codex discovers this definition is unverified.
5. For phase work, the intended durable path is plan (`docs/plans/README.md` and phase plan) → implementation → `scripts/verify_local.py P<n>` → `docs/PROGRESS.md`. This task is outside numbered phase delivery; read-only scope supersedes the ordinary progress-update instruction for this audit.

## 2. T0 baseline

### Inventory counts

- Root instruction files: 1 (`AGENTS.md`); parallel `CLAUDE.md`: 1.
- Nested instruction/override files: 0.
- Repository `.codex/` configuration/rules/agents: 0.
- Skills: 12 packages, 12 `SKILL.md` entrypoints, 12 `agents/openai.yaml` metadata files.
- Explicit custom agent definitions: 1 (`code-auditor/agents/audit-reviewer.yaml`).
- Repository-local MCP declarations: 0 found.
- Top-level durable phase-plan/progress surfaces: `docs/plans/README.md`, numbered/current plans, `docs/PROGRESS.md`.
- Repository-wide harness routing/evaluation suite: none found. Skill-specific evaluation and telemetry exists for predictive-validity audit; validation is skill-specific for code audit and harness utilities.

### Git/user state boundary

The worktree was already dirty before this report. `git status --short` showed modified `docs/PROGRESS.md`, `docs/claude-session-friction-report.md`, `docs/plans/README.md`, `tests/store/test_import_rules.py`, and untracked `docs/plans/HARNESS-001.md`, `scripts/check_python_edits.py`, `scripts/preflight_goal.py`, `tests/scripts/`, and `tests/store/import_linter_fixtures.py`. The existing diffs include a HARNESS-001 decision/evidence entry, plan-template additions and utility/test work. These are treated as pre-existing user changes, are not attributed to this audit, and must be preserved by all follow-on passes. This report is the only file this audit adds.

The skill inventory script reports 12 skills and 12 durable plans, one harness-tooling item, one instruction file and two knowledge items. Its durable-plan count includes the untracked HARNESS-001 plan. The script describes files; it does not establish Codex runtime behavior or quality.

### Runtime and validation evidence

- `python <installed-harness-engineer-skill>/scripts/inventory_harness.py --repo . --json` completed successfully and emitted the inventory above. The placeholder denotes the locally installed skill directory.
- `codex --version` returned `codex-cli 0.157.1`; `codex --help` exposed MCP, feature, doctor, plugin, execution and agent-session commands. This confirms a CLI is installed, not that repository skills/agent definitions or MCP tools load as intended.
- No tests, phase gate, live service checks, Git-changing commands, or external actions were run. Existing pass/fail claims in `docs/PROGRESS.md` and the dirty diff are historical/user-provided evidence, not checks performed for T0.
- No empirical before/after quality metric is available from static inspection. Do not claim a measured success-rate or context reduction.

### Baseline dimensions

| Dimension | T0 observation |
|---|---|
| Context duplication | Root `AGENTS.md` and `CLAUDE.md` repeat nearly all project rules. The root file is 3,574 bytes and relevant text includes invariants, phase planning, approval, preflight and verification. Actual always-loaded cost is runtime-dependent and not measured. |
| Duplicated authorities | Blueprint is named as product authority; AGENTS is general repo policy; progress is status/evidence; plan README is plan procedure. In practice several rules are duplicated in CLAUDE, skill documents, plans, and progress history. |
| Deterministic enforcement | A verifier and multiple skill preflights/validators exist, but the root invariants are described as tested rather than all mapped to a current, discoverable test/check inventory. Phase completion depends on verifier semantics and correct reporting of PASS/BLOCKED. |
| Durable state | Strong phase-oriented plan/progress artifacts; no generic active-task checkpoint beyond phase state. Long progress history and many plan files increase retrieval burden. |
| Feedback | Product tests/CI, local phase verification, skill-specific eval/telemetry and validators provide evidence. No single overall harness routing/outcome evaluation loop was found. |
| Failures and assumptions | Historical progress explicitly marks P6 blocked and P7/P8 stubs; earlier service checks include skips/unavailable Timescale cases. At T0 the actual service state and current verifier result are untested. |

## 3. Findings ranked by consequence

Severity: High = credible repeated path to wrong decisions/false confidence; Medium = meaningful reliability or maintenance risk; Observation = evidence gap/optimization pending runtime validation. Each finding has one primary owner only.

### F1 — Runtime discovery and precedence are assumed, not demonstrated

- **Severity/type:** High, unverified routing/runtime assumption.
- **Evidence:** No repo `.codex/`, rules, or MCP configuration; only root AGENTS. Skills/agent definitions depend on conventions and active runtime. CLI 0.157.1 exists, but no representative prompt checked which instructions, skills, or agent definitions it actually loads.
- **Consequence:** Later passes could change surfaces believed to be active or inert when their actual runtime behavior differs; skill routing claims may not reach the model.
- **Smallest intervention:** In the final integration pass, perform safe, representative runtime discovery/routing probes and document only observed effective paths; decide whether any missing project-local declaration is actually needed.
- **Falsifier:** Runtime evidence shows root instructions and intended skill metadata reliably load and route under the target invocation, with no missing declarations or precedence surprise.
- **Owner:** `FINAL_INTEGRATION`.

### F2 — Broad instructions and duplicated parallel-agent policy can drift

- **Severity/type:** Medium, duplication/drift risk.
- **Evidence:** `AGENTS.md` (3,574 bytes) and `CLAUDE.md` (3,564 bytes) share the same stack, invariants, planning, verification, goal-length and edit guidance. Root instructions are broad and always intended; the audit task itself required a read-only exception to ordinary progress updates.
- **Consequence:** Policy edits may land in one file but not the other; routine Codex tasks carry a relatively broad instruction set and distinctions between global policy and conditional phase workflow are less crisp.
- **Smallest intervention:** Identify which statements must stay global for Codex and which should be pointed to or scoped; preserve Claude parity where intentional and establish a low-cost drift check if justified.
- **Falsifier:** A line-by-line ownership map shows each duplication is intentional and routinely synchronized, and runtime context cost/routing provides no material downside.
- **Owner:** `CONTEXT_PASS`.

### F3 — Skill boundaries overlap at consequential shared workflows

- **Severity/type:** Medium, routing ambiguity risk.
- **Evidence:** 12 skills include cross-cutting `asymmetric-phase-delivery`, `asymmetric-layered-verification`, and `asymmetric-phase-source-control` alongside domain specialists such as decision persistence, model dispatch, fixture ingestion, point-in-time store, and contracts. Repository skills have no central routing index or measured classification benchmark. Descriptions contain useful exclusions, but many composite tasks naturally meet several triggers.
- **Consequence:** Agent may select only a generic phase skill or only one technical specialist, omitting specialist invariants or exact integration/gate expectations; implicit invocation adds runtime-dependent selection.
- **Smallest intervention:** Skills pass should map overlaps and define clear dominant triggers, boundaries and handoffs; retain the planned specialist order and avoid redesigning other surfaces here.
- **Falsifier:** A representative task matrix reliably selects complementary skills and their handoffs without materially conflicting instructions.
- **Owner:** `SKILLS_PASS`.

### F4 — Root invariants are not cross-referenced to one complete, current enforcement map

- **Severity/type:** Medium, mechanical-enforcement/legibility gap.
- **Evidence:** AGENTS says each of eight invariants has a test, but points at no test names/index. Existing test suite, schema checks and import-linter contracts may prove them, while skills add other preflights. T0 inventory does not establish exact current coverage or which checks are authoritative.
- **Consequence:** An agent can satisfy broad lint/tests yet miss a specialized invariant or overstate assurance; new contributors must reconstruct the map from tests and history.
- **Smallest intervention:** During final integration, trace each root invariant to its current falsifiable check and ensure gate claims distinguish local versus service-backed/partial coverage. Do not move product invariant definitions in specialist passes without evidence.
- **Falsifier:** A current machine-readable or concise maintained map demonstrates complete invariant → check → gate coverage and accurate failure semantics.
- **Owner:** `FINAL_INTEGRATION`.

### F5 — Phase state is durable but not a generic task checkpoint, and progress mixes state with history

- **Severity/type:** Medium, recovery/context risk.
- **Evidence:** `docs/PROGRESS.md` combines current phase table, decisions and extensive detailed history; plans cover numbered work. No root-level generic active task/decision checkpoint was found. The root instruction's “update at end” rule is unconditional, though this audit is read-only and cannot comply by mutation.
- **Consequence:** Non-phase work and long-running audits may rely on chat context; important current status can be harder to distinguish from historical notes. A literal update-at-end interpretation conflicts with read-only scopes unless scope hierarchy is clear.
- **Smallest intervention:** Context pass should clarify which durable mechanisms apply to all tasks versus phase work and how scoped read-only tasks reconcile the normal progress rule; avoid inventing a global state file unless recovery evidence warrants it.
- **Falsifier:** Existing project/runtime state or task conventions cover non-phase recovery and make current status immediately discoverable without duplicative state.
- **Owner:** `CONTEXT_PASS`.

### F6 — Evaluation is local to selected skills, not the harness as a whole

- **Severity/type:** Medium, evaluation gap.
- **Evidence:** Predictive-validity audit has corpus/runner/scorer/telemetry and code-auditor has a validation test suite; no common routing or harness evaluation across all 12 skills, instruction changes, or end-to-end task outcomes was found. The modified friction retrospective is pre-existing work with its own methodology and must be preserved, not treated as Codex-wide evidence.
- **Consequence:** Improvements can add process or alter routing without evidence that intended decisions improve or regress; static artifact counts cannot establish efficacy.
- **Smallest intervention:** Final integration should define a modest comparable evaluation/observability strategy using existing evidence where possible, with baseline and limits recorded; do not claim outcomes until sampled.
- **Falsifier:** Existing telemetry/evals outside repository scope demonstrably measure representative routing and task outcomes with reproducible baseline data.
- **Owner:** `FINAL_INTEGRATION`.

### F7 — Existing completion evidence has phase-specific and service/runtime caveats

- **Severity/type:** Observation / verification risk.
- **Evidence:** Progress records P6 blocked because its gate is unimplemented and P7/P8 as stubs; historical entries distinguish manual recipes, skips, absent `make`, TimescaleDB image/service limitations and pending CI. AGENTS says phase is done only if `verify_local.py P<n>` passes. New dirty docs also say HARNESS-001 P5 service-backed verification was unavailable.
- **Consequence:** Summaries or follow-on harness work could collapse historical/manual/partial evidence into a current pass or mistake a blocked product phase for a harness defect.
- **Smallest intervention:** Preserve status vocabulary and evidence provenance; final integration should verify the current verifier behavior and compare its output with progress claims only where safe and useful.
- **Falsifier:** A current authoritative run for each claimed phase exists, with output and service prerequisites recorded and no mismatch.
- **Owner:** `FINAL_INTEGRATION`.

## 4. Finding → specialist-pass ownership map

| Finding | Primary owner (exact label) | Expected pass responsibility | Dependencies / handoff |
|---|---|---|---|
| F1 | `FINAL_INTEGRATION` | Verify effective runtime loading/discovery and applicable precedence after specialist changes; resolve assumptions based on evidence. | Depends on final AGENTS, skills and context edits. |
| F2 | `CONTEXT_PASS` | Reconcile broad always-on policy, duplicate AGENTS/CLAUDE material, and scope exceptions while preserving intentional cross-runtime parity. | F3 boundaries inform whether workflow guidance belongs in skills. |
| F3 | `SKILLS_PASS` | Audit 12 routes for overlap, complementarity, exclusions, metadata and handoff behavior; preserve domain invariants. | F2 may adjust what should remain persistent policy. |
| F4 | `FINAL_INTEGRATION` | Check invariant-to-test/gate traceability and completion semantics across changed skills/context. | Depends on skills/context decisions; must not replace blueprint authority. |
| F5 | `CONTEXT_PASS` | Clarify durable-state navigation and conditional progress-update expectations for non-phase/read-only work. | Must preserve plans/progress as phase authorities. |
| F6 | `FINAL_INTEGRATION` | Set post-change evaluation/telemetry acceptance using comparable evidence and honest limits. | Depends on stable skill routes (F3) and context (F2). |
| F7 | `FINAL_INTEGRATION` | Validate evidence wording against actual verifier capabilities and distinguish pass/fail/blocked/partial. | Depends on preserving verifier semantics and service requirements. |

Ownership means the listed pass is accountable for closing or explicitly carrying the finding. Other passes may provide evidence but should not independently broaden into the finding owner's surface.

## 5. Dependencies between findings

```text
F2 Context scope and persistent policy ──► F3 skill boundaries and handoffs
F3 + F2 final surfaces ─────────────────► F1 effective runtime/routing probes
F2 state guidance ──────────────────────► F5 durable-state recovery clarity
F3 + F2 ────────────────────────────────► F4 invariant/check map and F7 evidence semantics
F1–F5 ─────────────────────────────────► F6 evaluation design for the final harness
```

The sequence should remain: `harness-agents-engineer` → `harness-skill-engineer` → `harness-context-engineer` → `harness-engineer` final integration. The first three own only their assigned surfaces unless an interface dependency is documented and handed to the relevant owner. The final pass checks cross-surface coherence and observed runtime outcomes.

## 6. Things subsequent passes must preserve

- This is an opening T0 inventory, not authorization to revise the harness in this pass.
- Preserve the user's pre-existing dirty files and diffs listed above; do not attribute their additions or reported validation to T0 work.
- Preserve `docs/asymmetric-committee-blueprint.md` as product authority and `docs/PROGRESS.md` as phase-status/history evidence unless a later, approved plan explicitly changes authority.
- Preserve exact approval semantics, phase gate distinction, paper-trading-only boundary, no paid live calls in tests, secret handling, point-in-time/anonymity/risk invariants, and any deterministic contract/import constraints when editing adjacent guidance.
- Keep agent definitions distinct from skill UI metadata. Do not infer that a file is runtime-loaded solely from its name or directory.
- Preserve existing skill-specific evaluators/telemetry and their cautious evidence model. Do not use static counts as quality measures.
- Retain explicit PASS/FAIL/BLOCKED/unknown distinctions, service prerequisites and skips; interrupted or unrun validation is not a pass.
- Keep one primary owner per finding; record any ownership transfer explicitly instead of letting adjacent specialist passes silently duplicate work.

## 7. Existing failures / unresolved runtime assumptions

### Existing or pre-existing state

- Worktree was dirty before T0; details in §2. No audit validation failure is established because product/harness tests were not run.
- Repository history/progress says P6 gate is blocked/unimplemented and P7/P8 gates are stubs. This is documented current status, not independently rerun.
- Existing entries report incomplete or service-limited historical validation (Timescale image/service, CI pending, manual gate recipes). These should remain qualified.
- No progress update was made because this user-defined audit requires read-only behavior. The ordinary instruction to update progress at session end cannot be followed without violating the explicit scope.

### Unverified assumptions

- Whether Codex 0.157.1 discovers repository `.agents/skills` and `agents/*.yaml` as expected, how it loads metadata, and whether `allow_implicit_invocation` has the assumed effect.
- The effective user-level Codex config, hooks, MCP servers, plugins, skills, profile, sandbox and tool exposure; configuration files were not inspected because this baseline is about repository harness authority and user-level files may contain sensitive configuration.
- Whether current runtime injects root AGENTS automatically, and exact precedence relative to user/developer instructions and skills.
- Whether the sole custom agent definition is discoverable and can be invoked with its stated contract.
- Whether existing verifier, skill preflights, scripts, schemas and CI pass now; inventory output does not validate them.
- Actual always-loaded context size, skill-selection accuracy, task recovery rate, and whether harness changes improve outcomes.

## 8. Final-integration acceptance criteria

The last campaign pass is acceptable when it can show, with paths and command/runtime evidence:

1. The effective post-change instruction, skill, agent, configuration, and tool discovery paths are documented as observed; unsupported behavior remains labeled unverified.
2. F1–F7 are each closed with evidence or explicitly carried as unresolved, with the single-owner labels retained and any scope transfer recorded.
3. Root/parallel instructions, specialist skills, plans/progress and deterministic validators have coherent authority and no material contradiction or broken reference.
4. Every mandatory repository invariant claimed in persistent guidance maps to a named falsifiable test/check or is labeled as a policy requirement without proven automated coverage.
5. Verification claims distinguish current runs from historical evidence, and PASS from BLOCKED, FAIL, skipped, interrupted, and unavailable states; no phase is represented complete contrary to its authoritative gate.
6. Existing skill-specific evaluation/telemetry is preserved; any broader evaluation claims use an explicit baseline and reproducible evidence. No empirical improvement metric is inferred from artifact changes alone.
7. No pre-existing user changes are overwritten, absorbed, or misattributed; final diff clearly identifies only authorized campaign edits.
8. Any behavior dependent on Codex version, profile, remote service, MCP, or undocumented runtime convention has a recorded observation or remains a stated limitation.
