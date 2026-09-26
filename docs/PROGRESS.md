# Progress

Read at the start of every session; update at the end. Spec: `docs/asymmetric-committee-blueprint.md` (§17 phases).

| Phase | Status | Gate | Branch | Notes |
|---|---|---|---|---|
| P-boot Bootstrap | Done (pending PR merge) | `make lint` + `make test` + `make up` healthy | `phase/P-boot` | Skeleton, uv, compose, Makefile, CI. No business logic. |
| P0 Contracts | Done (pending PR merge) | `make gate-P0` passes (lint + tests/contracts + tests/config + schema freshness) | `phase/P0` | `contracts/` enums + models + strict LLM schemas; `config/` yaml + loader. Plan: `docs/plans/P0.md`. |
| P1 Data | Done (pending PR merge) | `make gate-P1` passes (lint + import-linter + store/ingest/universe/config tests incl. backfill smoke) | `claude/bitemporal-timescaledb-as-of-9yro0f` | Schema + Alembic, `store/as_of.py`, 4 ingestors, EDGAR limiter, freshness, universe snapshots, backfill CLI. Plan: `docs/plans/P1.md`. 2-year real backfill still to run (needs network + keys). |
| P2 Features + gate + baseline | Done (pending PR merge) | `make gate-P2` passes (lint + contracts/config + deterministic features, leak-safe gate, risk properties, 12-week smoke) | `phase/P2` | `fs_v1`; fixture gate recall: 100%. Plan: `docs/plans/P2.md`. |
| P3 Agents | Not started | `make gate-P3` (stub) | `phase/P3` | |
| P4 Committee + risk + CIO | Implemented, gate not fully run | `make gate-P4` (lint + contracts/config/features/agents/committee/risk + schema check) | `phase/P4` | Plan: `docs/plans/P4.md`. Not marked Done: see P4 step 5. |
| P5 Orchestration + execution | Not started | `make gate-P5` (stub) | `phase/P5` | |
| P6 Evaluation | Not started | `make gate-P6` (stub) | `phase/P6` | |
| P7 Dashboard | Not started | `make gate-P7` (stub) | `phase/P7` | |
| P8 Forward test | Not started | `make gate-P8` (stub) | `phase/P8` | |

## Decisions

- 2026-09-25 — `uv` with `package = false`; top-level packages (`contracts/`, `store/`, …) sit at repo root exactly as §15, no `src/` layout.
- 2026-09-25 — `dashboard/` is an empty placeholder until P7; not in compose yet.
- 2026-09-25 — Docker image installs uv via pip (not `COPY --from=ghcr.io/...`) so builds work behind restrictive proxies.
- 2026-09-25 — Ruff/mypy exclude `docs/` and `dashboard/`.

- 2026-09-25 — P0: §3.1 `data_sufficiency`/`horizon_days` are `DataSufficiency`/`Horizon` enums, not Literals (spec edited).
- 2026-09-25 — P0: LLM output is split from the system envelope (`AgentVerdictLLM` → `AgentVerdict.from_llm`, same for red team and CIO). Only the `*LLM` schemas go to `response_format`, so the LLM never writes `model_served` or weights (spec §3.1/§10.2 edited).
- 2026-09-25 — P0: strict schemas inline `$ref`s and drop constraint keywords; Pydantic re-validates ranges after parsing. Regenerate with `uv run python -m contracts.schema_export`.
- 2026-09-25 — P0: `security_id` is a positive int; `MAX_POSITION = 0.08` is a hard contract cap, and `risk.yaml` may only be tighter.
- 2026-09-25 — P0: `load_config()` rejects placeholders (TODO slugs, empty sectors) unless `allow_placeholders=True` (tests only). `RUN_BUDGET_USD` env overrides `pipeline.yaml`.

- 2026-09-25 — P1: one version rule for every fact table: per natural key, max `(available_at, source_version)` among rows with `available_at <= as_of`. Inserts are `ON CONFLICT DO NOTHING`; `fundamentals_asfiled` and `decision_commitments` have insert-only triggers.
- 2026-09-25 — P1: spec §4.3 edited: `fundamentals_asfiled` + `period_start`/`fiscal_period`/`form`, `news_items` + `summary`, `price_bars.ts` → `event_time`, new `feed_health`.
- 2026-09-25 — P1: invariant 2 enforced by import-linter (`make lint`): agents/features/gate/committee/risk/evaluation/universe may not import `sqlalchemy`, `psycopg`, `store._tables`, `store.migrate`; decision code also not `store.write`/`store.db`/`ingest`. Ruff TID251 bans `store._tables` outside `store/`.
- 2026-09-25 — P1: EDGAR limiter = Redis sliding-window log, 8 per 1.05 s (5% jitter margin), shared by all workers. Live EDGAR requires `REDIS_URL`; the in-process limiter is only for replay.
- 2026-09-25 — P1: bars are unadjusted (`adjustment=raw`); split-adjusting history is look-ahead. P2 features must handle splits point-in-time.
- 2026-09-25 — P1: EDGAR `acceptanceDateTime` is read as Eastern despite its `Z`; Alpha Vantage `time_published` read as Eastern. Both are the later reading, so never look-ahead.
- 2026-09-25 — P1: universe ranks by log(ADV20) (SPEC-GAP: §4.4 "liquidity-adjusted score" undefined). Snapshots store every in-sector candidate with a reason.
- 2026-09-25 — P1: `pipeline.yaml` `freshness_sla_hours` (price 72h to cover weekends, fundamentals/insider 36h, news 1h). Trading-calendar-aware SLAs are left for P5 (kill switch).
- 2026-09-25 — P2: `fs_v1` uses annualized 20-day volatility (daily standard deviation × √252); the existing `k=0.02` and dispersion λ=0.5 are now the baseline methodology.
- 2026-09-25 — P2: portfolio volatility uses a deterministic diagonal covariance estimate because §8.1 does not define covariance estimation. Sector and volatility scaling never redistribute clipped/dropped weight.
- 2026-09-25 — P2: quant baseline emits `CommitteeDecision` with a singleton `quant_baseline` agent weight, allowing it to share the exact risk path without pretending to be an LLM voter.
- 2026-09-25 — P0 contract amendment (PR #8): `AgentVerdict`/`RedTeamVerdict` gain a system-filled `valid: bool` (backtest cutoff validity), set only in `agents/base.py` and never LLM-facing. It is required with no default (fail closed). Committee/evaluator exclude `valid=False` and never recompute it. Spec §3.1/§10.1/§10.3 reconciled; no persisted verdict rows exist before P3, so no migration is needed.
- 2026-09-25 — P0 config: `ServedModel` gains required `rpm`, `tpm` (>0) and `input_/output_price_usd_per_mtok` (≥0). Local cost (tokens × config prices) is authoritative; provider `usage.cost` is only a fallback when token counts are missing; both missing → `UsageUnavailableError` (fail closed, run `PARTIAL`). Spec §10.1/§10.2 edited. Placeholder rpm/tpm/prices in `models.yaml` are TODO(owner).
- 2026-09-25 -- P3 prep: anonymizer alias source. Identity aliases (name, ticker, CIK) come from stored `securities` via `store.as_of.alias_list` (listed-on-`as_of` only); brand/product aliases live in repo config `config/aliases.yaml` keyed by CIK. New contracts `Alias`/`SecurityAliases`/`AliasList` (+ `AliasKind`) in `contracts/data.py`. `agents/partitioner.py` holds only `AliasMasker` so far; data isolation is still P3. Shared aliases mask to `ENTITY_00`. Spec §12.1 edited. `tests/store/test_aliases.py::test_alias_list_is_point_in_time` needs Postgres and was skipped locally.

- 2026-09-25 — P0 follow-up: `config/sectors.yaml` (SIC range → sector → SPDR ETF, §4.1) added with `SectorsConfig` in the loader; ranges must be disjoint. The draft crosswalk ships `confirmed: false`, so production startup fails until the owner reviews it and flips the flag.
- 2026-09-25 — P3 plan (`docs/plans/P3.md`) owner decisions: red team stays non-voting and the §3 five-agent roster is unchanged (Platt/conformal ideas are P4 ablations at most); `AliasKind.PERSON` with 8-hex SHA-256 executive tokens plus role tokens; null `post_holdings` omitted, insider agent stays active with `ΔPos = shares × price / ADV30$`; Redis LLM cache keyed on `(config_hash, available_at, payload_hash)`, guard is `available_at <= as_of`, one `trials` row per distinct `config_hash` (never per cache miss). Spec edits for §3 Insider, §12.1 item 2 and §10.2 still to be made.
- 2026-09-25 — P3 step 1: `ServedModel` gains `family`, `measured_effective_cutoff` (nullable, required key), `accepts_temperature`, optional `reasoning_effort` (`ReasoningEffort` enum), `effective_cutoff`, and `ModelsConfig.latest_effective_cutoff()`. `models.yaml` keeps TODO placeholders; real slugs, prices and rate limits are still owner-supplied, and `measured_effective_cutoff` stays null until probes exist.
- 2026-09-25 — P3 steps 2-3: `features/renderer.py` (rule set N: `sig2`, `t-Nd`, `Q-n`, sector z/percentile, `scrub_text` for news) and `agents/partitions.py` (one builder per agent, `evidence_id` on every row, `available_at <= as_of` re-checked). `AliasKind.PERSON` (+ `Alias.canonical`) masks Form 4 filers to `EXEC_<8 hex>` = SHA-256(name ‖ CIK). Routine/opportunistic tag (Cohen-Malloy-Pomorski) is computed in the partitioner from visible Form 4 history and returns `UNKNOWN`, never `OPPORTUNISTIC`, when history is under three years; the P1 backfill is two years, so signals stay 0 until deeper Form 4 history is loaded. Colloquial short names ("Zephyr") are not derived; add them to `config/aliases.yaml`.
- 2026-09-25 — P3 step 4: `agents/base.py` = `run_agent_call` over a sync `ChatClient` protocol (transport lands in steps 5-6). Builds `AgentVerdict`/`RedTeamVerdict`, computes `valid` (BACKTEST and `as_of.date() <= served.effective_cutoff + 60d` -> False; live/ablation True), rejects citations not in `Partition.evidence`, one repair then discard, cost per call via `ServedModel.call_cost`. Unknown `response.model` raises `UnknownServedModelError` (run `PARTIAL`). Not yet built: request payload, cache, token bucket, backoff/DLQ (steps 5-6).
- **Open conflicts for owner (step 4 directive):** (a) directive cache key uses the *served* model, unknowable before the call, and differs from the adopted `(config_hash, available_at, payload_hash)` scheme; not implemented until decided. (b) Spec §3.1 shows three `p_outperform_{5,21,63}` fields, but `contracts/models.py` still has one `p_outperform` + `horizon_days`; spec wins per CLAUDE.md, so a contract change is needed before prompts (step 7). (c) Directive imports the schema from `contracts.schemas`; it is JSON files in `contracts/schemas/`.
- 2026-09-25 — P3 contract patch: `AgentVerdictLLM` now has `p_outperform_5/21/63` (spec §3.1); `p_outperform`/`horizon_days` removed from it, schema regenerated. `Horizon` enum still lacks `D5` (spec has it; `CommitteeDecision`/`OutcomeRecord`/red team still use 21/63 only), to be settled in P4 when per-horizon pooling is built.
- 2026-09-25 — P3 step 5: transport lives in `agents/llm/` (per plan; not in `base.py`): `openrouter.py` (payload, `models` fallback array, retries <=3 with exponential backoff + jitter, `Retry-After`, 429/5xx/network/upstream-in-200 retried, other errors raise `LLMRequestError`), `ratelimit.py` (Redis Lua token bucket, rpm and tpm per primary slug, charged every attempt), `store.py` (Redis cache, DLQ list `llm_dlq`). `run_agent_call` gained `cache`/`dlq`: retries exhausted -> DLQ + `DiscardReason.DEAD_LETTERED` (caller marks run `PARTIAL`).
- 2026-09-25 — P3 cache (owner directive): key = `sha256(agent, prompt_version, requested primary slug, partition hash)`, prefix `llm_eval:`. Stored value is the LLM output + `model_served`; envelope (`run_id`, `as_of`) and `valid` are recomputed on a hit, because partition text carries only relative days so `as_of` is not in the key. Known gaps vs the earlier `config_hash` plan: schema, fallback list and request params are not in the key; changing them without bumping `prompt_version` serves stale entries. Payload schema comes from `contracts.schema_export.response_format`, identical to the JSON file (freshness enforced by `--check`).
- P3 unverified locally: `tests/agents/test_llm_redis.py` (Lua bucket, cache, DLQ) skipped here (no Redis, Docker daemon down). Run with `REQUIRE_SERVICES=1 TEST_REDIS_URL=...` before trusting the bucket.

## Open issues (P1–P2)

- **Fixtures are synthetic.** This environment could not reach SEC/Alpaca/Alpha Vantage. Record real responses (`python -m ingest.backfill ... --record tests/fixtures/http`) and re-run the parser tests before trusting production ingestion.
- **News timestamps unverified (§18.1).** No real recordings, so the reliability check could not be done. Alpaca returns only the latest text (backfill items become visible at `updated_at`); Alpha Vantage has no zone and no revision stamp. Run `ingest.news.timestamp_audit` on real recordings before picking `NEWS_PROVIDER`.
- **TimescaleDB not tested locally** (no package here). Hypertable creation is covered only by CI (`REQUIRE_TIMESCALE=1`).
- **Seeding survivorship:** SEC's `company_tickers_exchange.json` lists current names only, so names delisted before the first backfill are missing. Snapshots are survivorship-safe from P1 on.
- Form 4: only the non-derivative table is ingested; 4/A amendments are separate rows (not merged with the original).
- Early-close sessions are treated as 16:00 ET closes (only delays availability); exchange holidays are not modelled for month-end dates.
- P2 covariance is diagonal; correlated sector portfolios can realize volatility above the modelled 12% target. Adopt a point-in-time covariance estimator before treating the target as a forecast.
- P1 stores raw bars but no point-in-time corporate-action feed. `fs_v1` therefore cannot distinguish splits from returns; add such a feed before using split-affected windows in production evaluation.

## Open questions (seeded from §18)

1. **News source:** Alpaca News vs Alpha Vantage `NEWS_SENTIMENT` (`NEWS_PROVIDER` env var). Pick on reliable `published_at` + revisions.
2. **Sector set** for the universe — freeze before P8.
3. **Primary horizon:** 21 vs 63 trading days — log both, pick before P8.
4. **Short side:** phase 2 only, after long-only results exist.
5. **Model slugs + cutoffs** in `config/models.yaml` are placeholders (owner to fill). Production startup fails until filled.
6. **SPEC-GAP §8.1/§7:** no defaults for sizing `k`, dispersion `λ`, or pooling `w_floor`. Placeholders are in `risk.yaml`/`pipeline.yaml`, to be settled before P4. Is σ_20d daily or annualized?

## P3 step 6 (2026-09-25)

- Cache key is now `sha256(agent, prompt_version, requested primary slug, config_hash, partition hash)` with a unit separator between parts (`agents/base.py::cache_key`, not `store.py`, where the function already lives). `config_hash` is a required argument of `run_agent_call` when a cache is passed; the caller (P5) must supply the run's config hash. This supersedes the "known gaps" note in the step 5 cache entry.
- `RunBudget` (thread-safe counter) and `BudgetExceededError` in `agents/base.py`. The limit is the existing `pipeline.yaml` `budgets.run_budget_usd` (there is no `settings.yaml`). Cost is booked, then `>=` the limit raises. A call made after that is never sent. The job goes to the DLQ with `error: budget_exceeded`. Owner directive named `PARTIAL_BUDGET_EXCEEDED`, but `RunStatus` has only `PARTIAL` (state machine and schema tests), so the caller marks `PARTIAL`; a new status is a contract change and is not made here. `DiscardReason.UNPROVIDED_EVIDENCE` was kept (directive called it `INVALID_CITATIONS`).
- `tests/agents/test_p3_e2e.py`: 5 cases over mocked OpenRouter (respx). Redis-backed tests remain skipped locally. `make gate-P3` is still a stub and P3 is not marked done.

## P3 step 7 (2026-09-25)

- `prompts/agents/<agent>.py` (five voters + red team) share `_common.py`: universal rules (JSON only, no dates/tickers/names/currency, 1-5 citations by `evidence_id`, "NA" is not filled in) and the three-horizon output block. `prompt_version` = `v1-<8 hex sha256(system prompt)>`, so any wording change changes the cache key. Directive said `prompts/agents/*.py` generators; this supersedes the plan's `prompts/<agent>/v1.md` files.
- Red-team severity is `low | med | high` (`BearSeverity`); the directive wrote `medium`, which the contract rejects. Caught by the runner test. `Horizon` still has no `D5`, so the red team's `horizon_days` is 21 or 63.
- `agents/runner.py`: `run_agents(...)` fans out (entity x 5 voters) plus the red team for `red_team_ids` (caller-supplied; the committee picks them in P4). Blocking `run_agent_call`s run in threads (`asyncio.to_thread`, `concurrency` bound); the Redis bucket in the client paces them. Verdicts go to a caller-supplied `sink`; universe loading, feature reads and the `agent_verdicts` write stay with the caller (import-linter, invariant 2), to be wired in P5. Voters use the FAST tier, the red team STRONG. Task key `(run_id, stage, security_id)`; the caller passes `completed` keys, only those skip. Discarded/dead-lettered/aborted tasks re-run on resume. `BudgetExceededError`, `UsageUnavailableError` and `UnknownServedModelError` stop dispatch: tasks not yet called go to the DLQ (`budget_exceeded` / `run_aborted`) and the run returns `RunStatus.PARTIAL`. A call that only fails repair is discarded and does not make the run PARTIAL; a dead-lettered one does.
- `universe/snapshot.py`: directive named `universe/snapshots.py`, which does not exist. Added `colloquial_short_name`, `alias_coverage_gaps`, `assert_alias_coverage` (short name = first word of the legal-suffix-stripped name; must be a brand alias for that CIK). It is not wired into `build_snapshot`: `config/aliases.yaml` is still empty, so enforcing it would fail every real snapshot. **Owner:** populate `aliases.yaml` for the included universe, then call `assert_alias_coverage` in the P5 run start.
- `make gate-P3` implemented (lint + contracts/config/features/universe/agents with `REQUIRE_SERVICES=1` + `schema_export --check`).
- Verified locally with Redis 7 and Postgres 16 (plain image, not TimescaleDB, pull failed) in Docker: all of those suites pass with zero skips; `test_llm_redis.py` is stable over 3 runs. `test_bucket_grants_burst_then_waits` was flawed (600 round trips let real refill hide the drain); now drains in one grant.
- **Not verified:** `make` is not installed here, so the target itself was not run, only its commands. `make lint` also fails on the untracked `.agents/` skill scripts (not part of this change). P3 is therefore not marked done. Untracked `.agents/`, `AGENTS.md`, `docs/audits/` were left alone.

## P4 step 1 (2026-09-25)

- `Horizon.D5 = 5` added (IntEnum, so the value is `5`, not the directive's `"5d"`); `RedTeamVerdictLLM` schema regenerated, `--check` passes. New contracts `HorizonPool`, `PooledForecast` in `contracts/models.py`. `pipeline.yaml` `horizons` is now `[5, 21, 63]`.
- Config: no `config/committee.yaml` (spec §15 lists none); parameters live in the existing `pipeline.yaml` `committee:` block (`weight_floor` 0.02, `shrinkage` T_w=30/τ=26, `overlap_weights`, `stacker` γα=1/γβ=10/T_s=8) and `risk.yaml` `bear_multiplier.med` 0.75.
- **Owner conflict, resolved to spec:** directive overlap weights 1/4.2 and 1/12.6 vs spec §7.1 (1, 1/4, ~1/13). CLAUDE.md says spec wins, so config holds 1.0 / 0.25 / 0.0769. Say so if you want the directive values.
- **Deferred to step 3 (sizing):** `k` 0.5, `λ_disp` 0.0 and the `p − b_h` edge change P2 baseline behaviour, so `risk.yaml` `k` (0.02) and `dispersion_lambda` (0.5) are untouched. Volatility is already annualized (P2).
- `committee/pooling.py`: floor is enforced by pinning floored agents and rescaling the rest, because "floor then renormalize" can put a weight back under 0.02 and break the propagation test.
- Not run: `make` (absent), `tests/store` (needs Postgres); 16 skips are service-gated.

## P4 step 2 (2026-09-25)

- `committee/stacker.py`: `fit_stacker` (pass-through alpha 0/beta 1 while independent periods < T_s = 8; otherwise ridge-logistic, alpha → logit(weighted trailing base rate, clipped to `logit_clip`), beta → 1, objective as directive: weighted NLL / M + γα/2 + γβ/2) and `compute_effective_agents` (Pearson error matrix, ρ̄, N_eff, `AlgorithmicMonocultureWarning` log at N_eff < 2). New contracts `CalibrationFit`, `ErrorCorrelation` (+ strategies).
- **Deviation from directive:** no `scipy.optimize`. numpy/scipy are not dependencies; the objective is strictly convex in 2 parameters, so a damped Newton solve in pure Python is used (test asserts gradient ≈ 0 at the solution). Adding scipy is one line if you prefer it.
- ρ̄ is computed on errors `p − y`, which share `−y`; independent-forecast agents therefore still show high ρ̄ (spec §7.3 as written). Read N_eff against that floor, not against A. Raise if you want a different definition.
- Undefined cases raise `ValueError` (constant-error agent, ρ̄ ≤ −1/(A−1)) instead of guessing.
- Not persisted: `calibration_models` write and evaluator wiring belong to later steps; the module takes resolved observations and `independent_periods` as inputs.
- Verified: tests/committee, tests/contracts, tests/config pass; ruff, format, mypy --strict, schema_export --check, lint-imports clean. `make` absent, `make gate-P4` still a stub.

## P4 step 3 (2026-09-25)

- `risk/sizing.py::size_committee_book` (new; `size_book`/P2 baseline path untouched, only the sector-cap loop was extracted to `_cap_sectors`). Mode is `fit.active`: rank (top `rank_top_m`=8 by pooled logit at `fit.horizon`, inverse-vol, x S_bear) or calibrated (`p_cal - b >= edge_hurdle` 0.04, `committee_k` x edge / sigma x (1 - `committee_dispersion_lambda` x dispersion) x S_bear). Then cap 8%, sector 30%, 1% floor, vol target + gross <= 1, final floor pass (vol scaling can cross 1%; dropped weight is never redistributed). Residual is cash (`ProposedBook.cash_weight`).
- `risk.yaml`/`RiskConfig`: new `rank_top_m`, `edge_hurdle`, `committee_k` 0.5, `committee_dispersion_lambda` 0.0 (defaults in the model so the baseline config and P2 tests are unchanged). Baseline keeps k 0.02, lambda 0.5, `entry_threshold` and the `p - 0.5` edge.
- **Deviations from directive:** no `SizeEngine` class or numpy (function API in the repo's style, pure Python); the test suite is `tests/risk/test_committee_sizing.py` (property fuzz for bounds, vol target, residual cash). Directive floors before vol scaling and never re-checks; a final floor pass is kept so the "< 1% is 0" invariant holds after scaling.
- **Open (spec §8.1):** the quant baseline is meant to run through the same mode; it still uses the P2 path. Calibrated mode is fed by the caller's `bear` map (red team runs on the top 15, §7.4); wiring is P5.

## P4 step 4 (2026-09-25)

- `agents/cio.py` + `prompts/agents/cio.py` (not in `PROMPTS`; that registry is the runner's voter/red-team set). `apply_cio_decision` (pure): vetoed names are removed from the `ProposedBook`, so their weight becomes residual cash; other weights are bit-identical. `run_cio`: one strong-tier call, one repair, `CioTierError` if `response.model` is outside the strong tier, `CioDecision.model_served` = served slug (invariant 7). Over-budget vetoes (`floor(M x cio_veto_budget)`, so 1 of 8; floor confirmed as the plan's open question default) raise `CioMiscalibrationAlert` from the pure function; `run_cio` catches it, pushes a DLQ record, discards all vetoes and returns the proposed book plus the logged decision.
- **Directive deviations, spec wins:** (a) conformal act-vs-escalate is *not* built: spec §17 defers it ("needs a held-out calibration set"), and §8.2 has the CIO see the whole book. (b) No new contracts: `CioDecisionLLM`/`CioDecision` already exist per §8.2 with `approve|veto|flag_for_review`, `reason`, `rationale`; the directive's `KEEP|VETO`, `portfolio_commentary` and `CandidateAllocation` were not adopted. Schema `--check` unchanged. (c) Tests use `apply_cio_decision` / `run_cio` with a fake `ChatClient`, not `run_cio_governance(mock_response=...)`. (d) Model tier comes from `config/models.yaml` strong tier (slugs are still TODO placeholders), not a hard-coded Claude slug.
- Not wired: persistence of `CioDecision`, task key `(run_id, cio, *)` idempotency and calling `run_cio` belong to P5 orchestration. `flag_for_review` keeps the name and is only logged.
- Verified: tests/agents/test_cio.py (12), agents/contracts/config/committee/risk suites, ruff, mypy --strict, lint-imports, schema_export --check. `make gate-P4` still a stub (step 5).

## P4 step 5 (2026-09-25)

- `tests/committee/test_p4_e2e.py`: 5 cases (rank mode, calibrated mode, single CIO veto -> cash, veto-budget breach -> alert + DLQ + untouched book, red-team severity halves weight) over real pooling, stacker, `size_committee_book`, `apply_cio_decision`/`run_cio` with a fake strong-tier client. The directive's fixed numbers (equity 0.64, cash 0.36) are not hard-coded; assertions are relational (cash rises by exactly the vetoed weight, survivors bit-identical).
- `make gate-P4` implemented in the P3 style (`lint`, pytest over the six suites with `REQUIRE_SERVICES=1`, `schema_export --check`). Directive's separate `ruff`/`mypy` lines are covered by `make lint`.
- Verified: ruff, format, mypy --strict (project packages), lint-imports, schema_export --check clean; pytest over the six suites passes with 5 skips (Redis-gated).
- **Not verified / why P4 is not marked Done:** `make` is absent, so the target itself was not run; Redis is unavailable so 5 agent tests skip (the gate sets `REQUIRE_SERVICES=1` and would fail here); `ruff check .` fails only on untracked `.agents/` scripts (not part of this change).
