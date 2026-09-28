# Skills Pass

**Date:** 2026-09-27
**Baseline:** `docs/harness-improvement/T0-baseline.md`
**Preceding pass:** `docs/harness-improvement/02-agents-pass.md`
**Scope:** all repository-level packages under `.agents/skills/`, their routing metadata, local references/helpers/evaluations, and skill-to-policy boundaries. No runtime route was exercised.

## Outcome

The repository has 12 project skill packages. Their workflows, sidecars, references, and available checks form a coherent set with useful decision-changing content. Static review confirmed the T0 overlap concern (F3) and found its main boundary: phase delivery is often confused with verification design and Git/PR management; decision persistence overlaps fact-store and model-dispatch work at database/task boundaries. Six skill descriptions now state ownership and cross-skill handoffs. Package bodies and existing agent contracts were left intact.

This is a static design result. No repeated Codex routing/task trials were run, so **G5 is UNVALIDATED** and no empirical improvement is claimed. Ten packages have `agents/openai.yaml` sidecars: nine opt into implicit invocation; predictive-validity has no `policy.allow_implicit_invocation` field. `code-auditor` and `repository-agents-md` have no skill metadata sidecar in the current tree. Runtime defaults and discovery are unverified, so neither absence is interpreted as enabled or disabled.

At pass start the branch was `phase/P6`, HEAD `7fed40e947b2e7a307994d8c5380d00d13cd094a`, and the tree was already dirty. Pre-existing modified paths were `docs/PROGRESS.md`, `docs/claude-session-friction-report.md`, `docs/plans/README.md`, and `tests/store/test_import_rules.py`; pre-existing untracked paths were `docs/harness-improvement/`, `docs/plans/HARNESS-001.md`, `scripts/check_python_edits.py`, `scripts/preflight_goal.py`, `tests/scripts/`, and `tests/store/import_linter_fixtures.py`. No skill package was dirty at start. Those changes were preserved; this pass added the six listed skill edits, this report, and one progress decision line.

## 1. Complete skill inventory and routing ownership map

| Skill | Exists to … when … | Positive trigger / nearest neighbors | Negative trigger and routing decision | Evidence, stop, failure, autonomy | Implicit invocation |
|---|---|---|---|---|---|
| `asymmetric-contract-schema` | change or review a shared inter-stage or serialized contract | models, enums, LLM output schemas, consumers, generated JSON schemas; neighbors: decision persistence and store when persistence/schema is implicated | exclude ordinary consumer logic that does not change a contract or serialized shape; contract source owns shape, schema is generated | Canonical model + generated-schema freshness + boundary tests; stop after exact checks/gate or report blocker; spec conflict stops a silent choice; no publication authority | Suitable: narrow, clear invariant boundary; metadata opts in |
| `asymmetric-decision-persistence` | build or review durable committee/risk decision units and replay/resume | atomic writes, identities, idempotency, decision/run records, transaction/replay behavior; neighbors: point-in-time store and model dispatch | exclude fact-table/as-of storage mechanics and provider call/retry/cache internals unless the change crosses those boundaries; new description names the handoffs | Atomicity/idempotency/replay/state tests and exact gate; stop on demonstrated contract or explicit blocker; unavailable DB/service means blocked evidence; no production writes | Suitable for consequential persistence; metadata opts in |
| `asymmetric-fixture-ingestion` | build/review external-source ingestion with deterministic offline evidence | SEC/Alpaca/news clients, HTTP, throttling, parsers, timestamps, fixtures, backfill/replay; neighbors: point-in-time store and model dispatch for their respective providers | exclude downstream feature/model logic that neither contacts nor parses an external source | Recorded fixtures, parser/timestamp assertions, offline replay/feed-health evidence; stop after fixture smoke and applicable gate; missing recording/service is reported; no live paid API tests or unrequested provider access | Suitable: provider safety and fixture boundary are consequential; metadata opts in |
| `asymmetric-layered-verification` | design/change verification gates or assess evidence strength | tests, CI, service gates, smoke/import/schema checks, acceptance semantics; neighbor: phase delivery; new description says routine phase implementation remains with phase delivery | exclude routine implementation that only runs an already-defined check; pair with phase delivery when changing its acceptance contract or interpreting whether evidence satisfies it | Exact command/result and what it proves; stop at acceptance decision or BLOCKED/NOT RUN; missing required service is not a pass; no CI mutation/production effect | Suitable when verification is the task; metadata opts in |
| `asymmetric-model-anonymization` | change identity/numeric masking or model-input partition/rendering | aliases, entity tokens, prompt rendering, partitions, leak tests; neighbors: model dispatch and point-in-time store for alias data | exclude provider retries/cost and committee logic that does not construct model input | Rendered-input negative leak tests and alias-coverage evidence; stop when forbidden identity/numeric leakage is ruled out or blocker recorded; missing aliases fail closed; privacy boundary preserved | Suitable: privacy invariant is material; metadata opts in |
| `asymmetric-model-dispatch` | change LLM provider call behavior and per-task dispatch outcomes | OpenRouter, model metadata, budgets, limiter, retries, cache, validation/repair/DLQ, task status; neighbor: decision persistence; new description separates per-task outcomes from atomic decision records/replay | exclude decision-unit transaction/replay work unless crossing into persisted decisions; no live paid-call testing | Mocked provider outcomes, actual served-model/cost/task evidence and resumable status; stop at terminal outcomes and required gate; missing cost/config fails closed; no paid calls | Suitable: cost/privacy and resumability are consequential; metadata opts in |
| `asymmetric-phase-delivery` | plan, implement, verify, and close a numbered project phase | starting/resuming `P<n>`, plan/spec reconciliation, implementation slices and phase closure; neighbors: layered verification and source control; new description names their conditional handoffs | exclude isolated bugfixes, PR-only review, and general exploration; phase delivery owns the end-to-end phase workflow | Ratified plan, invariant tests, exact phase gate, and progress evidence; stop on PASS or explicit blocker; spec ambiguity is escalated; no unapproved Git/PR action | Suitable for an explicitly numbered phase; metadata opts in |
| `asymmetric-phase-source-control` | manage Git/PR change units for a project phase | branch/commit slicing, staging, PR preparation, integration and recovery; neighbor: phase delivery; new description assigns phase planning/product implementation to phase delivery | exclude phase planning/implementation when no source-control action is in scope; user authorization remains the boundary for Git/external actions | Baseline, scoped diff, authorized change evidence, and exact gate/remote status; stop before each unauthorized authority boundary; preserve unrelated dirty work and report unavailable evidence | Suitable when Git/PR management is requested; metadata opts in |
| `asymmetric-point-in-time-store` | change persistence/fact-store schemas and historical as-of reads | `store/as_of.py`, writes/migrations, `available_at`, `source_version`, historical data; neighbor: decision persistence; new description separates fact-store boundary from decision-unit durability | exclude storage-independent application changes; combine with decision persistence only where both boundaries change | As-of/no-look-ahead tests, import boundary, migration/schema and service evidence; stop after exact checks or mark blocked; unavailable DB does not imply temporal correctness; no production writes | Suitable: look-ahead and storage boundaries are consequential; metadata opts in |
| `asymmetric-predictive-validity-audit` | audit identifiability and leakage resistance of incremental committee predictive/trading claims | preregistration, benchmark/control design, multiplicity, calibration, contamination, prospective tests; neighbor: code auditor for implementation correctness | exclude generic code review, implementation debugging, strategy optimization, post-hoc explanation when experimental validity is not the question | Falsifiable, evidence-backed verdict/report; stop at PASS/RED/INCONCLUSIVE for the bounded claim; missing protocol/evidence yields inconclusive; no claim of certification or causality without design | Implicit suitability is high for an explicit validity question; sidecar omits the setting and effective default is UNVERIFIED |
| `code-auditor` | produce a risk-ranked, evidence-backed code/release/security/compliance audit | diff/repository/claim audits; optional narrow independent reviewer contract; neighbors: predictive-validity audit and repository-agents-md | exclude implementation-only work, routine linting, formal certification; domain skills own their specific correctness invariants | Findings/verdict tied to source/tool evidence and schema validation; stop at adequate scope or INCONCLUSIVE; missing/contradictory proof lowers confidence; read-only unless remediation is requested | Suitable for explicit audit requests; no `agents/openai.yaml` sidecar is present |
| `repository-agents-md` | create/update root persistent agent instructions from repository evidence | root `AGENTS.md` scope, durability, policy-vs-skill classification; neighbors: harness/context engineers | exclude one-off instructions, implementation plans, and conditional workflows; this pass preserves its audited agent-layer contract | Final root file, factual/command/path review and validator; stop after in-scope root guidance or report authority conflict; preserve unrelated work and do not invent rules | Suitable for explicit root-instruction requests; no `agents/openai.yaml` sidecar is present |

### Ownership and composition rules

- **Contract shape vs. persistence:** contract-schema owns serialized model shape and generated schema freshness. Decision-persistence owns atomic decision records/replay; point-in-time-store owns fact-table schemas, migrations, and as-of reads. Compose only when the proposed change actually crosses those sources of truth.
- **Model call vs. decision state:** model-dispatch owns provider request/cost/retry/cache and task outcome; decision-persistence owns durable committee/risk units, their transaction boundary, and run-level replay. A cache/task change alone does not activate decision-persistence.
- **Phase lifecycle vs. supporting work:** phase-delivery owns plan through phase closure. Layered-verification owns verification design and evidence interpretation. Phase-source-control owns Git and PR change management. One phase request may select multiple skills, but the task must cross the named supporting boundary.
- **Audit intent vs. subject matter:** code-auditor owns general risk-ranked code/release/security/compliance review; predictive-validity-audit owns whether an experiment supports a predictive/trading claim. An implementation review with no such claim stays with code-auditor; an experiment-validity question routes to the specialist.
- **Persistent policy vs. workflow:** root `AGENTS.md` remains authoritative for durable repository policy, including phase approval and project invariants. Skills should keep conditional procedures, references, and preflights. The six skills repeat only their own effect boundary and domain constraints; no policy was moved into always-on instructions here.

## 2. Routing, loading, and package assessment

### Positive/negative/neighbor coverage

The 12 descriptions consistently state workflow plus activation condition and meaningful exclusions. The clearest overlap was not an absent domain route but missing explicit composition rules. The six edited descriptions cover the two highest-impact cross-cutting clusters: phase delivery/verification/source control and decision persistence/store/model dispatch. Existing distinctions for ingestion vs. downstream features, anonymization vs. dispatch, predictive audit vs. code audit, contract changes vs. storage-independent consumers, and persistent `AGENTS.md` vs. one-off procedure were retained.

Implicit invocation is appropriate for narrow recurring workflows whose triggering terms are concrete and whose bodies contain guardrails. The predictive-validity audit is also appropriate for implicit selection when a predictive-validity claim is being evaluated, but its sidecar setting is absent. The two packages without sidecars still have positive frontmatter descriptions; actual discovery and implicit behavior are runtime questions. Do not infer effective behavior until runtime discovery confirms defaults and selection behavior. This is a static suitability judgment, not observed routing.

### Core/reference/helper placement

- Core `SKILL.md` files carry the activation boundary, source-of-truth decisions, consequential workflow steps, verification, failure paths, and completion contract. These are needed in most invocations of each skill.
- `references/` hold branch-specific contracts, detailed checklists, evidence/statistical guides, and reusable examples. All checked local Markdown references exist; the mechanical inspector found no missing local references.
- `scripts/` hold repeated structural preflights, package/report validators, and telemetry/evaluation mechanics. No new helper was justified: the six routing clarifications are semantic and too small to warrant automation.
- Repository-wide phase approval, product invariants, no-live-paid-API policy, and phase definition-of-done remain in repository guidance and product authority; the skills point to those authorities instead of becoming a second policy source.
- No stale runtime version claim or unsupported Codex metadata was found in the available frontmatters/sidecars. Ten packages have `agents/openai.yaml` skill display/default-prompt metadata; `code-auditor/agents/audit-reviewer.yaml` remains the separate, explicit reviewer contract. Two packages have no skill metadata sidecar, a current-state discrepancy from T0’s claim of 12/12; discovery impact is UNVERIFIED.

### Decision and evidence coverage

Static review found the substantive choices classified in the skills as mandatory (source-of-truth/invariant/test constraints), prohibited (forbidden trust-boundary or unauthorized effects), or judgment (conditional escalation, applicable tests, and whether a boundary is crossed). Observable completion is distinct from attempt: skills require inspected diffs, exact checks/results, generated-source freshness, evidence artifacts or gate outcomes, and blocker/inconclusive reporting. Failure branches commonly distinguish missing tools/services/references, pre-existing failures, spec conflicts, incomplete evidence, and external authority boundaries. Stopping criteria are the exact gate/workflow completion or an explicit blocked/inconclusive outcome. No attempted-action-equals-success defect was found in the sampled and mechanically indexed completion sections.

The static rubric’s numerical semantic measures (Decision-Bearing Density, Decision Coverage, Mechanical Coverage, Failure Branch Coverage, Core Relevance) were not annotated or calculated, so **Design Readiness was not scored**. No `/50` value is inferred from package size or validator success.

## 3. T0 findings and prior-pass discoveries

| Finding | Disposition | Evidence |
|---|---|---|
| T0 F3 — overlapping skill boundaries | Addressed at the narrow metadata layer; runtime falsifier remains open | Six descriptions now state neighboring ownership and when to combine workflows. Full ownership map above. No routing trials were run. |
| T0 F1 — runtime discovery/precedence | Carried to FINAL_INTEGRATION | Static package presence and metadata do not show what Codex actually injects or selects. |
| T0 F2 — AGENTS/CLAUDE duplication | Carried to CONTEXT_PASS | Skills sometimes point at project policy; this pass did not rewrite always-on topology. |
| T0 F4 — invariant-to-check map | Carried to FINAL_INTEGRATION | No product invariants or product gates were modified; skill preflight inventory is not a substitute for that trace. |
| T0 F5 — generic task state/progress scope | Carried to CONTEXT_PASS | No generic checkpoint or progress-history redesign was attempted. |
| T0 F6 — no whole-harness evaluation | Carried to FINAL_INTEGRATION | Existing predictive-validity evals target that skill only; code-auditor and repository-agents-md have package-local validation/eval surfaces. No cross-skill routing eval exists. |
| T0 F7 — phase evidence caveats | Carried to FINAL_INTEGRATION | This pass did not run a product phase gate. The P6 phase preflight reports the plan exists and its five standard headings are not recognized; this is a structural preflight observation, not a phase-gate result. |
| Agents-pass reviewer contract | Preserved; no incompatibility found | `code-auditor/agents/audit-reviewer.yaml` remains read-only and bounded. Its skill workflow already defines optional independent review; no agent YAML or agent layer was changed. Runtime invocation remains unverified. |
| Agents-pass parent instruction collision | Carried, not changed | The inherited parent-directory `AGENTS.md` has a different repository purpose according to the prior pass. It is outside this skill pass; final integration owns effective-scope resolution. |

No prior finding was rejected as false. Two T0 inventory details are stale against the current tree: only 10/12 packages have `agents/openai.yaml`, and only 9/12 opt into implicit invocation there. The predictive-validity sidecar has display name, summary, and default prompt but no implicit-invocation setting; `code-auditor` and `repository-agents-md` have no sidecar. Runtime effect remains unverified.

## 4. Changed skill packages and revision hypotheses

| Package | Change | Revision hypothesis |
|---|---|---|
| `asymmetric-decision-persistence` | Names fact-store and dispatch neighbors and its atomic decision/replay ownership in the description. | A task changing only provider task state or fact-table/as-of mechanics is less likely to load this skill as the sole route. |
| `asymmetric-layered-verification` | Separates verification design/evidence interpretation from routine phase implementation. | Routine phase tasks are less likely to be mistaken for verification-system changes. |
| `asymmetric-model-dispatch` | Names dispatch/task-outcome ownership vs. decision-unit persistence/replay. | Dispatch-only changes are less likely to appear to require decision transaction guidance. |
| `asymmetric-phase-delivery` | Names conditional handoffs to verification and source-control workflows. | Composite phase work is more likely to load complementary guidance at the relevant boundary. |
| `asymmetric-phase-source-control` | Names Git/PR ownership and points phase planning/implementation to phase delivery. | Git-only work is less likely to load a phase implementation workflow as its owner. |
| `asymmetric-point-in-time-store` | Names fact-store/as-of ownership vs. atomic decision persistence. | Fact storage changes are less likely to conflate point-in-time semantics with decision transaction semantics. |

The hypothesis is routing precision/recall at these six edges, with no loss of activation on clear positives. It remains untested behaviorally.

## 5. G1–G5 status

- **G1 Routing boundary — PASS (static, after revision):** all 12 frontmatter workflows can be stated with activation and neighbor boundaries; six previously implicit edges now appear in descriptions. Metadata-sidecar coverage/implicit settings remain incomplete for three packages; runtime discovery is unresolved.
- **G2 Material decision value — PASS (static):** package instructions constrain domain-specific architecture, testing, authority, evidence, and failure decisions. No generic-only skill was identified.
- **G3 Observable definition of done — PASS (static):** completion is tied to reviewable artifacts/results or an explicit blocked/inconclusive state, not an attempted command/edit.
- **G4 Failure/autonomy controls — PASS (static):** domain and external-effect boundaries, failure branches, and evidence-strength caveats are present; generic reviewer agent contract remains read-only.
- **G5 Empirical value — UNVALIDATED:** no repeated same-start-state Codex skill/no-skill trials or native routing telemetry were captured.
- **Design Readiness — NOT MEASURED:** semantic rubric dimensions were not counted. Do not treat this as `/50` or an empirical score.

## 6. Validation and runtime evidence

- `inspect_skill.py` run against all 12 packages — **12/12 exit 0**; all local references resolve and no suspicious cache/temp artifacts were reported. Its simple parser reports `description_chars: 1` for folded YAML descriptions; independent PyYAML parsing confirmed every name matches its directory and every description is below 1,024 characters. Treat the inspector’s length field as a parser limitation, not a package defect.
- All 9 repository-specific `*_preflight.py` helpers exited 0 when invoked with their required arguments (`phase_preflight.py P6`). The initial no-argument phase-helper call returned its expected usage error and was rerun correctly. These are structural observations, not semantic or phase-gate passes. Its P6 result notes that the plan file exists but the standard headings are unrecognized.
- Predictive-validity package sanity check — **PASS**; its eval telemetry regression script — **PASS**. The bundled eval corpus/runner covers that skill’s positive, negative, and neighbor routes, but was not run because runtime trials were outside this static pass and repeated paired trials are required for G5.
- Code-auditor validator tests — `python -m pytest .agents/skills/code-auditor/tests -q`: **9 passed**. Validator `--help` is not implemented; no sample audit artifact was available to validate in this pass.
- Repository AGENTS validator against current `AGENTS.md` — **PASS**, no heuristic findings. This validates the input to the skill, not a new skill output.
- PyYAML parse of all 12 descriptions — **PASS**, 12 names match directory names and all descriptions are under the 1,024-character limit.
- `git diff --check` — **PASS** (Git emitted existing LF/CRLF conversion notices for dirty files).
- No live Codex skill-selection/injection, reviewer subagent invocation, full predictive-validity eval run, or phase gate was performed. Runtime route activation and G5 are **UNVERIFIED**.

Every package’s full file inventory was examined. Each has a core skill file; optional display metadata is present for ten. Nine asymmetric domain skills have focused references and read-only preflights; predictive-validity and code-auditor add local evaluation/validation assets; repository-agents-md adds routing/task scenarios and a validator. Scan of the six changed descriptions found no unresolved placeholders, temporary paths, secrets, or machine-local paths. Validation-generated bytecode caches were removed. No package sidecar, helper, reference, evaluation corpus, or agent definition was edited.

## 7. Remaining failure mode and handoffs

**Largest remaining failure mode:** the active runtime may fail to discover a skill or may select only one member of a complementary pair. Static wording cannot prove injection, priority, co-selection, or false activation. A further likely blind spot is lack of one shared cross-skill routing corpus; current eval assets validate only selected individual packages.

**Next revision hypothesis:** after runtime telemetry is available, build a small fixed cross-skill corpus around phase delivery/verification/source control, dispatch/persistence/store, contract/store, code audit/predictive validity, and negative adjacent prompts. Compare observed injection with expected owners and explicit complements before changing descriptions further.

**CONTEXT_PASS discoveries:**

1. Skill bodies repeatedly restate read-status, preserve-unrelated-work, no-implicit Git/external effects, and phase/gate procedures. The context pass should decide which are durable `AGENTS.md` policy and which are workflow-local; this pass did not prune the topology.
2. `asymmetric-phase-source-control` explicitly reads `CLAUDE.md` as part of its baseline despite Codex as the active agent. Decide whether that is intentional cross-runtime authority or a context-loading cost; no edit was made here.
3. Conditional workflows and exact approval rules remain appropriately distinct in principle, but the root policy’s phase scope and read-only task exception require the planned context reconciliation.

**FINAL_INTEGRATION discoveries:**

1. Probe actual skill discovery/injection and implicit-invocation default, especially predictive-validity metadata and complementary phase/storage routes.
2. If a common route evaluation is built, preserve existing skill-specific corpora and distinguish static eligibility from observed injection.
3. Preserve the code-auditor read-only reviewer contract and report runtime use as unverified until exercised.
4. Reconcile the inherited parent `AGENTS.md` scope and current P6 plan/gate evidence against the agent-pass and verification-preflight observations.

No commit, push, PR, merge, publication, production action, or live paid API call was performed.
