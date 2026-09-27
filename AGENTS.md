# AGENTS.md — Asymmetric Committee Fund

The authoritative spec is `docs/asymmetric-committee-blueprint.md`. When code and spec disagree, the spec wins. If the spec is wrong or ambiguous, stop and propose a spec edit. Do not silently diverge.

## Current state

Track phase status in `docs/PROGRESS.md` (phase, gate status, open issues, decisions). Read it at the start of every session and update it at the end.

## Stack

Python 3.12, uv, Pydantic v2, FastAPI, Celery + Redis, TimescaleDB (Postgres 16), SQLAlchemy 2 core + Alembic, httpx, pytest + hypothesis, ruff + mypy --strict, Next.js 15 (dashboard only), Docker Compose.

## Invariants (never violate — each has a test)

1. **Contracts:** every inter-stage message is a model in `contracts/`. Use `extra="forbid"` and Enums, never bare strings, for categorical fields. Do not define message shapes anywhere else.
2. **Point-in-time reads:** agent, feature, gate, committee and risk code reads the DB **only** through `store/as_of.py`. No raw SQL against fact tables outside `store/`.
3. **No look-ahead:** nothing with `available_at > as_of` may reach a feature, prompt or decision. Form 4 `available_at` = filing acceptance time. Fundamentals are stored as-filed and never overwritten.
4. **Anonymity:** LLM prompts never contain a ticker, company name, CIK or named executive. The partitioner masks them, and a test asserts it.
5. **Commit before scoring:** the evaluator refuses to compute outcomes for a run without a `decision_commitments` row.
6. **Deterministic sizing:** LLMs never output weights. Weights come from `risk/` only.
7. **Served model is logged:** every LLM verdict stores `response.model`, not the requested model.
8. **Run-scoped idempotency:** task keys are `(run_id, stage, security_id)`. Only `COMPLETED` short-circuits.

## Working rules

- Start each phase in plan mode. Write the plan to `docs/plans/P<n>.md` before coding.
- Begin implementation only when the owner's entire response is exactly `APPROVED`, apart from surrounding whitespace. Corrections, qualifications, conditions, questions, or additional requests require an in-place plan revision and resubmission.
- Revise plans in place; reconcile removed or renamed concepts throughout the full document and remove superseded text before resubmitting.
- For source changes, prefer Edit/Write. Use a scratch script for complex scripted transformations, then validate every touched file.
- Keep goal text task-specific: omit standing project, Git, scope, and safety instructions already supplied by repository guidance. Preflight goal text to 3,500 characters or fewer.
- Tests first for every invariant touched in the phase.
- Never call live paid APIs in tests. Use recorded fixtures (`tests/fixtures/`) and respx/httpx mocks.
- Secrets come only from `.env` (git-ignored). Keep `.env.example` current.
- Only paper-trading endpoints exist. Any code path to a live Alpaca endpoint is a bug.
- SEC EDGAR: global limiter ≤ 8 req/s, User-Agent from `SEC_USER_AGENT` env var.
- Small commits, conventional messages (`feat(ingest): …`). One phase = one branch `phase/P<n>` → PR to main.
- Done means `python scripts/verify_local.py P<n>` passes. Do not mark a phase done otherwise.

## Commands

- `make up` / `make down` — compose stack
- `make test` — full suite
- `make lint` — ruff + mypy
- `python scripts/verify_local.py P<n>` — authoritative local phase verification (services, lint, schema freshness, and phase checks). `make gate-P<n>` is a compatibility alias.
