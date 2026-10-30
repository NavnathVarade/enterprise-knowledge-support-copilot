.PHONY: install lock lock-check lint format typecheck test test-unit test-integration check compose-up compose-down

install:
	uv sync --frozen

lock:
	uv lock

lock-check:
	uv lock --check

lint:
	uv run ruff check .

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

typecheck:
	uv run mypy packages apps/api/app data scripts rag

test-unit:
	uv run pytest tests/unit -v

test-integration:
	uv run pytest tests/integration -v

test: test-unit test-integration

# Runs exactly what CI runs, in order, so a local failure here means CI would fail too.
check: lock-check lint format-check typecheck test

compose-up:
	docker compose -f infrastructure/docker/docker-compose.yml up -d --build

compose-down:
	docker compose -f infrastructure/docker/docker-compose.yml down -v

.PHONY: eval-routing eval-retrieval eval-answer
eval-routing:
	uv run python -m scripts.run_eval routing --markdown
eval-retrieval:
	uv run python -m scripts.run_eval retrieval --markdown --min recall@5=0.9
eval-answer:
	uv run python -m scripts.run_eval answer --markdown
