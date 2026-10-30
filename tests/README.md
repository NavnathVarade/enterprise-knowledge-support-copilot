# Tests

| Directory | Needs | What it proves |
|---|---|---|
| `tests/unit` | nothing external (some skip without `kustomize`, `tofu`, `actionlint`, `nginx`) | pure logic, settings validation, manifests/Helm/Terraform/workflow policy, architecture fitness, docs integrity |
| `tests/integration` | PostgreSQL 16 + pgvector >= 0.8, Redis | repositories, retrieval with ACLs, API over real HTTP, ingestion pipeline, audit chain, backup/restore, gateway/web nginx configs |
| `tests/e2e` | the above + nginx, Chromium (Playwright), built frontend | the browser journey through the real gateway and API, with stand-in Kafka/S3/LLM (see `docs/testing/PHASE_42_E2E.md`) |
| `apps/web` (`npm test`) | Node 22 | React component and client tests |

## Environment
Install with `uv sync --frozen`, run everything through `uv run` (for example `uv run pytest`).

Copy the values from `.env.test` (committed, **test-only** keys and a localhost database; these are accepted only because `ENVIRONMENT=test`). Create a disposable database named `ekc_test` with `CREATE EXTENSION vector`, run `alembic upgrade head`, then `pytest`. Test and benchmark databases must have `test`/`bench`/`load` in the name; destructive helpers refuse anything else.

Settings tests start from an empty environment and ignore any `.env` (`EKC_ENV_FILE` is set empty by `tests/conftest.py`); to run the integration suite from a file instead of exported variables set `EKC_ENV_FILE=.env.test`. Environment-mode coverage: `tests/unit/test_environment_modes.py` (policy), `tests/integration/test_local_event_dispatch.py` (dev dispatcher vs real Postgres), `tests/integration/test_environment_runtime.py` (startup/readiness/shutdown with Kafka client classes tripwired).

Browser tests use Playwright's Chromium (`uv run playwright install chromium`); on an offline machine point `EKC_CHROMIUM_PATH` at an installed Chromium binary instead.

Optional tool paths for policy tests: `KUSTOMIZE_BIN`, `TOFU_BIN`, `ACTIONLINT_BIN`.

## Rules
- Do not run two things against the same test database at once. The e2e harness runs a relay and consumer on it, which will pick up other tests' outbox events; its fixtures are module-scoped so they stop before other modules run, but a manually started harness must be stopped first.
- Markers: `unit`, `integration`, `security`, `contract`, `e2e` (see `pyproject.toml`).
- The suite proves behaviour against the real database and Redis. It does **not** prove behaviour against Kafka, S3, an LLM provider or a cluster; docs say so wherever it matters.
