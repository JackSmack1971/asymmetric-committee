# Repository History: Repeatable Workflows

**Review date:** 2026-09-26  
**Repository:** `asymmetric-committee`  
**Evidence scope:** commits reachable from local and remote refs inspected in this worktree, plus current `docs/plans/`, `docs/PROGRESS.md`, blueprint and current `git status`. This is a short, concentrated history (2026-09-25 to 2026-09-26), so “repeatable” below means a recognizable pattern used across multiple phases or commit groups, not a statistically established long-term practice. Current uncommitted files are called out separately and are not described as committed history.

## Executive summary

The history shows a phase-oriented engineering workflow: establish a plan against the blueprint, resolve ambiguities in docs, implement in small conventional commits, add invariant-focused tests, wire phase gates, and record status/evidence in `docs/PROGRESS.md`. Several technical workflows recur inside this shape: contracts before consumers with generated schema freshness; point-in-time storage and import boundaries; fixture-backed ingestion; privacy/anonymization leak checks; layered model-call reliability controls; and transactional, idempotent orchestration with explicit recovery evidence.

The strongest recurring signal is not simply “tests accompany features.” It is the pairing of a named invariant with a boundary, a failure/replay test, and a recorded phase decision. The history also contains limits: all visible dates are within two days; P5 history is incomplete relative to the current worktree; several plans report validation that cannot be corroborated from commits alone; and the current tree is heavily modified/untracked.

## Evidence and limits

Observed history starts with repository bootstrap (`3eed1d2`) and proceeds through P0–P5 work. Branches include `main`, `phase/P3`, `phase/P4`, and `phase/P5`; P5 is current HEAD at `4e41722`. P3 and P4 have phase-tip commits, while P5's recorded progress says substantial later P5 work is local and uncommitted. The current worktree has modifications across P5 code, tests, docs, config and dependencies, plus new files in `execution/`, `orchestration/`, `api/`, `evaluation/`, migrations and tests. These cannot be attributed to committed history based on the current evidence.

The report uses commit subjects and file lists as direct evidence of changes. `docs/plans/P<n>.md`, `docs/PROGRESS.md`, and the blueprint provide design/validation context but are repository-authored records rather than independent proof that every stated gate ran. In particular, current P5 progress reports local validation and says CI has not run. No external CI run was inspected here.

## Workflow 1: Phase plan → specification reconciliation → implementation → gate/status record

**Pattern.** Phase work repeatedly begins with a plan or explicit specification decision, often before implementation; code is delivered in focused commits; phase gates and progress notes close the loop. This is clearest for P0/P1/P3/P4/P5.

**Observed sequence and files.**

- P0: `ac2f2f4` adds `docs/plans/P0.md` and updates the blueprint/packaging; `000b8d6`, `32741c9`, and `1aac3ae` implement contracts, schema export, and config; `b610e06` wires `Makefile` gate and updates `docs/PROGRESS.md`.
- P1: `e8aa82c` adds `docs/plans/P1.md` and edits blueprint §4.3; `51d3a62` adds the bitemporal store and import rules; `b5a1a34` adds ingest clients/parsers; `9c63de6` adds backfill/snapshot replay smoke; `c9a7fbd` adds the phase gate, CI service configuration, plan/progress updates.
- P3: `27bdd31`, `816660f`, and `a27da4b` successively create/refine `docs/plans/P3.md`, recording gaps, owner decisions and a topology conflict before/alongside feature work. `94f11d2` and later progress updates record state and open conflicts. `ac4be8b` adds the P3 gate and fixes a Redis timing test.
- P4: `fa66ed0` lands the P4 plan/progress with pooling, stacker, sizing, CIO and gate changes together. `docs/PROGRESS.md` records design decisions, deviations and evidence.
- P5: `4725d44` reconciles blueprint §§4.3/9 and adds/updates `docs/plans/P5.md` before the `eb1f424` contracts, store/sink and pipeline commits.

**Typical files touched.** `docs/plans/P<n>.md`, `docs/PROGRESS.md`, `docs/asymmetric-committee-blueprint.md`, `Makefile`, `.github/workflows/ci.yml`, implementation modules, and targeted tests. Plan README (`docs/plans/README.md`) describes ordered small commits as a convention.

**Why repeatable / useful.** Reconciliation commits (`9d1980d`, `4725d44`) show the spec is treated as mutable authority, not just a retrospective narrative. Progress notes preserve decisions, deviations, unresolved issues and validation scope. This gives later work a traceable decision trail.

**Operational caveat.** Completion language differs by phase and branch. P4 progress notes say its gate remains a stub/not fully run; P5 current progress describes a local gate-equivalent run but also says `make` is unavailable and CI has not run. Do not infer a passing official gate from phase labels or commit subjects.

## Workflow 2: Shared contracts first; generated schema stays synchronized

**Pattern.** Message/enumeration shapes are established in `contracts/` before stage consumers rely on them. LLM-facing JSON schema is generated from Python models and checked for freshness.

**Evidence.** `000b8d6` introduces `contracts/enums.py`, `contracts/models.py` and contract tests. `32741c9` adds `contracts/schema_export.py`, checked-in JSON schemas and schema tests. P2 (`aecf4b4`), P3 (`38b0dbf`), P4 (`fa66ed0`) and P5 (`eb1f424`) extend models/enums alongside strategies/tests. The P5 plan explicitly calls for contracts before store consumers; the P5 record introduces stage-boundary record types before `store/write.py` depends on them.

**Files.** `contracts/{enums.py,models.py,data.py,schema_export.py}`, `contracts/schemas/*.json`, `tests/contracts/{strategies.py,test_*.py}`, and downstream stage/store modules. The documented freshness commands are `uv run python -m contracts.schema_export` and `uv run python -m contracts.schema_export --check`; the latter is also included in phase validation notes.

**Relevant safeguards.** Enum and compatibility tests constrain serialized shapes; schema tests and `--check` detect drift between the source model and generated artifacts. This is a source/derived-file workflow with a clear authority boundary.

## Workflow 3: Point-in-time data path with persistence isolated behind store APIs

**Pattern.** Store schema and `as_of` reads own database concerns; decision logic consumes typed records and should not reach directly into SQL or ingestion. Historical availability is checked at read/decision boundaries.

**Evidence.** `51d3a62` creates `store/as_of.py`, `store/db.py`, migrations, data contracts and import-boundary tests. It also adds rules preventing decision packages from importing database internals. P1 ingestion work (`b5a1a34`) writes source-derived records; P2 (`aecf4b4`) builds the walk-forward pipeline and store writes; P5 (`19fe1f7`) extends migrations/write helpers. Import-boundary enforcement recurs in `7421201` for execution and sink. Progress notes state the rule as point-in-time reads through `store/as_of.py` and document import-linter/Ruff rules.

**Files.** `store/as_of.py`, `store/_tables.py`, `store/write.py`, `store/migrate.py`, `store/migrations/versions/*`, `contracts/data.py`, `pyproject.toml`, and `tests/store/test_as_of.py`, `test_schema.py`, `test_import_rules.py`; decision packages include `features/`, `gate/`, `agents/`, `committee/`, `risk/`, `evaluation/`, and `universe/`.

**What is actually repeatable.** The implementation pattern appears in P1/P2/P5 and is reinforced by static import checks; schema tests compare migrations to metadata. This guards both temporal validity and architectural separation.

## Workflow 4: External ingestion with shared throttling, recorded fixtures and replay smoke

**Pattern.** External-provider code is isolated in `ingest/`, rate-limited where needed, exercised using local fixtures/mocks, and checked through a replay/backfill smoke path.

**Evidence.** `b5a1a34` groups EDGAR client, Redis limiter, Alpaca bars, XBRL, Form 4 and news provider code with parser/client tests and fixtures. `9c63de6` adds a backfill command, a synthetic universe, recorded HTTP responses and `tests/ingest/test_backfill_smoke.py`. P1 gate work (`c9a7fbd`) configures Postgres/TimescaleDB and Redis services in CI. The current guidance requires the shared EDGAR limiter and fixture/mock testing rather than live paid APIs.

**Files.** `ingest/{http.py,edgar_client.py,edgar_submissions.py,edgar_xbrl.py,edgar_form4.py,alpaca.py,news.py,backfill.py,timeutil.py}`, `config/pipeline.yaml`, `tests/ingest/*`, `tests/fixtures/{http,alpaca,edgar,news,universe_smoke.yaml,synth.py}`, and `universe/{snapshot.py,sectors.py}`.

**Specific safety/quality check.** `docs/PROGRESS.md` records that news timestamp/revision reliability remains unverified against real recordings. This is a useful example of the workflow preserving an unresolved data-quality issue rather than treating fixture success as source reliability evidence.

## Workflow 5: Anonymize model inputs and test for identity/numeric leakage

**Pattern.** Inputs are rendered/partitioned per agent, identity aliases are point-in-time, sensitive names/numbers are scrubbed, and tests assert what does not appear in prompts or rendered text.

**Evidence.** `3f32194` adds alias contracts/config/store reads and tests. P3 then adds renderer leak harness (`f75e202`), PERSON aliases and hashed executive tokens (`314f387`), currency/share/date scrubbing with evidence IDs (`39bbe7b`), per-agent partitions and identity/number leak tests (`4c6bea1`), alias-coverage guard (`fd87559`) and stop-word short-name coverage (`feb10b8`).

**Files.** `config/aliases.yaml`, `config/aliases.py`, `contracts/data.py`, `contracts/enums.py`, `store/aliases.py`, `store/as_of.py`, `features/renderer.py`, `agents/partitioner.py`, `agents/partitions.py`, and tests under `tests/agents/`, `tests/features/`, `tests/universe/`, `tests/store/`, `tests/config/`, `tests/contracts/`.

**Invariant evidence.** Tests are aimed at leak classes (identity, numeric/date, coverage) rather than only successful rendering. Progress notes describe availability re-checks and the limitation that colloquial short names must be added to config. The history therefore captures both automated guards and a manual data-coverage dependency.

## Workflow 6: Reliable model dispatch with bounded cost, caching, retries and resumable task outcomes

**Pattern.** The LLM boundary is centralized. Provider calls are paced, retried/validated, cost-budgeted, cached by configuration, and routed to a DLQ or partial-run outcome when recovery is not safe.

**Evidence.** P0 adds per-model rates/prices and fail-closed cost (`cb003d5`). P3 adds model family/cutoff/temperature/reasoning metadata (`1e7d939`), verdict validation/repair/discard (`6855d00`), OpenRouter client, Redis token bucket, cache and DLQ (`58ff8d9`), run budget breaker/config-hash cache key/e2e test (`339f8eb`), and async batch runner/prompts (`518d7fa`). `ac4be8b` fixes the Redis burst test while adding the phase gate.

**Files.** `config/models.yaml`, `config/loader.py`, `agents/base.py`, `agents/llm/{openrouter.py,ratelimit.py,store.py}`, `agents/runner.py`, `prompts/agents/*`, `contracts/models.py`, `tests/agents/{test_openrouter.py,test_llm_redis.py,test_base.py,test_runner.py,test_p3_e2e.py,test_prompts.py}`, and `docs/PROGRESS.md`.

**Decision details from progress records.** Budget/cost/model metadata failures stop dispatch; tasks not yet called are DLQ'd; only completed task keys short-circuit on resume; retriable or discarded tasks can run again; partial status distinguishes an interrupted/failed run from a complete one. Tests use HTTP mocks/fixtures, not live paid model calls.

## Workflow 7: Decision pipeline and persistence built around atomicity, idempotency and replay

**Pattern.** Stage records are modeled explicitly; one step's persistence is transactional; repeated delivery/replay is idempotent; failures are injected mid-write to prove rollback; orchestration delegates reads/writes through loader and sink boundaries.

**Evidence.** P5 commit sequence is especially clear: contracts (`eb1f424`) → migration and write helpers (`19fe1f7`) → `DecisionSink.flush_step` and atomicity tests (`4ad1005`) → import-boundary contracts (`7421201`) → plan/spec reconciliation (`4725d44`) → weekly `BacktestOrchestrator` plus integration-style tests (`4e41722`). `tests/orchestration/test_sink.py` includes rollback/replay cases per progress record; `tests/orchestration/test_pipeline.py` exercises weekly steps and failure paths with mocked model calls and a recording sink.

**Files.** `contracts/models.py`, `contracts/enums.py`, `store/_tables.py`, `store/migrations/versions/0003_p5_sink.py`, `store/write.py`, `orchestration/{sink.py,pipeline.py}`, `tests/orchestration/{test_sink.py,test_pipeline.py}`, and `tests/store/test_import_rules.py`.

**Current-tree extension, not yet committed in inspected history.** `docs/PROGRESS.md` describes additional P5 work for commitment recomputation, anchoring, execution state, worker claims/locks, reset, API, scheduling, Celery and broker gateway. The current worktree shows these as untracked/modified files (`execution/*`, many `orchestration/*`, `api/main.py`, migrations `0004`–`0006`, `evaluation/anchoring.py`, and associated tests). They are relevant to the present workflow but not evidence of committed repetition yet.

## Workflow 8: Layered verification with fast unit checks and service-backed gates

**Pattern.** Feature tests are supplemented by reusable phase gates, lint/type/import checks, schema freshness, and service-backed integration tests where behavior depends on Redis/Postgres/TimescaleDB.

**Evidence.** Bootstrap (`3eed1d2`) creates Make targets, Compose services and CI. P0 (`b610e06`) and P1 (`c9a7fbd`) turn gates into phase-level checks; P2 adds gate-P2 with implementation (`aecf4b4`); P3 adds gate-P3 (`ac4be8b`); P4 lands a gate in its feature commit (`fa66ed0`); P5 commits import-boundary checks and records its gate requirements in plan/progress. CI workflow is `.github/workflows/ci.yml`; operational targets and gates are in `Makefile`.

**Files.** `Makefile`, `.github/workflows/ci.yml`, `docker-compose.yml`, `pyproject.toml`, tests by subsystem, `contracts/schema_export.py`, and phase docs. Tests include unit/property-style contract tests, service-backed tests, fixture replay and phase e2e scenarios.

**Limit.** Gate existence and gate success are different facts. Progress notes explicitly distinguish stub/unrun gates from local command-level runs and CI. The P5 gate requires services and TimescaleDB; a run without that service requirement is not the documented gate.

## Workflow 9: Small conventional commits and phase/PR branch integration

**Pattern.** Changes are often split into focused commits with area-prefixed conventional subjects; phase work is isolated on branches and merged into `main` through PR merge commits.

**Evidence.** Subjects such as `feat(contracts)`, `feat(store)`, `feat(ingest)`, `feat(agents)`, `feat(committee)`, `feat(orchestration)`, `docs(plan)`, `docs(spec)` and `build` appear throughout the history. P0/P1 and several targeted fixes are followed by merge commits (`f43fe10`, `7c6df96`, `e260e4c` and others). P3/P4/P5 appear as distinct phase branches. P5 sequence decomposes the sink feature into model, storage, transactional boundary, architecture lint and orchestration commits.

**Files.** This is a repository-process pattern rather than a fixed file set; commit messages and branch graph are its evidence. Related durable records are phase plans/progress and CI/gate definitions.

**Limit.** The present worktree is dirty and contains substantial P5 edits. Current changes cannot be assumed to follow the committed small-commit pattern until they are committed. No recommendation about committing or branch operations is implied by this report.

## Cross-workflow observations

1. **Invariant-first testing is prominent.** Examples include as-of selection, schema parity, leak absence, budget stops, retry/resume semantics, atomic rollback, commitment mismatch, and idempotent replay.
2. **The documentation is part of the workflow.** Plans capture sequencing and decisions; progress records explain deviations and status. These notes are essential to understand why several implementations differ from initial directives.
3. **Boundary enforcement is repeated mechanically.** Import-linter/Ruff rules and dedicated tests constrain database and broker-facing access, in addition to architectural prose.
4. **Evidence claims have different strengths.** Commit history proves files changed; tests prove only their covered behavior when run; progress notes report wider validation but should not be conflated with independently inspected CI results.
5. **The timeline limits recurrence confidence.** The visible history is recent and densely sequenced, which supports recognizable repeated workflows within this project but not claims about stable practice over longer periods.

## Files to consult for future work

- Authority and invariants: `docs/asymmetric-committee-blueprint.md`, `AGENTS.md`
- Phase status and open issues: `docs/PROGRESS.md`
- Phase plans: `docs/plans/P0.md` through `docs/plans/P5.md`
- Gate/CI behavior: `Makefile`, `.github/workflows/ci.yml`, `docker-compose.yml`, `pyproject.toml`
- Current committed P5 implementation: `contracts/`, `store/`, `orchestration/pipeline.py`, `orchestration/sink.py`
- Current uncommitted P5 extension: `execution/`, `api/`, additional `orchestration/`, migrations `0004`–`0006`, and corresponding tests

## Conclusion

The repository has a coherent, recognizable workflow centered on phase plans and invariant-driven implementation. The strongest technically reusable practices are contracts/schema-first changes, point-in-time/store boundaries, fixture-backed provider integration, explicit privacy leak tests, bounded model invocation, and transaction/replay verification. The process’s strongest practical improvement is maintaining the evidence distinction already visible in progress notes: planned vs implemented, tests run vs gates passed, and local work vs committed/CI-verified work.
