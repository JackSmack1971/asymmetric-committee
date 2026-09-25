.PHONY: up down test lint fmt $(addprefix gate-P,0 1 2 3 4 5 6 7 8)

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

fmt:
	uv run ruff format .
	uv run ruff check --fix .

# Phase gates: each phase replaces its stub with the real acceptance check (§17).
gate-P0:
	@echo "gate-P0: not implemented"; exit 1

gate-P1:
	@echo "gate-P1: not implemented"; exit 1

gate-P2:
	@echo "gate-P2: not implemented"; exit 1

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

