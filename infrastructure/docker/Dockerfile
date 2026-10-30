# ONE image, several roles. The container command selects the role, so the API, the workers,
# the migration job and the topic-provisioning job always run exactly the same code and
# dependency set (no image drift between them):
#
#   api              (default)  uvicorn apps.api.app.main:app
#   ingestion worker            python -m packages.workers ingestion
#   outbox relay                python -m packages.workers outbox-relay
#   migrations                  alembic upgrade head
#   topic provisioning          python -m scripts.create_topics
#
# Dependencies are installed from the committed uv.lock (`uv sync --frozen --no-dev`): exact
# versions with hashes, nothing resolved at build time, and the build fails if pyproject.toml and
# uv.lock disagree. Runtime image: non-root, no compilers, no uv, no pip cache, no dev/test packages.
#
# Base image is referenced by TAG. Pinning by digest (--build-arg PYTHON_IMAGE=python@sha256:...)
# is the supply-chain hardening step; CI should pass the digest it scanned.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# hadolint ignore=DL3006
FROM ${PYTHON_IMAGE} AS builder

# uv is copied from its pinned official image into the BUILDER only; it never reaches the runtime stage.
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PYTHONDONTWRITEBYTECODE=1

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml uv.lock .python-version ./
# The application code is copied into the runtime stage and imported from /app, so only the
# third-party dependencies are installed here (--no-install-project).
RUN uv sync --frozen --no-dev --no-install-project

# hadolint ignore=DL3006
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    EKC_HEARTBEAT_FILE=/tmp/ekc-heartbeat

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ekc \
    && useradd --uid 10001 --gid ekc --shell /usr/sbin/nologin --create-home ekc

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY packages ./packages
COPY apps/api ./apps/api
COPY rag ./rag
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY scripts/__init__.py scripts/create_topics.py ./scripts/

# Provenance labels; CI passes the real values (--build-arg VERSION=... GIT_SHA=...).
ARG VERSION=dev
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="enterprise-knowledge-copilot" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD curl --fail http://127.0.0.1:8000/api/v1/health/live || exit 1

# Exec form (no shell) so SIGTERM reaches the process: uvicorn drains in-flight requests,
# workers finish the current message. Roles other than the API override CMD/args; the
# HEALTHCHECK above only applies to the API (Kubernetes uses its own probes).
# --no-proxy-headers: uvicorn must not rewrite the client address from X-Forwarded-For (that would
# let any caller choose its own identity). The app applies HARDENING_TRUSTED_PROXY_HOPS itself.
# --timeout-graceful-shutdown 25: on SIGTERM uvicorn stops accepting, lets in-flight requests finish
# for up to 25 s, then exits. Docker/Kubernetes stop grace must be LONGER (Compose: 30 s).
STOPSIGNAL SIGTERM
ENTRYPOINT ["uvicorn"]
CMD ["apps.api.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-server-header", "--timeout-graceful-shutdown", "25"]
