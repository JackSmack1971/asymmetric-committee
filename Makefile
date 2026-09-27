.PHONY: up down test lint fmt migrate backfill-smoke $(addprefix gate-P,0 1 2 3 4 5 6 7 8)

up:
	docker compose up -d --build --wait

down:
	docker compose down

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy .
	uv run lint-imports

fmt:
	uv run ruff format .
	uv run ruff check --fix .

# Compatibility aliases. scripts/verify_local.py owns phase mapping, services, env and results.
gate-P0:
	uv run python scripts/verify_local.py P0

gate-P1:
	uv run python scripts/verify_local.py P1

migrate:
	uv run python -m store.migrate

# The gate's smoke run as a CLI against $$DATABASE_URL (after `make migrate`).
backfill-smoke:
	NEWS_PROVIDER=alpaca uv run python -m ingest.backfill --days 90 --end 2024-06-28 \
	    --tickers ALFA,BRVO,CHRL,DLTA,ECHO --universe tests/fixtures/universe_smoke.yaml \
	    --replay tests/fixtures/http

gate-P2:
	uv run python scripts/verify_local.py P2

# P3: lint (incl. import-linter) + contracts/config/features/universe + agents (partitions, leak
# tests, prompts, client, Redis bucket/cache/DLQ, runner) + exported LLM schemas are current.
# Needs Redis (TEST_REDIS_URL or a local binary); REQUIRE_SERVICES=1 turns a skip into a failure.
gate-P3:
	uv run python scripts/verify_local.py P3

# P4: lint (ruff, mypy --strict, import-linter) + contracts/config/features/agents (incl. CIO) +
# committee (pooling, stacker, e2e) + risk sizing + exported LLM schemas are current.
# Agents tests need Redis (TEST_REDIS_URL or a local binary); REQUIRE_SERVICES=1 fails on a skip.
gate-P4:
	uv run python scripts/verify_local.py P4

# P5: lint (ruff, mypy --strict, import-linter) + the whole suite, because P5 sits on P1-P4, with
# Postgres and Redis *required* (REQUIRE_SERVICES=1: a missing service fails, never skips) +
# schema freshness. The suite includes the anchoring, Celery, kill-switch, execution and end-to-end
# tests; all of them use local stand-ins (respx, a MockTransport Alpaca, a bare git repo), so the
# gate makes no external call and needs no credentials. TimescaleDB is required so the hypertable
# test must actually run. Services: TEST_DATABASE_URL, TEST_REDIS_URL (or local binaries).
gate-P5:
	uv run python scripts/verify_local.py P5

gate-P6:
	uv run python scripts/verify_local.py P6

gate-P7:
	uv run python scripts/verify_local.py P7

gate-P8:
	uv run python scripts/verify_local.py P8
