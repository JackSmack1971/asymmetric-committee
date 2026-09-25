# Progress

Read at the start of every session; update at the end. Spec: `docs/asymmetric-committee-blueprint.md` (§17 phases).

| Phase | Status | Gate | Branch | Notes |
|---|---|---|---|---|
| P-boot Bootstrap | Done (pending PR merge) | `make lint` + `make test` + `make up` healthy | `phase/P-boot` | Skeleton, uv, compose, Makefile, CI. No business logic. |
| P0 Contracts | Done (pending PR merge) | `make gate-P0` passes (lint + tests/contracts + tests/config + schema freshness) | `phase/P0` | `contracts/` enums + models + strict LLM schemas; `config/` yaml + loader. Plan: `docs/plans/P0.md`. |
| P1 Data | Done (pending PR merge) | `make gate-P1` passes (lint + import-linter + store/ingest/universe/config tests incl. backfill smoke) | `claude/bitemporal-timescaledb-as-of-9yro0f` | Schema + Alembic, `store/as_of.py`, 4 ingestors, EDGAR limiter, freshness, universe snapshots, backfill CLI. Plan: `docs/plans/P1.md`. 2-year real backfill still to run (needs network + keys). |
| P2 Features + gate + baseline | Done (pending PR merge) | `make gate-P2` passes (lint + contracts/config + deterministic features, leak-safe gate, risk properties, 12-week smoke) | `phase/P2` | `fs_v1`; fixture gate recall: 100%. Plan: `docs/plans/P2.md`. |
| P3 Agents | Not started | `make gate-P3` (stub) | `phase/P3` | |
| P4 Committee + risk + CIO | Not started | `make gate-P4` (stub) | `phase/P4` | |
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
