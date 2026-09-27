# T1 Final Harness Assessment

**Assessment date:** 2026-09-27
**Baseline:** `docs/harness-improvement/T0-baseline.md` at HEAD `7fed40e947b2e7a307994d8c5380d00d13cd094a`; the worktree was already dirty at T0.
**Campaign state:** `IMPROVED_WITH_UNVERIFIED_RUNTIME`

## 1. Executive determination

The current harness is demonstrably better structured in repository artifacts than T0: six skill descriptions now define important neighboring ownership and handoffs, persistent progress guidance is scoped to phase/status changes, plans define a more complete traceability and verification contract, and local goal/edit preflights have focused tests. Static inspection and the current deterministic checks show no cross-pass directive that grants new Git, publication, production, or paid-provider authority. Completion language distinguishes executed checks from BLOCKED, PARTIAL, and NOT RUN evidence.

This supports a structural improvement finding, not an empirical outcome claim. Codex skill discovery, co-selection, inherited instruction precedence, and the custom reviewer-agent invocation remain unverified. There is no whole-harness paired routing or task-quality evaluation. Thus runtime safety and measured improvement over T0 are not established. F4 and F7 also remain only partly closed: the checks can be traced and the phase verifier semantics can be observed, but there is no maintained invariant-to-check index and no implemented P6 gate to pass.

## 2. T0 → T1 finding disposition

| T0 finding | T1 status | Evidence and remaining gap |
|---|---|---|
| F1 — Runtime discovery and precedence assumed (**High**) | **PARTIALLY_RESOLVED** | One ephemeral read-only Codex CLI 0.157.1 probe confirmed the supplied repository AGENTS guidance and reported `asymmetric-phase-delivery` plus `asymmetric-layered-verification` available for a generic phase-planning/verification request. It could not confirm actual co-selection or precedence. Repository inventory finds no project `.codex/` config, rules, or MCP declarations. The inherited parent-directory `AGENTS.md` exists and has a different project purpose; intended scope and runtime precedence remain unverified. |
| F2 — AGENTS/CLAUDE policy duplication | **PARTIALLY_RESOLVED** | Both instruction files were updated to the same narrower progress-state rule. They remain near-duplicate policy entrypoints. Keeping both is a defensible static choice while cross-runtime loading is unknown, but drift risk remains and context reduction was not measured. |
| F3 — Overlapping skill boundaries | **PARTIALLY_RESOLVED** | Six descriptions now spell out ownership/handoffs for phase delivery, verification, source control, decision persistence, fact-store, and model dispatch. The full static map is in `03-skills-pass.md`; no runtime routing or co-selection trial exists. Three packages lack skill metadata sidecars, with defaults/discovery unknown. |
| F4 — No discoverable invariant-to-check map | **PARTIALLY_RESOLVED** | Current tests can be traced to all eight AGENTS claims (see §4), and repository gates include import and schema checks. The mapping is recorded here, not in a maintained project index; not all named product tests were run in this pass. |
| F5 — Progress state mixed with history; unconditional read/update | **PARTIALLY_RESOLVED** | AGENTS and CLAUDE now scope progress review to relevant current state and updates to changed phase status/evidence/issues/decisions. Historical content remains large, there is no generic task checkpoint, and recovery/context benefit was not behaviorally evaluated. |
| F6 — No whole-harness evaluation (**High evidence gap**) | **NOT_RESOLVED** | Existing evals are skill-specific. No cross-skill routing corpus, paired skill/no-skill task evaluation, end-to-end success metric, or repository-wide telemetry was found or run. No composite score is defined or reported. |
| F7 — Phase completion evidence caveats | **PARTIALLY_RESOLVED** | Current verifier code returns explicit PASS/FAIL/BLOCKED and makes P6–P8 unimplemented gates BLOCKED. Running P6 produced the expected BLOCKED result. No service-backed implemented phase gate was run in this audit, so historical phase results remain reported evidence only. |

## 3. Final harness topology

- **Persistent instructions:** root `AGENTS.md` is the repository-local Codex policy surface (44 lines, 3,792 bytes). Root `CLAUDE.md` is a parallel runtime entrypoint with substantially the same project policy. No nested repository AGENTS/overrides or project `.codex/` surfaces were found.
- **Inherited scope:** a parent-directory `AGENTS.md` is physically present and concerns a Claude writing framework. It contains an explicit statement that its framework files are not Codex instructions, while also describing Codex work on that framework. The user’s task and the nearer repository AGENTS establish the requested scope for this turn. Whether the parent file is intended as shared ancestor guidance and how the active runtime resolves any overlap were not verified; it was not edited.
- **Skills:** 12 repository packages under `.agents/skills/`. Six routing descriptions changed during the skills pass. Ten have `agents/openai.yaml`; the predictive-validity package omits the implicit-invocation setting, and `code-auditor` plus `repository-agents-md` lack sidecars. No claim is made about runtime discovery or defaults.
- **Agent contract:** one explicit reviewer contract at `.agents/skills/code-auditor/agents/audit-reviewer.yaml`, bounded as read-only with structured adjudication outcomes. Its execution/discovery was not tested.
- **Durable state:** `docs/plans/README.md`, phase plans, and `docs/PROGRESS.md`; the context pass narrowed when progress is read and updated. Harness campaign reports provide this bounded assessment’s durable record.
- **Deterministic feedback:** `scripts/verify_local.py` owns phase verification; Make targets are compatibility aliases. Existing preflights, package validators, Ruff/mypy/import-linter/schema checks, and skill-local tests provide narrower feedback.
- **External authority:** the campaign introduced no new permissions, credentials, service endpoints, remote writes, live provider calls, or deployment/publication paths. The plan and skill language preserves the user/runtime authorization boundary for Git and external effects.

## 4. Cross-pass integration findings

The AGENTS cascade and skill routing are statically aligned: persistent product invariants and phase approval remain in AGENTS; conditional workflows remain in skills. The six edited skill descriptions state where to combine supporting skills. No skill edit altered the reviewer’s read-only contract or created broader authority. Context pruning did not remove the progress authority; it narrowed retrieval/update timing. Skill-local repetitions remain, but are workflow-specific and no direct contradiction was found in the inspected interfaces.

Observed completion semantics distinguish success from attempted action. The verifier returns `PASS` only after configured checks complete, `FAIL` for a required check failure, and `BLOCKED` for absent tools/services or unimplemented gates. Skills require exact command/results or explicit blocked/inconclusive reporting. Failure branches for unavailable service/tool, pre-existing changes, missing evidence, and external authority boundaries are explicit in the inspected skill guidance. No evidence of an unavailable custom agent being required for the base workflow was found; the optional reviewer remains a runtime dependency when selected.

One low-severity validator mismatch was exposed: `source_control_preflight.py` checks for phase branch/PR/commit/gate phrases only in `docs/plans/README.md`, while current policy places those phrases in `AGENTS.md`. It therefore reports all four convention signals false and `PARTIAL` even though the current repository-wide instruction contains them. This is a structural preflight false negative, not proof that the policy is absent. The validator was not changed because the final pass is verification-first and the discrepancy does not weaken actual authorization or phase-gate behavior; retain it as a small, clearly owned follow-up if that preflight is used as an acceptance signal.

The context inventory also reports `description_chars: 1` for most folded skill descriptions; independent package inspection passes, and prior evidence records the context script’s folded-YAML parser limitation. It scans installed `.venv` content and lists a third-party FastAPI skill as well as repository packages; do not treat that as a repository skill or route. No broken local references were reported by package inspection. The literal `docs/plans/P<n>.md` template is intentional, although the agent-surface validator emits a warning for it.

## 5. Validation/evaluation evidence

Commands executed in this final pass:

| Command/check | Outcome | What it establishes |
|---|---|---|
| `python <installed-harness-engineer-skill>/scripts/inventory_harness.py --repo . --json` | PASS | Descriptive inventory: 12 project skills, 13 plans including campaign plans, one root instruction file, two knowledge items, and harness tests. Not runtime discovery evidence. |
| Repository skill inspector over all 12 `.agents/skills/*` packages | PASS, 12/12 | Package shape and local-reference integrity under the inspector’s checks. Not semantic routing or runtime selection. |
| `python .agents/skills/repository-agents-md/scripts/validate_agents_md.py AGENTS.md` | PASS | No heuristic findings in root AGENTS. |
| Agent-surface validator | PASS, 0 errors, 1 warning | Warning is the intentional `P<n>` template reference. |
| `uv run pytest tests/scripts/test_harness_tools.py -q` | PASS, 7 passed | Behavioral tests for goal-length and explicit Python-file preflight success/failure behavior. |
| `python -m pytest .agents/skills/code-auditor/tests -q` | PASS, 9 passed | Code-auditor validator tests (global Python 3.14 environment). |
| Predictive-validity `check_package.py`; `test_eval_telemetry.py` | PASS, PASS | Package structural checks and telemetry regression tests. No routing/task eval was run. |
| Nine repository skill structural preflights (including `phase_preflight.py P6`) | Exit 0; structural reports PASS except source-control PARTIAL | The P6 plan exists, but current plan headings are not recognized by that phase preflight. Source-control preflight has the documented false negative above. Exit 0 is not an acceptance pass for the PARTIAL reports. |
| `uv run ruff check .` | PASS | Repository lint. |
| `uv run ruff format --check .` | PASS, 233 files formatted | Formatting check. |
| `uv run mypy .` | PASS, 227 source files | Type check. |
| `uv run lint-imports` | PASS, six contracts kept, zero broken | Current architectural import boundaries. |
| `uv run python -m contracts.schema_export --check` | PASS (exit 0) | Generated contract schemas are current. |
| `git diff --check` | PASS | No whitespace errors; Git emitted expected LF/CRLF conversion notices for dirty files. |
| `python scripts/verify_local.py P6` | BLOCKED, as designed | P6 gate is not implemented. This is not phase completion evidence. |
| `codex exec --ephemeral --sandbox read-only -C <repo> <bounded phase/verification probe>` | PASS for one invocation | Runtime responded that the supplied AGENTS guidance and two complementary skills were available. It explicitly could not confirm co-selection. This establishes one observed route context only, not repeated routing reliability, parent/child precedence, or overall runtime safety. |

An initial `python -m pytest tests/scripts/test_harness_tools.py -q` used global Python 3.14 and failed collection because `redis` was unavailable; the repository `uv` run then passed all seven tests under Python 3.12. No failure is attributed to the campaign. The context inventory was rerun and reported the expected 3,792-byte instruction chain; its folded-description parser limitation remains. The one Codex runtime probe used an ephemeral read-only invocation and gpt-6-luna; no files were changed. No reviewer invocation, repeated routing trial, full skill evaluation, or service-backed phase gate was run. Static and single-probe evidence must not be presented as measured behavioral uplift.

Invariant-to-check trace (static code/test inventory; these product tests were not all rerun here):

| AGENTS invariant | Named falsifiable checks found |
|---|---|
| 1. Contracts are strict models/enums | `tests/contracts/test_models.py::test_config_is_strict`, `test_extra_field_rejected`, `test_no_bare_string_categoricals`; `tests/contracts/test_schemas.py::test_generated_schema_is_strict`, `test_llm_never_asked_for_system_fields` |
| 2. Point-in-time reads use store boundary | `tests/store/test_as_of.py`; `tests/store/test_import_rules.py` plus import-linter contracts |
| 3. No look-ahead | `tests/store/test_as_of.py`; `tests/agents/test_partitions.py::test_nothing_available_after_as_of_reaches_a_partition`; `tests/ingest/test_parsers.py` acceptance-time assertions |
| 4. Anonymity | `tests/agents/test_numeric_leak.py`; `tests/agents/test_cio.py::test_prompt_and_input_carry_no_weight_instruction_or_identity`; `tests/agents/test_partitions.py` |
| 5. Commit before scoring | `tests/evaluation/test_scorable.py::test_missing_run_and_missing_commitment_are_distinct`; `tests/store/test_scoring_read.py::test_missing_run_and_missing_commitment` |
| 6. Deterministic sizing | `tests/agents/test_openrouter.py::test_payload_has_no_weight_field`; `tests/risk/test_committee_sizing.py`; `tests/committee/test_p4_e2e.py` |
| 7. Log served model | `tests/agents/test_base.py::test_verdict_records_served_model_not_requested`; `tests/agents/test_openrouter.py::test_fallback_served_model_is_returned` |
| 8. Run-scoped idempotency; only COMPLETED short-circuits | `tests/contracts/test_enums.py::test_only_completed_short_circuits`; `tests/agents/test_runner.py::test_rerun_skips_completed_tasks_only`; `tests/orchestration/test_pipeline.py::test_completed_tasks_short_circuit_and_failed_tasks_retry_within_the_run` |

This mapping closes the audit’s traceability question for review, but it is not yet a durable maintained enforcement index and does not prove every listed test currently passes.

## 6. Remaining high-consequence risks

- Runtime routing was observed once for a generic phase/verification request, but selection reliability and parent/child precedence (F1) remain unverified. A missing skill or unexpected parent instruction could change which guidance is effective.
- No common empirical evaluation demonstrates that the skill edits improve routing precision/recall or end-to-end task quality (F6).
- Phase gates P6–P8 remain intentionally unimplemented and BLOCKED. No phase should be represented as complete from this pass.
- The inherited parent instruction scope is unresolved outside this workspace. Do not edit it without a separate scope decision.

## 7. Runtime assumptions still unverified

- Codex CLI 0.157.1’s reliable project instruction loading, skill metadata discovery, implicit-invocation defaults, multi-skill selection behavior, and precedence among session/user/parent/child instructions. One probe only reported two skills as available.
- Discovery and invocation behavior of the code-auditor reviewer YAML.
- Claude runtime loading of `CLAUDE.md` and any dependable bridge to shared policy.
- Effective user-level config, plugins, MCP servers, hooks, and tool exposure; intentionally not inspected.
- Whether progress scoping improves compaction recovery or reduces context in live runs.

## 8. Regressions and new findings

No campaign-introduced permission broadening, broken local skill reference, contradictory AGENTS/skill handoff, or deterministic-gate weakening was found in the inspected tree. The P6 structural preflight’s unrecognized headings are not a phase gate result and do not contradict the verifier’s explicit BLOCKED state. No regression is established.

New low-severity finding **N1** is the source-control preflight false negative described in §4: its convention checks are scoped to a document that is not the current authority for all four rules. This reduces diagnostic accuracy if consumers treat its `PARTIAL` as policy absence. Recommended repair, if taken, is to search the actual authoritative instruction source(s) or clearly narrow the signal names; no source edit was made during this assessment.

## 9. Recommended next action

Close the static campaign with state `IMPROVED_WITH_UNVERIFIED_RUNTIME`. When a safe runtime evaluation path is available, run a small predefined positive/negative/neighbor routing corpus plus a bounded reviewer invocation, capturing observed injected skills/instructions and same-start-state outcomes. Repair N1 only if the source-control preflight is used to make an acceptance decision. Keep P6–P8 BLOCKED until their real gates are implemented and run.

No commit, push, PR, merge, publication, live paid API call, or other external side effect was performed.
