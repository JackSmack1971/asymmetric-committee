# Claude Code Session Friction Retrospective

Corpus: six Claude Code JSONL transcripts in `.claude-session-review/`, all recorded 2026-09-27 UTC between 00:14 and 02:11 (the same evening, same repository, same user). Report date 2026-09-26 (local).

## Scope and methodology

**Corpus.** Each transcript was parsed independently into a private ledger (task, user corrections, tools, validations, failures, rollbacks, unresolved work) before any cross-session comparison. Short IDs are the first eight characters of the filename.

| ID | Lines | What it actually contains |
|---|---|---|
| `0497d55c` | 8 | `/clear`, then a rejected `/goal` (no model activity) |
| `757704f4` | 8 | `/clear`, then a rejected `/goal` (no model activity) |
| `3fecb2c4` | 320 | P6.2 scorability gate, plan mode through implementation |
| `336fd332` | 1319 | P6.3 market-data foundations, plan mode through implementation |
| `f14a6e61` | 49 | One question answered from `docs/PROGRESS.md` |
| `7967975c` | 6 | `/clear` only. Its session ID matches the session that produced this report, so it is the analysis session's own stub |

**Event references.** `L<n>` is the 0-based JSONL line index. Times are the UTC `HH:MM:SS` of that record. Durations are given only where both endpoints are timestamped in the corpus.

**Read-only constraints.** No source, test, config, Git, dependency, environment or transcript file was modified. Parsing used throwaway scripts and dumps in the session scratchpad outside the repository. Repository files were read only to interpret events: `Makefile`, `tests/services.py`, `docs/plans/*.md` and `docs/PROGRESS.md` (grep), and `.github/workflows/ci.yml` (grep) for the `make` and test-service findings. The only file written to the repository is this report.

**Unit-of-analysis rule.** One underlying episode counts once, even when it produces several failed calls, retries and corrections. Distinct causes with distinct recovery loops within one session count separately. Every finding reports three figures: episodes (occurrences), affected sessions, and, where relevant, raw event counts. They are never substituted for one another.

**Recurrence definition.** A finding is "recurring" if it appears in at least two sessions. Because the corpus has only **two substantive implementation sessions** (`3fecb2c4`, `336fd332`) plus a question session and three stubs, no finding can exceed 2/6 sessions. Both substantive sessions are consecutive, by one user, on adjacent sub-phases of one project. Independence is therefore weaker than the raw count suggests, and no finding is rated above what that supports.

**Evidence limitations.**
- Model reasoning is not in the transcripts and is not inferred.
- Two Explore subagents were launched in `336fd332` (L45, L47). Their internal work is not in the corpus.
- Tool results were reviewed at full length only where an event needed interpretation; otherwise the first few hundred characters were used.
- The transcripts contain the user's email address, organisation identifiers and absolute local paths. None are reproduced here.
- One 13m56s gap with no logged events (`336fd332` L188 00:54:24 to L190 01:08:20) has no evidenced cause and is not counted as friction.
- Several user messages contain a `:chatgpt-content-reference{index="0"}` marker. This shows the pasted text originated in another tool. It is not evidence about who or what reviewed the plan.

## Session evidence overview

"Candidate friction events" are normalised episodes under the unit rule, before recurrence analysis.

| Transcript | Primary task | Validations observed | Candidate friction events | Unresolved outcome | Evidence-quality notes |
|---|---|---|---|---|---|
| `0497d55c` | Set a P6.3 goal (5766 chars) | None | 1 (goal over limit; same episode as `757704f4`) | Task not started | Stub. No assistant activity |
| `757704f4` | Set a P6.3 goal (4031 chars) | None | 1 (same episode as above) | Task not started | Stub. Started about 1 minute after `0497d55c` |
| `3fecb2c4` | P6.2 scorability gate (plan, then implement) | 13 ruff commands, 11 pytest commands (Postgres-backed with `REQUIRE_SERVICES=1`), `mypy`, `lint-imports`, `schema_export --check`, P6.1 sims (21 passed) | 6 | Timescale-only test skipped. Full `make gate-P5` not run because `make` is absent. Nothing committed (by instruction) | Full tool I/O visible. Plan file lives outside the repo (`~/.claude/plans`) |
| `336fd332` | P6.3 market-data foundations (plan, then implement) | 33 ruff commands, 21 pytest commands (TimescaleDB+Redis), `mypy`, `lint-imports`, `schema_export --check`, migration up/down/up, P6.1 sims | 14 | Owner must validate the Alpaca condition mapping. `make` absent and CI not run. Nothing committed (by instruction) | Subagent internals not visible. 13m56s unexplained gap. Goal duration 00:44:16 to 02:08:47 (1h24m31s) |
| `f14a6e61` | Summarise phases P0 to P3 | Read of `docs/PROGRESS.md` only | 0 | None. The answer states it did not inspect code | Complete. 27 messages, 17s turn |
| `7967975c` | (none) | None | 0 | n/a | Analysis session's own stub |

Distinct episodes across the corpus: 21 (14 + 6 + 1 shared goal episode).

Candidate events reviewed and not promoted:
- New-test failures fixed in one to three calls (`3fecb2c4` L196, L235; `336fd332` L906) were treated as ordinary red/green development.
- A missing PDF toolchain (`336fd332` L120, L124, L135; recovered with a stdlib extractor by L139, 29s) is a single-session event with trivial cost.

## Recurring findings

Categories 2 and 6 have no recurring finding; see the end of this section.

### Category 1. Requirements, ambiguity, and decision churn

#### Finding F1: Plan approval needed repeated rounds; first drafts conflicted with existing boundaries and later revisions left stale text

- **Category:** 1 (with a Category 6 component: stale text after revision)
- **Responsibility class:** Mixed (model/agent execution for plan content and stale text; human decision/process for review cadence and approval wording)

**Evidence**

`3fecb2c4` (P6.2):
- L60/L67, 00:16:23: the plan says it "mints an unforgeable `ScoringTicket`" and adds `load_scoring_evidence` to `store/as_of.py` that "composes the existing `store.write` loaders".
- L68, 00:17:49 (rejected with feedback): "Do not claim `ScoringTicket` is 'unforgeable'"; "Do not make `store.as_of` call into `store.write`. That reverses the read/write boundary"; keep `evaluation/scorable.py` pure and add import-linter coverage.
- L75: the revision was appended to the plan file as "`# REVISIONS (owner feedback, supersede anything above…`", leaving the superseded text in place.
- L83, 00:20:24: second rejection ("approved, with these final corrections"). L87, 00:20:27: the agent treated it as approval and started implementing.
- Consequence: 2 rejected submissions, 4m04s from first submission to implementation start.

`336fd332` (P6.3): five rejections, then approval at L320.
- L86 to L87 (00:49:38 to 00:52:18): the plan contained "ETFs get a documented synthetic CIK" and `reference_set_id text`. Feedback: "Do not create synthetic CIKs for ETFs" and "Do NOT change the blueprint from `run_id` to `reference_set_id=bt:<date>`".
- L200 to L208: replace the daemon-thread halt capture design.
- L230 to L231: "fix these inconsistencies… Remove the remaining thread language from tests" and "Preserve existing P5 halt safety semantics. Do not reorder broker cancellation" (the v2 plan placed the capture call "in `_halt`, after `ks.halt(trigger)` and before cancels").
- L257 to L258: "Remove synchronous dispatch from the halt path entirely."
- L287 to L288: "Resolve the remaining `halt_reference_requests` schema contradiction… The earlier schema text still lists `intended_symbols JSONB`."
- L319 to L320 (01:22:25 to 01:23:02): approved.
- Consequence: 5 rejected submissions and 6 `ExitPlanMode` calls. First submission to approval is 33m24s. Of that, 11m29s is the sum of the five rejected submissions' wait-to-rejection intervals plus 37s for the approval. The rest is agent revision time including web research (L94 to L199) and cannot be attributed further.

Other sessions: none of the four remaining sessions reached plan approval.

**Recurrence**
- Episodes: 2 plan-approval loops (one per session). Rejected submissions: 7 (2 + 5). Corrective user turns: 7.
- Affected sessions: 2/6.
- Not every round is an agent defect. Rounds 208 and 258 introduce new reviewer rulings on halt design. Rounds 231 and 288 in `336fd332`, and the appended-revisions structure in `3fecb2c4`, point to stale text after patch-style revision.

**Measurable cost:** 7 rejected `ExitPlanMode` calls; 7 corrective turns; 3 of the 7 (L68 item 2, L87 items 1 to 2, L231 item 2) name a spec/architecture-boundary conflict in the agent's draft; 2 (L231, L288) name contradictions between revised and unrevised text; elapsed times above.

**Root-cause hypothesis**
- Hypothesis: plan drafts do not include an explicit check against the CLAUDE.md invariants, import-linter contracts and blueprint keys, so conflicts surface only at human review.
- Hypothesis: revisions are applied as incremental edits or appended blocks, so removed concepts survive elsewhere in the document.
- Alternative: reviewer requirements were legitimately discovered progressively, and some rounds would occur under any plan format.
- Approval wording is ambiguous. In `3fecb2c4` "approved, with these final corrections" was treated as approval. In `336fd332` "The plan is otherwise approved" (L258) and "approved subject to these final consistency fixes" (L288) were treated as rejections and resubmitted. Both handlings passed without a further user correction, so which is intended is not established.

**Recommended intervention:** advisory plan template with a boundary check and an owner-decision list (repository documentation), a CLAUDE.md plan-revision rule, and an explicit approval-wording convention (human workflow).
**Traceability:** ranked backlog item 1; CLAUDE.md amendment A2.
**Confidence:** Medium.

### Category 3. Implementation, rollback, and rework loops

#### Finding F3: Scripted file patches through shell heredocs corrupted content or failed to run

- **Category:** 3 (rework), with a Category 6 component (tool choice)
- **Responsibility class:** Model/agent execution, with a tooling contribution (Git Bash quoting on Windows)

**Evidence**

`3fecb2c4`, two episodes:
- L166 to L184, 00:24:36 to 00:24:49: a `cat >> … <<'EOF'` append failed with `unexpected EOF while looking for matching '` (L167). The agent: "Nothing ran, so I'll redo it with the file tools." Recovered with `Read` and `Edit`.
- L255 to L289, 00:30:05 to 00:30:29: a `python - <<` patch of `tests/store/test_import_rules.py` produced `missing closing quote in string literal` (L256). Two further heredoc repair attempts (L267, L274) failed (L268, L275), plus inspection calls (L262, L271, L278). Diagnosis at L266: "Bash's heredoc turned my `\n` escapes into real newlines." A single `Edit` (L284) fixed it.

`336fd332`, two episodes:
- L467 to L489, 01:27:19 to 01:27:47: `python - <<` patch of `store/_tables.py` failed with `unexpected EOF…` (L474). Recovered by writing the patch script to the scratchpad and running it (L477 to L488).
- L997 to L1016, 01:49:46 to about 01:49:58: a regex patch left `"|alpaca_calendar:aaaa1111` followed by a raw newline inside a string (L998 `invalid-syntax: missing closing quote`; L1008). Fixed by `Edit` (L1011).

**Recurrence**
- Episodes: 4. Affected sessions: 2/6.
- Context: `python - <<` was used 9 times in `3fecb2c4` and 52 times in `336fd332`. The method usually works; failures occur when the payload contains backslash escapes or unbalanced quotes.

**Measurable cost:** 4 episodes; 17 tool calls in the recovery windows (window counts include the initiating call); elapsed 13s, 24s, 28s and about 12s (about 77s in total); 2 failed repair attempts in one episode (after the initial corrupting patch); 2 sessions where the agent used `Edit`/`Write` as the recovery path.

**Root-cause hypothesis**
- Hypothesis: escape sequences in heredoc payloads are altered between the model's command text and the Python interpreter when running under Git Bash on Windows. The agent's own diagnosis at L266 supports this. Not independently reproduced here.
- The default tool guidance already prefers `Edit`/`Write`, so the failures show that guidance is not decisive when the model chooses scripting for multi-site patches.

**Recommended intervention:** CLAUDE.md rule on how to patch files (amendment A1). A hook that detects backslashes inside heredocs is possible but over-engineered for about 77s total observed cost.
**Traceability:** ranked backlog item 3.
**Confidence:** High for the mechanism; low per-episode cost.

### Category 4. Test, build, type-check, lint, and verification failures

#### Finding F5: Static-check failures (ruff, mypy, import-linter) discovered after batches of writes, then fixed in loops

- **Category:** 4
- **Responsibility class:** Model/agent execution (mixed causes across rule types)

**Evidence**
- `3fecb2c4`: failing static-check results at L128, L136, L144, L148 (00:22:24 to 00:22:46: a new import-linter contract broke on the existing `evaluation.walkforward -> store.write`; mypy `arg-type` in `store/write.py`; `UP047` in `evaluation/scorable.py`) and L247 (mypy `arg-type` in a test). Results at L256, L268 and L275 belong to F3.
- `336fd332`: 19 failing static-check results between 01:33:04 and 01:58:55, 16 of them containing `E501 Line too long (… > 100)`. Roughly 8 write-then-fix clusters at L596 to L610, L638, L674 to L679, L759, L829 to L834, L992, L1105 and L1143 to L1204. L1182 and L1196 are patch scripts whose `AssertionError` guards failed when the old text was not found. L998 belongs to F3.

**Recurrence**
- Episodes: about 10 (2 in `3fecb2c4`, about 8 in `336fd332`), approximate because clusters are contiguous fix loops that I delimited by inspection.
- Failing results: 9 in `3fecb2c4` (4 of them F3-induced) and 19 in `336fd332` (1 F3-induced), 28 in total.
- Affected sessions: 2/6.
- Rule types differ: `E501` dominates `336fd332` and is absent from `3fecb2c4`. Merging is therefore provisional.

**Measurable cost:** 28 failing tool results, each fixed within seconds (for example 00:22:24 to 00:22:49 for the first `3fecb2c4` cluster). No rollbacks. I did not sum a total elapsed time.

**Root-cause hypothesis**
- Hypothesis: code is written in large batches and checked afterwards. `ruff format` does not wrap long strings and comments, so `E501` survives it.
- Alternative: this is the intended cost of a strict lint gate. The cost is real but small.

**Recommended intervention:** early per-file feedback through a deterministic post-write check (a hook). A CLAUDE.md line is not justified by the evidence.
**Traceability:** ranked backlog item 5.
**Confidence:** Low.

### Category 5. Dependency, environment, permissions, and tooling friction

#### Finding F2: Local verification stack rebuilt by hand each session; `make` absent, so the CLAUDE.md gate cannot run

- **Category:** 5
- **Responsibility class:** Dependency/environment, with a repository-documentation component (bring-up commands are scattered across plans)

**Evidence**

`3fecb2c4`:
- L217 to L218, 00:26:32 to 00:26:36: the first Postgres-backed `pytest` run ended in `ERROR at setup`.
- L222 to L230: `docker ps -a` showed `ac-test-pg … Exited (0) 13 hours ago`; the agent read `tests/services.py`, ran `docker inspect` and `docker start`. The run was repeated at L234.
- L299: `SKIPPED [1] tests\store\test_schema.py:108: TimescaleDB not installed on this test server`. The test server was plain Postgres 16.
- L312 (final report): "Not run: the full `make gate-P5` suite, since `make` is absent on this host."
- 5 of 11 pytest commands carry an inline `TEST_DATABASE_URL`.

`336fd332`:
- L332 to L348: only the plain-Postgres container was up.
- L376: `docker run -d --name ac63-ts … -p 55433:5432 timescale/timescaledb:latest-pg16` and `docker run -d --name ac63-redis -p 56380:6379 redis:7-alpine`.
- L388: wait loop plus `grep` of `tests/services.py` for `TEST_DATABASE_URL` and `TEST_REDIS_URL`.
- 13 of 21 pytest commands carry the inline connection strings.
- L1308 (final report): "`make` is absent on this host and CI has not run on the branch."

Repository context, read only to interpret: `docs/plans/P6.md:26` says "`make` is absent on this host, so the target's commands were run by hand." `Makefile` has `gate-P6` as a stub that exits 1. `CLAUDE.md` defines done as `make gate-P<n>` passing.

**Recurrence**
- Episodes: 2 (one per substantive session). Affected sessions: 2/6.
- The two sessions chose different stacks (plain Postgres reused; new Timescale and Redis containers on other ports).

**Measurable cost**
- Bring-up and discovery calls: 3 (`3fecb2c4`: L222, L229, L234 rerun) and 4 (`336fd332`: L332, L339, L376, L388).
- 1 failed first run in `3fecb2c4`.
- 18 of 32 pytest commands (56%) carry re-typed connection variables.
- 2 of 2 final reports state that the CLAUDE.md completion command could not be run.
- 1 test skipped for lack of TimescaleDB.

**Root-cause hypothesis**
- Hypothesis: no single command starts the right services and prints the env vars; the knowledge lives in `tests/services.py`, `docs/plans/P1.md`, P5/P6 plan text and CI YAML.
- Hypothesis: `make` is missing on the host, and nothing substitutes for it.
- Uncertain: whether installing `make` is acceptable on this host.

**Recommended intervention:** a `make`-independent `scripts/verify_local` (services, checks, tests, gate) or installing `make`, plus a one-line CLAUDE.md pointer once the script exists (amendment A3, conditional).
**Traceability:** ranked backlog item 2.
**Confidence:** Medium.

#### Finding F4: `/goal` text over the 4000-character limit; two attempts rejected before a third succeeded

- **Category:** 5 (harness limit)
- **Responsibility class:** Task-prompt quality, with a harness constraint that is only reported after submission

**Evidence**
- `0497d55c` L5 to L6, 00:42:19: `/goal` command output "Goal condition is limited to 4000 characters (got 5766)".
- `757704f4` L5 to L6, 00:43:17: same message, "got 4031".
- `336fd332` L7 to L9, 00:44:16: accepted at 3886 characters.
- Each failed session had `/clear` followed by `/goal` and no model activity.
- The same standing preamble appears in all four goal texts (goal lengths: `3fecb2c4` 3874, `0497d55c` 5766, `757704f4` 4031, `336fd332` 3886 characters): "Read CLAUDE.md…", "inspect git status/diff", "preserve … unrelated untracked files", "Do not commit". `f14a6e61` L18 lists `core-invariants.md` and `CLAUDE.md` among the automatically loaded instruction files, so these lines duplicate always-loaded instructions.

**Recurrence**
- Episodes: 1 (two consecutive failed invocations of one attempt). Failed invocations: 2. Affected sessions: 2/6.
- The two sessions are consecutive retries, so this is the weakest form of recurrence in the report.

**Measurable cost:** 2 failed `/goal` invocations, 2 abandoned sessions, and 1m57s from the first failure to the accepted goal (00:42:19 to 00:44:16, both timestamped). The failure blocked all work in each session.

**Root-cause hypothesis**
- Hypothesis: the limit is only surfaced after submission; there is no local length check. Whether goal text is re-injected in full is visible in `336fd332` L1286 ("Stop hook feedback" quotes the goal), so long goals also cost context on every stop check. Not measured here.

**Recommended intervention:** a task-prompt template that drops the duplicated preamble, plus a pre-submit length check (`wc -m`).
**Traceability:** ranked backlog item 4.
**Confidence:** Medium.

### Categories with no supported recurring finding

- **Category 2, repository discovery and lost-context recovery.** Both goal sessions began with `PROGRESS.md` and `docs/plans/P6.md`, and `f14a6e61` answered a four-phase question from `PROGRESS.md` in 17s. There were 0 exactly duplicated Bash commands (42 of 42 distinct in `3fecb2c4`, 110 of 110 in `336fd332`) and 0 duplicated `Read` ranges. Re-reads of `store/write.py`, `tasks.py` and `market_data.py` (3 to 4 each) followed edits to those files. No repeated-investigation friction was found.
- **Category 6, agent execution quality (other than F1 and F3).** No premature edits, scope drift or missed validation appeared. Both goals said "do not start P6.3" or "P6.4+", and both final reports stayed within scope. Both sessions updated `docs/PROGRESS.md` as CLAUDE.md requires.

## Isolated but potentially high-impact findings

These are single-session, non-recurring, and are not systemic.

### I1 (non-recurring, `336fd332`): Background verification runs killed by memory pressure

- **Category / responsibility:** 5 / permissions or external (host condition; cause unknown)
- **Evidence:**
  - L1239 to L1240, 01:59:35: the full `REQUIRE_SERVICES=1 REQUIRE_TIMESCALE=1` suite was started in the background. L1267 to L1268, 02:00:18: the sims were started the same way.
  - L1283 to L1284, 02:00:24: both were `killed` ("stopped because the system is running low on memory"). The harness note says this "says nothing about the command."
  - L1285: the agent reported that results were unknown and did not restart.
  - L1286, 02:00:36: the goal's Stop hook fed back the full verification requirement.
  - L1256 and L1264: two failed tool calls while waiting (`sleep 240 … tail` blocked by the harness; `Monitor` called without its schema).
  - Reruns run sequentially: full suite L1297 to L1298, 02:00:41 to 02:03:38 (2m57s); sims L1301 to L1302, 02:03:41 to 02:08:24 (4m43s).
- **Consequence:** the sequential reruns took 7m40s of measured run time (2m57s + 4m43s) after the kill; the killed runs' own duration is not recorded.
- **Root cause:** unknown. Hypothesis only: two Docker containers plus two parallel test processes exceeded host memory. Not established by the corpus.
- **Confidence:** Low. Not corroborated, so no ranked item depends on it.

## Ranked friction backlog

Ranking basis: expected impact × recurrence × confidence, qualitatively; the evidence does not support a numeric score. Every item traces to a reported finding.

| Rank | Finding(s) | Proposed intervention | Layer | Affected sessions | Observed cost targeted | Confidence |
|---|---|---|---|---|---|---|
| 1 | F1 | Plan template in `docs/plans/README.md` with a mandatory "Boundary check" and "Owner decisions" section; CLAUDE.md plan-revision rule (A2); approval-wording convention | Repository documentation + CLAUDE.md + human workflow | 2/6 | 7 rejected plan submissions and corrective turns; 33m24s and 4m04s from first submission to approval or implementation start | Medium |
| 2 | F2 | Install `make` or add `scripts/verify_local` (services, checks, tests, gate); optional Makefile `test-services` target | Deterministic tooling / bootstrap | 2/6 | 7 bring-up calls across two sessions; 56% of pytest commands re-type connection vars; gate not runnable in 2/2 final reports | Medium |
| 3 | F3 | CLAUDE.md file-editing rule (A1) | CLAUDE.md | 2/6 | 4 episodes, 17 tool calls, about 77s | High |
| 4 | F4 | Goal template without duplicated preamble and a `wc -m` pre-check | Task-prompt template | 2/6 (1 episode) | 2 failed `/goal` invocations, 2 abandoned sessions, 1m57s | Medium |
| 5 | F5 | PostToolUse hook running `ruff check` on the touched file | Deterministic tooling | 2/6 | 28 failing static-check results | Low |

**Why each outranks the next**
- **1 over 2.** Item 1 targets 7 corrective user turns and the longest measured delay (33m24s). Item 2 has real but smaller measured cost (about 7 tool calls), and its fix depends on whether `make` can be installed.
- **2 over 3.** Item 2 affects every gate-dependent session and blocks the CLAUDE.md completion criterion outright. Item 3 wastes about 77s in four episodes even though its confidence is higher.
- **3 over 4.** Item 3 recurred as 4 independent episodes. Item 4 is a single episode split across two stub sessions.
- **4 over 5.** Item 4 has a clear mechanical cause and a near-free fix. Item 5 merges heterogeneous rule types and its intervention is unproven.

## Proposed CLAUDE.md amendments

### A1. File-editing rule (addresses F3)

```
## Editing files
- Change source with the Edit/Write tools. Do not patch files with `python - <<EOF` or `sed` when the payload contains backslash escapes (`\n`, `\"`) or nested quotes: under Git Bash on this host they are rewritten and the file ends up with broken string literals.
- If a scripted patch is unavoidable, Write the script to the scratchpad and run it, then run `ruff format` and `ruff check` on the touched files before the next step.
```

Why CLAUDE.md: the failure is a tool-choice decision taken at write time, which no script or CI check can prevent, and the default tool guidance did not stop it in either session. A hook that pattern-matches heredoc payloads would cost more than the observed 77s.

### A2. Plan-revision rules (addresses F1)

```
## Plans
- Revise a plan in place: rewrite the affected sections. Never append a "REVISIONS" block that leaves superseded text above it.
- Before every ExitPlanMode call, including resubmissions, grep the plan for each term, table, column or file that the feedback removed or renamed, and reconcile every hit.
- Include a "Boundary check": for each new module, import, write path or identifier, name the CLAUDE.md invariant, import-linter contract or blueprint section it touches. A choice that would change an existing safety-path order (halt/cancel/flatten), the read/write boundary (`store.as_of` vs `store.write`), identifier truthfulness (for example a placeholder CIK) or a blueprint key goes under "Owner decisions" with options; it is not built into the design as a default.
```

Why CLAUDE.md: these are execution-time behaviours the agent must know while drafting. They depend on judgement (what touches a boundary) that a script cannot decide. The stale-term grep could be scripted, but the plan lives in the agent's context and the check must run before submission. The examples come from the observed rejections.

### A3. Verification pointer (addresses F2; apply only after `scripts/verify_local` exists)

```
- `make` is not installed on the Windows dev host. Run `scripts/verify_local` (services, check, test, gate <Pn>). A final report must state whether the phase gate ran via `make`, via `verify_local`, or not at all.
```

Why CLAUDE.md: a pointer to the deterministic tool is durable, repository-specific guidance. The tool itself is the intervention. Do not add this line before the script exists.

No CLAUDE.md change is justified for F4 (a task-prompt template concern) or F5 (weak evidence and a better hook).

## Proposed non-CLAUDE.md harness changes

### Deterministic controls

1. **Verification stack (F2).**
   - Smallest step: install GNU `make` on the host so the existing `gate-P<n>` targets run as CLAUDE.md states.
   - Add a Makefile `test-services` target, or a script, that idempotently starts `timescale/timescaledb:latest-pg16` (with `--platform linux/amd64` as used in `336fd332` L376) and `redis:7-alpine` on fixed ports and prints `TEST_DATABASE_URL`, `TEST_REDIS_URL`, `REQUIRE_SERVICES=1`, `REQUIRE_TIMESCALE=1`.
   - If `make` cannot be installed: `scripts/verify_local` with `services`, `check` (ruff check, ruff format --check, mypy, lint-imports, `schema_export --check`), `test`, `gate <Pn>`. The check set is the one both substantive sessions assembled by hand.
   - Implement `gate-P6` so it does not exit 1 as a stub before P6 closes.
2. **Post-write static check (F5).** A PostToolUse hook that runs `ruff check` (including `E501`) on the file just written and returns the output. It moves feedback from a batch to the write. It is unproven and does not remove the loop, only shortens it.

### Advisory guidance

3. **Plan template (F1).** Add "Boundary check", "Owner decisions" and "Supersession rule" sections to `docs/plans/README.md`. The evidence is the three boundary conflicts and two stale-text rounds above.
4. **Approval wording (F1, human workflow).** Rejection feedback should begin with either "APPROVED, apply these edits and proceed" or "NOT APPROVED, resubmit". Consolidate feedback into one round where possible. `3fecb2c4` and `336fd332` handled "approved with corrections" differently, and the corpus cannot say which was wanted.
5. **Goal template (F4, task-prompt template).** Goal text contains only the objective, the scope fence ("do not start P6.4+") and acceptance checks. Drop instructions already loaded automatically (read CLAUDE.md, preserve unrelated changes, do not commit). Run `wc -m` before submitting.
6. **CI as fallback (F2, weak evidence).** Both final reports note that CI has not run on the branch. Running the gate in CI on the work-in-progress branch would supply a gate result when `make` is unavailable locally. Pushing needs the owner's decision, so this is advisory only.

## New-session preflight checklist

Each line traces to an observed event.

- [ ] Goal text is 4000 characters or fewer (`wc -m`) and omits rules already loaded automatically. (F4)
- [ ] `make --version` works. If it does not, use the verification script instead of hand-assembling commands. (F2)
- [ ] The Postgres test service is TimescaleDB, not plain Postgres, when the change touches schema or migrations, and Redis is up. Otherwise a Timescale test skips. (F2)
- [ ] Connection variables are exported once, not re-typed per command. (F2)
- [ ] The plan has a "Boundary check" and an "Owner decisions" list, and revisions are made in place with the removed terms grepped. (F1)
- [ ] Plan feedback starts with "APPROVED" or "NOT APPROVED". (F1)
- [ ] File patches use Edit/Write; no backslash escapes or nested quotes in `python - <<` payloads. (F3)
- [ ] Lines stay at or under 100 columns, including comments, docstrings and strings. (F5)
- [ ] Heavy verification runs are executed one at a time, not as parallel background jobs. (isolated, I1)

## Measurement plan for the next six sessions

**Cohort rules.** Count the same events with the same definitions used here. Include stub sessions, but report every metric both per all sessions and per substantive implementation session (a session with at least one Write or Edit to repository code). The baseline has only two substantive sessions, so values are directional. Aim for at least four substantive sessions in the next cohort.

**Comparison procedure**
1. Copy the six transcripts read-only to a review folder.
2. Parse each JSONL independently. Record for each `tool_use` its name, input, timestamp and paired `tool_result`, including `is_error`.
3. Apply the definitions below with the same regexes, then apply the unit-of-analysis rule and manual clustering of contiguous fix loops.
4. Compute each metric per session, then the median and range per cohort.
5. Compare against the baseline column. A metric moves only if the direction holds in at least 2 of the substantive sessions.
6. Do not compare elapsed times unless both endpoints are timestamped.

| Metric | Exact definition | Unit | Collection method | Baseline from these six sessions | Desired direction | Interpretation caveat |
|---|---|---|---|---|---|---|
| Corrective user turns | Rejected `ExitPlanMode` results carrying feedback, plus user messages that redirect work; excludes `/clear`, `/goal` and Stop-hook feedback | per session | `tool_result` with `is_error` on `ExitPlanMode`; manual read of user messages | 7 in total (`3fecb2c4` 2, `336fd332` 5, others 0); 3.5 per substantive session | Down | Some rounds add legitimate new requirements, so zero is not the target |
| Plan submissions per approved plan | `ExitPlanMode` calls until approval or implementation start | per plan | count `tool_use` | 2 (`3fecb2c4`, approval implicit at L87), 6 (`336fd332`) | Down | Approval semantics differ between the two baselines |
| Plan-time boundary conflicts | Rejection items citing a spec, invariant or architecture boundary violation | per plan | manual read of rejection text | 3 (L68 item 2, L87 items 1 to 2, L231 item 2) | Down | Manual classification |
| Scripted-patch corruption episodes | Episodes where a heredoc/`sed` patch produced a shell error or invalid syntax, then a repair | per session | `unexpected EOF`, `invalid-syntax` results tied to a preceding heredoc | 2 and 2 | Down to 0 | Low per-episode cost |
| Failing static-check results | Tool results from `ruff`, `mypy` or `lint-imports` containing an error summary | per session | regex `Found \d+ errors?`, `: error:`, `E501`, `Broken contracts` | 9 (`3fecb2c4`, 4 F3-induced) and 19 (`336fd332`, 1 F3-induced) | Down | Volume follows amount of code written |
| Service bring-up calls before first passing services-backed test | Bash calls that start or inspect containers or read `tests/services.py`, counted from session start to the first passing run | per session | manual, keyed on `docker` and `services.py` | 3 and 4 (baseline counts calls, not time) | Down | Baseline sessions started with services in different states |
| Inline env-var rate | pytest Bash commands containing `TEST_DATABASE_URL` divided by all pytest commands | ratio | regex on commands | 5/11 and 13/21 (56% pooled) | Down toward 0 | Falls only if the environment persists between calls |
| Gate runnable | Final report states the CLAUDE.md gate command ran | yes/no per session | read final message | 0 of 2 substantive sessions | Up | Depends on gate existing (`gate-P6` is a stub) |
| Failed `/goal` invocations | `/goal` outputs containing "limited to 4000 characters" | count | grep | 2 (1 episode) | 0 | Very small baseline |
| Sessions ending with unresolved work | Session where the goal was not started, or a final report lists a blocker or skipped required check | sessions per cohort | manual | 4 of 6 (2 stubs never started; `3fecb2c4` skipped test and gate; `336fd332` owner blocker and gate) | Down | Not all blockers are harness friction (for example the owner's Alpaca mapping validation) |
| Repeated identical commands | Exact-duplicate Bash command strings | rate per session | string compare | 0 in both substantive sessions | Stay at 0 | Guard metric; near-duplicates after edits are expected |
| Friction events attributable to missing repository guidance | Episodes classified "repository documentation" or "repository structure" under this report's labels | per session | apply the same responsibility labels | 1 (F2) per substantive session | Down | Classification is judgemental; label before looking at metric values |

Elapsed-time baselines (plan-approval loops of 4m04s and 33m24s) may be reused only where both endpoints are timestamped.

## Executive summary

Across six transcripts, only two contain implementation work, and the most costly recurring friction in both is the plan-approval loop: 7 rejected `ExitPlanMode` submissions (2 in P6.2, 5 in P6.3), with 33m24s from first submission to approval in P6.3, driven by draft designs that crossed existing architecture boundaries and by revisions that left stale text behind. The strongest evidence-backed interventions are a plan template with a boundary check and owner-decision list (plus an in-place revision rule) and a scripted local verification path, because `make` is absent on the host and both final reports say the CLAUDE.md gate could not be run; a file-patching rule in CLAUDE.md addresses four shell-heredoc corruption episodes with a high-confidence cause. The main uncertainty is generalisability: both substantive sessions are consecutive work by one user on adjacent sub-phases, so no finding is seen in more than 2 of 6 sessions, the goal-length episode is a single episode split across two stub sessions, and approval wording is ambiguous between the two plan loops. The next six sessions should test the interventions by comparing corrective turns, plan submissions per approved plan, plan-time boundary conflicts, scripted-patch corruption episodes, service bring-up calls and gate-runnable status against the baselines here, using the same counting rules and per-substantive-session reporting.
