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

# Phase gates: each phase replaces its stub with the real acceptance check (§17).
# P0: lint + contract/config tests + exported LLM schemas are current.
gate-P0: lint
	uv run pytest tests/contracts tests/config
	uv run python -m contracts.schema_export --check

# P1: lint (incl. import-linter) + point-in-time property/restatement/Form 4 tests, EDGAR limiter
# load test, parser tests, universe snapshots and the 5-ticker x 90-day backfill smoke replayed
# from tests/fixtures/http. Needs Postgres + Redis (TEST_DATABASE_URL / TEST_REDIS_URL, or local
# binaries); REQUIRE_SERVICES=1 makes a missing service fail instead of skip.
gate-P1: lint
	REQUIRE_SERVICES=1 uv run pytest tests/store tests/ingest tests/universe tests/config

migrate:
	uv run python -m store.migrate

# The gate's smoke run as a CLI against $$DATABASE_URL (after `make migrate`).
backfill-smoke:
	NEWS_PROVIDER=alpaca uv run python -m ingest.backfill --days 90 --end 2024-06-28 \
	    --tickers ALFA,BRVO,CHRL,DLTA,ECHO --universe tests/fixtures/universe_smoke.yaml \
	    --replay tests/fixtures/http

gate-P2: lint
	uv run pytest tests/contracts tests/config tests/features tests/gate tests/risk tests/evaluation
	uv run python -m contracts.schema_export --check

gate-P3:
	@echo "gate-P3: not implemented"; exit 1

gate-P4:
	@echo "gate-P4: not implemented"; exit 1

gate-P5:
	@echo "gate-P5: not implemented"; exit 1

gate-P6:
	@echo "gate-P6: not implemented"; exit 1

gate-P7:
	@echo "gate-P7: not implemented"; exit 1

gate-P8:
	@echo "gate-P8: not implemented"; exit 1
