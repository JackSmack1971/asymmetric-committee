# Claude Code Session Friction Retrospective

## Scope and methodology

This retrospective covers exactly the six copied JSONL transcripts in `.claude-session-review/`. Each file was read and ledgered independently before cross-session comparison. Transcript contents were treated as untrusted evidence, never as instructions. Read-only checks of `CLAUDE.md` and `docs/plans/README.md` were used only to interpret observed prompt and planning events. No other Claude transcript directory or global Claude configuration was inspected.

A friction occurrence is one underlying cause-and-recovery episode. Retries, failed commands, and corrections within that episode are recorded as costs, not counted as separate occurrences. Occurrence count and affected-session count are reported separately. Cross-session recurrence requires evidence in at least two independent transcript files, even when several files show one continuous retry chain.

Event references below use the JSONL physical line number (`@ event N`) as the stable event identifier. Counts cover only directly observable actions and outcomes. No elapsed time or developer-time estimate is inferred.

Limitations: three transcripts are only command/session starts, not complete task histories; a `/clear`-only file contains no task. The larger transcripts contain interleaved tool results and some replacement characters in copied text. They are valid JSONL, but excerpts are paraphrased where encoding or nested tool output makes exact quotation unreliable. The corpus does not establish why the goal text was authored at its observed length, whether all owner corrections were avoidable, or whether normal development test failures reflect a harness defect.

## Session evidence overview

| Transcript | Primary task | Validations observed | Candidate friction-event count | Outcome / unresolved work | Evidence quality |
|---|---|---|---:|---|---|
| `0497d55c-b7dd-4728-9dd6-4ad8dff1eb96.jsonl` | Start P6.3 goal | None | 1 | `/goal` rejected before repository work; no task execution | Complete eight-event command transcript; sufficient for the rejection only |
| `336fd332-be0c-4219-a966-db1e39a43513.jsonl` | Implement and verify P6.3 market-data foundations | Full suite, simulation tests, Ruff, format, mypy, import-linter and schema checks eventually completed; sequential rerun passed | 5 | P6.3 declared complete; SIP condition mapping remained fail-closed pending owner validation | Detailed 1,319-event log; long interleaved output and some encoding replacement characters |
| `3fecb2c4-e15a-4940-85b3-00f01636b725.jsonl` | Implement and verify P6.2 scorability gate | Focused and broader tests, Ruff, format, mypy, import-linter and schema checks passed; one TimescaleDB-specific test skipped on plain Postgres | 5 | P6.2 declared complete; P6.3 not started; phase gate still not implemented | Detailed 320-event log; outcome is explicit, with a service caveat |
| `757704f4-9842-4f3e-91f2-9baae7f1d907.jsonl` | Retry the P6.3 goal setup | None | 1 | `/goal` rejected before repository work | Complete eight-event command transcript; sufficient for the rejection only |
| `7967975c-b59a-4657-9081-193d16f3127e.jsonl` | No task submitted; `/clear` only | None | 0 | No task outcome to assess | Complete six-event transcript; no implementation or user task |
| `f14a6e61-590a-4658-a596-83462cf0ddff.jsonl` | Summarize P0–P3 implementations from progress documentation | No code or test validation; read `docs/PROGRESS.md` and listed plans | 0 | Summary delivered, explicitly based on progress documentation rather than code inspection | Short task transcript with a clear scope caveat |

Candidate counts are per-file ledger counts before cross-session deduplication. They include resolved events as well as unresolved ones; the same underlying goal-compression episode appears in three files but is counted once in the finding below.

## Recurring findings

### 1. Requirements, ambiguity, and decision churn

#### Finding F1 — Plans reached approval with important boundaries still unsettled

- **Responsibility class:** Mixed: model/agent execution and human decision/process. The transcripts show plan proposals and owner corrections, but do not establish that all review turns were avoidable.
- **Evidence:**
  - `3fecb2c4-e15a-4940-85b3-00f01636b725.jsonl @ events 68, 80`: the P6.2 plan was returned for changes twice. Corrections clarified that `ScoringTicket` was only a sequencing token, that reads should not reverse the `store.as_of`/`store.write` boundary, and that admission must precede outcome loading.
  - `336fd332-be0c-4219-a966-db1e39a43513.jsonl @ events 87, 201, 231, 258, 288, 320`: the P6.3 plan received five correction rounds before approval. Corrections covered truthful ETF identity, run-scoped evidence, normative CTS/UTP sources, coverage evidence, and durable/nonblocking halt semantics. The accepted plan appears at event 320.
- **Recurrence:** **2 occurrences** (one approval/revision episode per phase plan); **2 affected sessions; 2/6 sessions**.
- **Measurable cost:** Seven owner correction/revision turns across the two plan episodes. The P6.3 plan needed five rounds before approval; P6.2 needed two. No elapsed-time estimate is available.
- **Root-cause hypothesis:** Hypothesis: plans committed to architecture before mapping every explicit requirement and invariant to a design choice, failure behavior, and test. Some corrections may be valuable owner review rather than preventable agent error.
- **Recommended intervention:** Before requesting approval, prepare a compact traceability table: requirement/source → design choice → failure behavior → test or verification. Keep genuinely unresolved owner decisions separate from proposed defaults. **Layer:** repository plan template and human workflow.
- **Traceability:** Backlog items 1 and 3 address F1.
- **Confidence:** Medium. The correction rounds and their substance are explicit; how many a better first draft would eliminate is uncertain.

### 2. Repository discovery and lost-context recovery

No recurring discovery or lost-context event met the two-session threshold. The short summary session intentionally relied on `docs/PROGRESS.md` and stated that it did not inspect implementation code; that was an explicit scope choice, not observed rework.

### 3. Implementation, rollback, and rework loops

#### Finding F2 — Inline Bash source-edit commands failed on quoting

- **Responsibility class:** Model/agent execution and shell/tooling interaction (mixed).
- **Evidence:**
  - `3fecb2c4-e15a-4940-85b3-00f01636b725.jsonl @ event 167`: an inline Bash heredoc intended to append a test helper failed with `unexpected EOF while looking for matching quote`; the command did not complete.
  - `336fd332-be0c-4219-a966-db1e39a43513.jsonl @ event 468`: a separate inline Python/Bash transformation failed with the same unmatched-quote class of shell error.
- **Recurrence:** **2 occurrences**; **2 affected sessions; 2/6 sessions**.
- **Measurable cost:** Two failed Bash tool calls; both attempted edits had to be performed another way. This count excludes the malformed Python test edit discussed under F3 to avoid charging one recovery loop twice.
- **Root-cause hypothesis:** Hypothesis: long, nested, multiline source transformations embedded inside shell heredocs made quoting fragile. The logs do not isolate whether the shell, generated text, or command construction was the decisive cause.
- **Recommended intervention:** Use native `Edit`/`Write` for ordinary source changes. For complex transformations, write a standalone script in the session scratch area and execute it with explicit target paths instead of nesting the transformation inside a Bash command. **Layer:** human workflow and harness tool-use pattern.
- **Traceability:** Backlog item 5 addresses F2.
- **Confidence:** Medium. Both parse failures are direct, but only two instances establish the pattern.

### 4. Test, build, type-check, lint, and verification failures

#### Finding F3 — Hand-maintained test inventories lagged behind module or task additions

- **Responsibility class:** CI/validation workflow and repository structure.
- **Evidence:**
  - `3fecb2c4-e15a-4940-85b3-00f01636b725.jsonl @ events 244–275`: an import-linter test built a synthetic package tree that lacked `evaluation.anchoring`; the test failed before reaching its intended assertion. Updating that fixture then produced invalid string literals in `tests/store/test_import_rules.py`, and two subsequent repair attempts still left the file unparsable. The broader suite later passed.
  - `336fd332-be0c-4219-a966-db1e39a43513.jsonl @ event 906`: the P5 regression run found that an exact Celery beat schedule assertion did not include the newly added P6.3 task. The expected inventory had to be reconciled with the changed schedule before verification completed.
- **Recurrence:** **2 occurrences**; **2 affected sessions; 2/6 sessions**.
- **Measurable cost:** Two observed test failures, one in each session. In the P6.2 episode, three unsuccessful edit/parse attempts followed the initial fixture failure before the fixture test could pass. The malformed-edit calls are included here as recovery cost and are not counted again as F2 occurrences.
- **Root-cause hypothesis:** Hypothesis: tests maintain parallel inventories of modules and scheduled tasks, and those inventories are easy to miss when adding a package boundary or registered task. This may be ordinary feature-test maintenance rather than a systemic test architecture defect.
- **Recommended intervention:** Add an implementation checklist entry for inventory-bearing tests: when adding an import-linter contract/module or scheduled task, update and run its synthetic-tree or schedule test in the same slice. Where feasible, build synthetic packages and schedule assertions from shared registries instead of duplicating lists. **Layer:** repository test helpers and phase workflow.
- **Traceability:** Backlog item 4 addresses F3.
- **Confidence:** Medium. The two failures are explicit; whether shared test helpers are preferable depends on how often these inventories change.

### 5. Dependency, environment, permissions, and tooling friction

No recurring dependency or environment issue met the two-session threshold. The resource and TimescaleDB events below were each observed in one session only.

### 6. Agent execution quality

#### Finding F4 — The `/goal` command limit caused one multi-session compression loop

- **Responsibility class:** Task-prompt quality and deterministic harness constraint (mixed; the evidence does not establish who authored each version of the goal text).
- **Evidence:**
  - `0497d55c-b7dd-4728-9dd6-4ad8dff1eb96.jsonl @ event 7`: `/goal` rejected a 5,766-character condition against a 4,000-character limit.
  - `757704f4-9842-4f3e-91f2-9baae7f1d907.jsonl @ event 7`: the P6.3 retry was 4,031 characters and was rejected against the same limit.
  - `336fd332-be0c-4219-a966-db1e39a43513.jsonl @ events 6–9`: the P6.3 condition was then accepted at 3,886 characters; implementation proceeded in that session.
- **Recurrence:** **1 underlying compression/retry episode** spanning **3 affected sessions; 3/6 sessions**. The two rejected submissions are retries within the same P6.3 setup chain, not two independent root-cause occurrences.
- **Measurable cost:** Two failed `/goal` calls; the first two transcripts end before repository inspection or implementation. The third transcript shows a successful 3,886-character submission. No time estimate is inferred.
- **Root-cause hypothesis:** Hypothesis: the goal payload carried detailed phase instructions that could have been summarized or linked to repository plans, while the fixed command cap was only enforced after submission. `CLAUDE.md` currently advises keeping goal text below 3,500 characters, but a command-entry limit can fail before repository guidance is read.
- **Recommended intervention:** Add a goal composer/preflight that counts the final payload, warns above 3,500 characters, and blocks submission above the observed 4,000-character cap. It should count without saving or logging prompt content. **Layer:** task-prompt template or harness command wrapper.
- **Traceability:** Backlog item 2 addresses F4.
- **Confidence:** High. Character counts, rejection messages, and the accepted retry are directly recorded.

No additional cross-session premature-edit, scope-drift, missed-validation, or weak-handoff pattern met the recurrence threshold beyond F1–F4.

## Isolated but potentially high-impact findings

### I1 — Resource pressure interrupted two heavy validations in one session

- **Finding:** **Non-recurring.**
- **Category:** Dependency/environment and validation workflow.
- **Responsibility class:** Environment/resource constraint; exact source of the memory limit is unknown.
- **Evidence:** `336fd332-be0c-4219-a966-db1e39a43513.jsonl @ events 1285–1303`: two background validations were stopped with a “system is running low on memory” notice. The assistant reported their results as unknown, did not claim a pass, then ran the suite and simulation sequentially; those later commands completed successfully.
- **Recurrence:** **1 occurrence**; **1 affected session; 1/6 sessions**.
- **Measurable cost:** Two heavy validation attempts were terminated and rerun sequentially. The recovered sequential validations passed; the interrupted attempts supplied no result.
- **Root-cause hypothesis:** Hypothesis: running multiple memory-heavy validations concurrently exceeded the session’s available memory.
- **Recommended intervention:** When local memory is constrained, run the full suite and simulation serially or verify available resources before starting concurrent jobs. **Layer:** human validation workflow.
- **Traceability:** This isolated item is not in the ranked recurring backlog.
- **Confidence:** High for the interruption and recovery; medium for the precise resource cause.

### I2 — A TimescaleDB-specific check was skipped on plain Postgres

- **Finding:** **Non-recurring.**
- **Category:** Dependency/environment and validation workflow.
- **Responsibility class:** Test environment.
- **Evidence:** `3fecb2c4-e15a-4940-85b3-00f01636b725.jsonl @ events 295, 299, 313`: the broader P6.2 suite passed on plain PostgreSQL 16, while `tests/store/test_schema.py` reported that TimescaleDB was not installed and skipped its Timescale-specific check. The final response called out that limitation.
- **Recurrence:** **1 occurrence**; **1 affected session; 1/6 sessions**.
- **Measurable cost:** One schema test was skipped; the transcript does not show a retry against TimescaleDB in that session.
- **Root-cause hypothesis:** Hypothesis: the local database service did not match the TimescaleDB environment required by that test. The transcript establishes the skip, not why that environment was selected.
- **Recommended intervention:** Require the Timescale service for any gate that claims Timescale schema coverage; keep plain-Postgres results labeled partial. **Layer:** environment/bootstrap and validation workflow.
- **Traceability:** This isolated item is not in the ranked recurring backlog.
- **Confidence:** High for the skip; medium for the environment cause.

The PDF-reading path in the P6.3 transcript also lacked installed PDF utilities and required a custom extraction attempt. The transcript shows that extraction later succeeded, so no continuing high-impact consequence is established and it is not elevated to a separate finding.

## Ranked friction backlog

Ranking considers observed consequence, recurrence across independent files, and confidence. Items 1 and 3 are distinct controls for the same planning finding: one improves the first submission; the other prevents contradictions from surviving later revisions.

1. **Add a pre-approval requirement-to-design-to-test traceability table.**
   - **Underlying finding:** F1.
   - **Intervention:** Plan template / human workflow.
   - **Affected sessions:** 2/6.
   - **Observed cost targeted:** Seven owner correction/revision turns across two plan approvals.
   - **Confidence:** Medium.
   - **Why it outranks item 2:** It targets the highest-count corrective work, and the corrections concerned durable data identity, run-scoped evidence, and halt safety; item 2 is more mechanically preventable but had lower consequence.

2. **Preflight `/goal` length before submission.**
   - **Underlying finding:** F4.
   - **Intervention:** Task-prompt template or deterministic command wrapper.
   - **Affected sessions:** 3/6.
   - **Observed cost targeted:** Two rejected calls and two session starts that stopped before repository work, followed by one accepted retry.
   - **Confidence:** High.
   - **Why it outranks item 3:** The failure was deterministic and repeated across three files in one task chain; a character counter can prevent it without interpreting requirements.

3. **Use a revision-delta and consistency pass before resubmitting a plan.**
   - **Underlying finding:** F1.
   - **Intervention:** Plan review checklist / human workflow.
   - **Affected sessions:** 2/6.
   - **Observed cost targeted:** In P6.3, four additional correction rounds followed the initial plan correction; stale design details were still called out in later reviews.
   - **Confidence:** Medium.
   - **Why it outranks item 4:** The reviewed contradictions had higher consequence than the test-inventory failures, though a checklist cannot guarantee owner decisions are predictable.

4. **Update inventory-bearing test fixtures with module and task registrations.**
   - **Underlying finding:** F3.
   - **Intervention:** Repository test helpers / phase workflow.
   - **Affected sessions:** 2/6.
   - **Observed cost targeted:** Two failed test invocations, one in each implementation session.
   - **Confidence:** Medium.
   - **Why it outranks item 5:** Both failures came from directly observable duplicated test inventories and are preventable within the affected change slice.

5. **Avoid nested shell heredocs for multiline source edits.**
   - **Underlying finding:** F2.
   - **Intervention:** Harness tool-use pattern / human workflow.
   - **Affected sessions:** 2/6.
   - **Observed cost targeted:** Two failed Bash tool calls from unmatched quotes.
   - **Confidence:** Medium.
   - **Why it ranks fifth:** The failure mechanism is clear, but only two isolated command failures were observed and both were recovered.

## Proposed CLAUDE.md amendments

No new CLAUDE.md text is justified by this corpus. The current file already says to keep goal text at or below 3,500 characters, prefer `Edit`/`Write`, and use a scratch script for complex transformations. The `/goal` command rejects oversize text before those repository instructions are necessarily loaded; repeating the same guidance in CLAUDE.md is unlikely to prevent that failure. The observed shell failures also support enforcing or operationalizing the existing guidance rather than duplicating it.

## Proposed non-CLAUDE.md harness changes

### Deterministic controls

- Add a goal-entry preflight that counts the final text before submission, warns above 3,500 characters, and blocks above the 4,000-character command limit. Store only the count and result, not prompt contents.
- Keep structural test fixtures close to the registries they model. For synthetic import-linter trees, provide a helper that creates all configured root packages and required modules. For scheduled tasks, make test updates part of task registration changes and retain an explicit assertion for required task names.
- For complex scripted Python edits, offer a scratch-script workflow that can run `py_compile` and the configured linter against listed target files immediately after transformation, before a broad test run.

### Advisory guidance and human workflow

- Before plan approval, make a short requirement/source → design → failure behavior → verification table, and distinguish unresolved owner decisions from settled choices.
- After owner feedback, record each correction with the plan sections it changes and concepts it supersedes; reread the complete revised plan before resubmission. The existing plan document already calls for in-place reconciliation; the transcripts suggest the review workflow should make that check explicit.
- On memory-constrained machines, run heavy full-suite and simulation validations serially. Report interrupted attempts as unknown until a complete rerun exists.
- When local verification claims TimescaleDB coverage, use the Timescale service rather than silently accepting a plain-Postgres skip.

## New-session preflight checklist

- [ ] Before `/goal`, remove standing repository instructions already available in the harness and count the final goal text; keep it under 3,500 characters.
- [ ] Read current progress/spec/plan, then map each explicit requirement to a plan choice, failure behavior, and verification.
- [ ] List unresolved owner decisions separately; do not choose a durable architecture or data-source rule silently.
- [ ] After each correction, remove superseded terms throughout the full plan and reread it before requesting approval again.
- [ ] Use `Edit`/`Write` for ordinary edits. Put complex transformations in a scratch script instead of a nested shell heredoc.
- [ ] Immediately parse/lint touched Python files after a scripted transformation; update module/task inventory tests alongside registrations.
- [ ] Check the intended database service before claiming schema coverage, and serialize memory-heavy validations when resources are constrained.
- [ ] Do not report interrupted or skipped checks as passing evidence.

## Measurement plan for the next six sessions

Use the next six consecutive Claude Code session transcripts for repository work, including short or empty sessions in the six-session denominator. Review each file independently first. For each metric, record JSONL event numbers, count one underlying cause/recovery loop once across session files, and separately record affected sessions. Apply the same thresholds below; do not recategorize ordinary test failures as harness friction without evidence.

| Metric | Exact definition and counting unit | Baseline from this corpus | Desired direction | Interpretation caveat |
|---|---|---|---|---|
| Goal-limit failure episodes | Distinct task goal-compression chains with a rejected `/goal`; count episodes, plus failed calls and affected transcript files separately | 1 episode; 2 rejected calls; 3/6 files touched by the chain | Fewer episodes and zero rejected calls | A single chain can span several transcripts; do not count each retry as an episode |
| Plan correction turns | Owner correction/revision turns between first plan submission and approval; count turns and plan episodes separately | 7 turns across 2 plan episodes and 2/6 sessions | Fewer avoidable turns without suppressing necessary decisions | A correction can improve safety and should not be treated as waste solely because it occurred |
| Scripted-edit failures | Distinct source-edit episodes blocked by shell parsing or leaving a target syntactically invalid; count episodes and failed tool calls separately | 2 independent shell parse failures in 2/6 sessions; a related malformed-fixture repair loop is recorded under test inventory | Fewer episodes and fewer repair calls | Shell pipelines can mask exit status; inspect tool output, not only process code |
| Structural test-inventory failures | Failed tests directly caused by stale synthetic module trees or task-registration expectations; count failed test episodes | 2 episodes in 2/6 sessions | Fewer failures | Do not include legitimate product-behavior failures |
| Resource-interrupted validations | Heavy validation attempts explicitly terminated for resource pressure; count interrupted attempts and episodes | 1 episode; 2 terminated attempts in 1/6 sessions | Zero interrupted attempts | Resource cause is only known when the transcript says so |
| Task-bearing sessions ending blocked | Sessions with an explicit task that end before task execution or with required validation unresolved; count sessions, not retries | 2/5 explicit-task sessions ended at the rejected goal stage; the sixth file had no task | Fewer blocked sessions | Also report the six-file ratio; a task may span multiple sessions |

For comparison, publish the next cohort’s raw numerator and denominator for every metric, then compare with these baselines using the same episode-deduplication rule. Keep task-bearing-session rates separate from all-transcript rates. Do not infer time savings; record elapsed time only when both timestamps for the same episode are explicit and comparable.

## Executive summary

Across six transcripts, the strongest repeated friction was plan approval churn: two implementation sessions accumulated seven corrective plan turns over architectural boundaries that should be mapped to requirements and verification before review. The most enforceable quick win is a `/goal` length preflight; two calls failed the hard 4,000-character cap in one P6.3 retry chain spanning three session files. A requirement-to-design-to-test plan table, a safe scratch-edit path, and synchronized test inventories target the other directly observed loops. The main uncertainty is whether owner plan corrections were avoidable or were the intended safety review. Apply the same ledgers and episode-counting rules to the next six transcripts to test whether rejected goals, corrective plan turns, edit failures, and stale-inventory test failures decline without reducing decision quality.
