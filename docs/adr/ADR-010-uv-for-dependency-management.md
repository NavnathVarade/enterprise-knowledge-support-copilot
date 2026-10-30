# ADR-010: uv and a single lock file for dependency management

## Context
Dependencies were installed with pip: a hand-regenerated `requirements/constraints.txt` (exact versions, **no hashes**) for production, and `pip install -e ".[dev]"` for development. Two sources of truth could drift, the pins carried no integrity hashes, and dev dependencies that tests actually needed (`pyyaml`, `jsonschema`) were only present by accident of the system Python.

## Decision
Use uv. `pyproject.toml` declares dependencies (runtime in `[project]`, tooling in the `dev` dependency group); `uv.lock` is the only lock file, universal across platforms and recording a sha256 for every artifact. Python is pinned by `.python-version` (3.12, the image's version). CI and the Docker build install with `uv sync --frozen` (`--no-dev --no-install-project` in the image), so a stale lock fails the build instead of resolving silently. `[tool.uv] required-environments` makes the lock prove wheel availability for Linux amd64 and arm64. Tools run through `uv run` / `uvx`; vulnerability scanning exports the production set with `uv export --frozen --no-dev` and runs pip-audit on it.

## Alternatives considered
- Keep pip + pip-tools (`--generate-hashes`): works, but needs a separate compile step and venv management; uv does both and is faster.
- Poetry/PDM: comparable; uv's `--frozen` workflow and the PEP 735 dependency group fit the Dockerfile and CI with the least ceremony.

## Consequences
- Hashes are now recorded and verified (closes the Phase 30/32 "no hashes" gap for locked installs).
- Migrating exposed two undeclared test dependencies and a hidden reliance on `python -m pytest` putting the repo root on `sys.path` (now `pythonpath = ["."]` in pytest config). Scripts that import project packages must run as modules (`uv run python -m scripts.x`).
- The uv binary is copied from a pinned official image into the builder stage only; it is not in the runtime image.
- Playwright's browser download is separate from the lock (`uv run playwright install chromium`, or `EKC_CHROMIUM_PATH`).
- Verified locally: lock resolution, `uv sync --frozen`, the full test suite and browser tests through `uv run`, a production-only environment built with the Dockerfile's exact sync command, pip-audit on the export. **Not verified:** a real `docker build`, the updated GitHub workflows on GitHub.

## Status
Accepted — Phase 49.
