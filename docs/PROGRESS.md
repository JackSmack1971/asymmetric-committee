# Progress

Read at the start of every session; update at the end. Spec: `docs/asymmetric-committee-blueprint.md` (§17 phases).

| Phase | Status | Gate | Branch | Notes |
|---|---|---|---|---|
| P-boot Bootstrap | Done (pending PR merge) | `make lint` + `make test` + `make up` healthy | `phase/P-boot` | Skeleton, uv, compose, Makefile, CI. No business logic. |
| P0 Contracts | Done (pending PR merge) | `make gate-P0` passes (lint + tests/contracts + tests/config + schema freshness) | `phase/P0` | `contracts/` enums + models + strict LLM schemas; `config/` yaml + loader. Plan: `docs/plans/P0.md`. |
| P1 Data | Not started | `make gate-P1` (stub) | `phase/P1` | |
| P2 Features + gate + baseline | Not started | `make gate-P2` (stub) | `phase/P2` | |
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

## Open questions (seeded from §18)

1. **News source:** Alpaca News vs Alpha Vantage `NEWS_SENTIMENT` (`NEWS_PROVIDER` env var). Pick on reliable `published_at` + revisions.
2. **Sector set** for the universe — freeze before P8.
3. **Primary horizon:** 21 vs 63 trading days — log both, pick before P8.
4. **Short side:** phase 2 only, after long-only results exist.
5. **Model slugs + cutoffs** in `config/models.yaml` are placeholders (owner to fill). Production startup fails until filled.
6. **SPEC-GAP §8.1/§7:** no defaults for sizing `k`, dispersion `λ`, or pooling `w_floor`. Placeholders are in `risk.yaml`/`pipeline.yaml`, to be settled before P4. Is σ_20d daily or annualized?
