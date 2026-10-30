# Contributing

## Ground rules (these are enforced, not aspirational)
- **No fabricated results.** A number in a document must come from a command you ran and a stored output. Anything not run is labelled `NOT RUN` / `NOT YET MEASURED`.
- **Secrets never enter the repository** (gitleaks gates CI). Credentials come from a secret manager; `.env` files are ignored.
- **No localhost between services** in deployed configuration (settings validator).
- **Tenant isolation is not optional**: retrieval uses `build_document_acl_predicate`; new queries that touch tenant data must filter on `tenant_id`.
- **Module boundaries**: a new package must be added to `LAYERS` in `scripts/architecture_check.py`; imports must not point upward or form cycles (ADR-009).

## Local workflow
First-time setup (PostgreSQL with pgvector and Redis locally; **no Docker or Kafka needed**) is in `HOW_TO_RUN.md` Part A. Environments: `.env.example` is the development template, `.env.production.example` the production one (ADR-011). Day to day:
```
uv sync --frozen                                  # creates .venv from uv.lock (Python from .python-version)
uv run ruff check --fix . && uv run ruff format .
uv run mypy packages apps/api/app data scripts rag   # strict
uv run python scripts/architecture_check.py
uv run python scripts/readiness_review.py          # needs kustomize
uv run python scripts/gen_reference_docs.py        # regenerate docs/reference/*
uv run pytest                                      # needs Postgres+pgvector and Redis, see tests/README.md
cd apps/web && npm ci && npm run lint && npm run typecheck && npm test
```
Browser tests (`tests/e2e`) additionally need nginx, Chromium (`python -m playwright install chromium`) and a built frontend; they skip when those are missing.

## Dependencies (uv)
`pyproject.toml` declares dependencies; `uv.lock` is the single, hash-recording lock file and **must be committed with any dependency change**: `uv add <pkg>` / `uv remove <pkg>` (or edit `pyproject.toml` then `uv lock`). Dev tools live in the `dev` dependency group (installed by default; images use `--no-dev`). CI and the Docker build use `--frozen`, so a stale lock fails fast. `make lock-check` runs `uv lock --check`. To upgrade one package: `uv lock --upgrade-package <pkg>`; then run the full suite and `uvx pip-audit` on `uv export --frozen --no-dev --no-emit-project --no-hashes`.

## Pull requests
Use `.github/pull_request_template.md`. A change is done when: tests cover the behaviour (a bug fix includes a test that fails without the fix), CI-equivalent checks above pass, documentation is updated in the same change (generated references are regenerated; behaviour changes update the relevant phase document), and any new limitation is stated in the docs.

## Documentation
`docs/README.md` is the index. Add a document there when you add one (a test fails otherwise). Prefer correcting a stale statement over adding a caveat elsewhere.
