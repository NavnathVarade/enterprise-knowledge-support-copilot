"""
Centralized, typed application configuration.

Design rules enforced here (see docs/architecture/PHASE_0_ARCHITECTURE.md and
docs/adr/ADR-011-environment-aware-configuration.md):
  - No component reads os.environ directly outside this module.
  - ENVIRONMENT selects the operating mode. It defaults to `production` so a forgotten
    variable can only ever make the process STRICTER, never silently run in dev mode.
  - Precedence, highest first: explicit process environment variables, then the `.env`
    file (path overridable with EKC_ENV_FILE; empty = no file), then field defaults.
  - An empty variable (`FOO=`) counts as unset, never as a configured empty endpoint.
  - Development: Kafka is off unless KAFKA_ENABLED=true; Redis defaults to a documented
    localhost URL; uploaded originals may live on the local disk.
  - Staging/production: Kafka is mandatory, every dependency endpoint must be set
    explicitly (no localhost default), and no development-only substitute is accepted.
  - Configuration is validated eagerly on process startup (get_settings() is called once
    in each entrypoint), so a misconfigured deployment fails at boot, not on first
    request. Errors name the setting, never its value (values can embed passwords).
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import (
    Field,
    PostgresDsn,
    RedisDsn,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# A denylist of hosts, not a bind address.
_LOCALHOST_MARKERS = ("localhost", "127.0.0.1", "0.0.0.0")  # noqa: S104  # nosec B104
_ASYMMETRIC_JWT_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)


_DEFAULT_DEV_REDIS_URL = "redis://localhost:6379/0"
_DEFAULT_DEV_OBJECT_DIR = ".local-data/object-store"
# Kubernetes / cluster-internal DNS suffixes: a development process cannot resolve these, so
# seeing one means a production `.env` was copied over.
_CLUSTER_HOST_MARKERS = (".svc.cluster.local", ".svc:", ".cluster.local")


class Environment(StrEnum):
    development = "development"
    staging = "staging"
    production = "production"
    test = "test"

    @property
    def is_hardened(self) -> bool:
        """Staging and production: strict mode, no development-only substitutes."""
        return self in (Environment.staging, Environment.production)


class SettingsLoadError(RuntimeError):
    """Configuration is invalid. The message lists WHICH settings are wrong and why, but never
    their values: connection strings routinely contain passwords."""


def to_sync_database_url(url: str) -> str:
    """The same database as a psycopg (synchronous) SQLAlchemy URL, for Alembic.

    asyncpg takes `?ssl=require`; psycopg/libpq only understand `sslmode`. Production settings
    REQUIRE the `ssl` form, so without this translation `alembic upgrade head` would fail in
    exactly the environments where TLS is enforced. Everything else (credentials, host, other
    query parameters) is passed through byte-for-byte."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1))
    query = [("sslmode" if key == "ssl" else key, value) for key, value in parse_qsl(parts.query)]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _dotenv_path() -> str | None:
    """`.env` in the working directory unless EKC_ENV_FILE says otherwise ("" disables it).
    Read on every instantiation (not at import) so tests and tools can redirect it."""
    return os.environ.get("EKC_ENV_FILE", ".env") or None


class _EnvSettings(BaseSettings):
    """Base for every settings group: empty variables are unset, extras are ignored, and the
    `.env` file is a source (below real environment variables in precedence)."""

    model_config = SettingsConfigDict(env_ignore_empty=True, extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        path = _dotenv_path()
        if path:
            sources.append(
                DotEnvSettingsSource(settings_cls, env_file=path, env_file_encoding="utf-8")
            )
        sources.append(file_secret_settings)
        return tuple(sources)


class DatabaseSettings(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="DATABASE_")

    url: PostgresDsn = Field(
        ...,
        description="Async SQLAlchemy DSN, e.g. postgresql+asyncpg://user:pass@postgres:5432/db",
    )
    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=5, ge=0, le=100)
    pool_timeout_seconds: float = Field(default=10.0, gt=0)
    statement_timeout_ms: int = Field(default=15_000, gt=0)
    connect_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        description="Bound on establishing a connection (asyncpg's own default is 60 s).",
    )
    read_only_role_url: PostgresDsn | None = Field(
        default=None,
        description="Read-only DSN used exclusively by the SQL analytics engine (Section 40).",
    )


class RedisSettings(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="REDIS_")

    url: RedisDsn = Field(
        default=RedisDsn(_DEFAULT_DEV_REDIS_URL),
        description=(
            "e.g. rediss://redis:6379/0. The localhost default exists for development only: "
            "staging/production reject a Settings object whose REDIS_URL was not set explicitly."
        ),
    )
    socket_timeout_seconds: float = Field(default=2.0, gt=0)
    default_ttl_seconds: int = Field(default=300, gt=0)


class KafkaSettings(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="KAFKA_")

    enabled: bool | None = Field(
        default=None,
        description=(
            "Master switch. Unset => derived from ENVIRONMENT: on for staging/production, off "
            "for development/test. Staging/production reject an explicit false."
        ),
    )
    bootstrap_servers: str | None = Field(
        default=None,
        description="Comma-separated broker list, e.g. kafka-0:9092,kafka-1:9092. "
        "Required whenever Kafka is enabled; ignored (never resolved) when it is disabled.",
    )
    security_protocol: str = Field(default="SASL_SSL")
    consumer_group_prefix: str = Field(default="ekc")
    max_poll_records: int = Field(default=100, ge=1)
    producer_acks: str = Field(default="all")
    producer_enable_idempotence: bool = Field(default=True)
    sasl_mechanism: str | None = Field(
        default=None, description="e.g. SCRAM-SHA-512, PLAIN. Required for SASL_* protocols."
    )
    sasl_username: str | None = None
    sasl_password: SecretStr | None = None
    ssl_ca_location: str | None = Field(
        default=None, description="CA bundle path for private broker CAs (SSL/SASL_SSL)."
    )

    def require_bootstrap_servers(self) -> str:
        """The broker list, or a clear error. Clients are only ever built through this."""
        if not self.bootstrap_servers or not self.bootstrap_servers.strip():
            raise SettingsLoadError(
                "KAFKA_BOOTSTRAP_SERVERS is required when Kafka is enabled (KAFKA_ENABLED)."
            )
        return self.bootstrap_servers.strip()


class EventSettings(_EnvSettings):
    """How `document.ingested` and friends travel from the transactional outbox to a handler.

    With Kafka (production) a relay publishes outbox rows to the broker and a consumer group
    processes them. Without Kafka (development) an in-process dispatcher reads the SAME outbox
    table and runs the handler directly. See docs/adr/ADR-011 for the exact semantics."""

    model_config = SettingsConfigDict(env_prefix="EVENTS_")

    local_dispatch: bool | None = Field(
        default=None,
        description=(
            "Run the in-process outbox dispatcher inside the API. Unset => on only in "
            "development with Kafka disabled. Forbidden in staging/production and whenever "
            "Kafka is enabled (it would process events twice)."
        ),
    )
    local_dispatch_poll_seconds: float = Field(default=1.0, gt=0)
    local_dispatch_max_attempts: int = Field(default=5, ge=1, le=50)
    local_dispatch_retry_base_seconds: float = Field(default=2.0, gt=0)


class LLMSettings(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="LLM_")

    provider: str = Field(default="anthropic")
    model: str = Field(default="claude-sonnet-4-6")
    fallback_provider: str | None = None
    fallback_model: str | None = None
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_output_tokens: int = Field(default=1024, gt=0)
    max_context_tokens: int = Field(default=12_000, gt=0)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_base_delay_seconds: float = Field(default=0.5, gt=0)
    retry_max_delay_seconds: float = Field(default=8.0, gt=0)
    api_key: SecretStr | None = Field(default=None, description="Provider API key (secret).")
    # Optional pricing for cost accounting; with no price configured, cost is reported
    # as unknown rather than 0.
    input_usd_per_million_tokens: float | None = Field(default=None, ge=0)
    output_usd_per_million_tokens: float | None = Field(default=None, ge=0)
    # The fallback model is usually priced differently. Unset => its cost is reported as
    # unknown (NULL), never silently priced at the primary model's rate.
    fallback_input_usd_per_million_tokens: float | None = Field(default=None, ge=0)
    fallback_output_usd_per_million_tokens: float | None = Field(default=None, ge=0)


class EmbeddingSettings(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="EMBEDDING_")

    provider: str = Field(default="anthropic")
    model: str = Field(default="voyage-3-large")
    dimensions: int = Field(default=1024, gt=0)
    batch_size: int = Field(default=64, gt=0)
    request_timeout_seconds: float = Field(default=20.0, gt=0)
    api_key: SecretStr | None = Field(default=None, description="Voyage API key (secret).")
    rerank_model: str = Field(default="rerank-2")
    usd_per_million_tokens: float | None = Field(
        default=None, ge=0, description="Embedding price; unset => cost recorded as unknown."
    )


class IngestionSettings(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="INGESTION_")

    max_upload_size_bytes: int = Field(default=25 * 1024 * 1024, gt=0)  # 25 MB
    allowed_mime_types: tuple[str, ...] = Field(
        default=(
            "text/plain",
            "text/markdown",
            "text/csv",
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/json",
        )
    )
    # Parser-safety limits (Phase 32). They bound memory/CPU/embedding cost per document.
    max_pdf_pages: int = Field(default=1000, gt=0)
    max_zip_entries: int = Field(default=2000, gt=0)
    max_zip_uncompressed_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    max_zip_compression_ratio: int = Field(default=100, gt=1)
    max_extracted_chars: int = Field(default=5_000_000, gt=0)


class ObjectStoreSettings(_EnvSettings):
    """S3-compatible object storage for uploaded originals (Spec §83)."""

    model_config = SettingsConfigDict(env_prefix="OBJECT_STORE_")

    bucket: str | None = Field(default=None, description="Bucket name; required in staging/prod.")
    local_directory: str | None = Field(
        default=None,
        description=(
            "DEVELOPMENT ONLY: keep uploaded originals on the local disk instead of S3. "
            "Rejected in staging/production. Unset in development with no bucket => "
            f"{_DEFAULT_DEV_OBJECT_DIR}."
        ),
    )
    region: str | None = None
    endpoint_url: str | None = Field(
        default=None, description="Override for S3-compatible stores (MinIO, Ceph, R2)."
    )
    key_prefix: str = Field(default="")
    sse: str | None = Field(
        default="AES256", description="Server-side encryption: AES256, aws:kms, or empty."
    )
    kms_key_id: str | None = None
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    max_attempts: int = Field(default=5, ge=1)


class SecuritySettings(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="SECURITY_")

    jwt_public_key: str = Field(..., description="PEM public key used to verify access tokens")
    jwt_private_key: str | None = Field(
        default=None,
        description=(
            "PEM private key used to SIGN access tokens. Only required by the service "
            "that issues tokens (the auth/login path); pure verification-only "
            "deployments can omit this and run with jwt_public_key alone."
        ),
    )
    jwt_algorithm: str = Field(
        default="RS256",
        description="Asymmetric algorithms only (HS*/none are rejected: algorithm confusion).",
    )
    jwt_issuer: str
    jwt_audience: str
    access_token_leeway_seconds: int = Field(default=30, ge=0)
    access_token_ttl_seconds: int = Field(default=900, gt=0)  # 15 minutes

    @field_validator("jwt_algorithm")
    @classmethod
    def _asymmetric_only(cls, value: str) -> str:
        if value not in _ASYMMETRIC_JWT_ALGORITHMS:
            raise ValueError(
                f"SECURITY_JWT_ALGORITHM must be one of {sorted(_ASYMMETRIC_JWT_ALGORITHMS)}; "
                "symmetric (HS*) and 'none' are not permitted."
            )
        return value


class ObservabilitySettings(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="OTEL_")

    service_name: str = Field(default="enterprise-knowledge-copilot")
    exporter_otlp_endpoint: str | None = None
    traces_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    log_level: str = Field(default="INFO")
    metrics_bearer_token: SecretStr | None = Field(
        default=None,
        description="Bearer token required to scrape /metrics. Unset => endpoint is disabled "
        "(404), so metrics are never exposed by accident.",
    )
    metrics_port: int = Field(default=9102, ge=1, le=65535, description="Worker metrics port.")


class RateLimitSettings(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="RATE_LIMIT_")

    tenant_queries_per_minute: int = Field(default=120, gt=0)
    user_queries_per_minute: int = Field(default=30, gt=0)
    ingestion_uploads_per_minute: int = Field(default=20, gt=0)


class HardeningSettings(_EnvSettings):
    """HTTP-surface hardening knobs (Phase 32). Defaults are the safe choice."""

    model_config = SettingsConfigDict(env_prefix="HARDENING_")

    docs_enabled: bool | None = Field(
        default=None,
        description="Serve /docs, /redoc and /openapi.json. None = only in development/test.",
    )
    require_dependency_tls: bool = Field(
        default=True,
        description=(
            "Staging/production refuse to start unless Postgres (ssl=require|verify-ca|verify-full) "
            "and Redis (rediss://) connections are TLS. Setting this false is an explicit, audited "
            "exception for a trusted private network (e.g. the single-host Compose deployment)."
        ),
    )
    max_request_body_bytes: int = Field(default=1_048_576, gt=0)
    upload_overhead_bytes: int = Field(
        default=262_144, ge=0, description="Multipart framing allowance on top of max upload size."
    )
    trusted_proxy_hops: int = Field(
        default=0, ge=0, le=5,
        description="Reverse proxies in front of the API whose X-Forwarded-For entry is "
        "trusted. 0 = ignore X-Forwarded-For entirely (the TCP peer is the client).",
    )  # fmt: skip
    login_max_failures_per_account: int = Field(default=5, gt=0)
    login_max_failures_per_ip: int = Field(default=30, gt=0)
    login_window_seconds: int = Field(default=900, gt=0)
    login_throttle_fail_open: bool = Field(
        default=True, description="If Redis is down: allow logins (True) or refuse them (False)."
    )
    hsts_max_age_seconds: int = Field(default=31_536_000, ge=0)


class SignupSettings(_EnvSettings):
    """Self-service organization signup (`POST /api/v1/auth/signup`)."""

    model_config = SettingsConfigDict(env_prefix="SIGNUP_")

    enabled: bool | None = Field(
        default=None,
        description=(
            "Allow anyone who can reach the API to create a NEW organization (tenant) with a "
            "first admin user. None = on in development/test, OFF in staging/production: an "
            "internet-facing deployment must opt in explicitly with SIGNUP_ENABLED=true."
        ),
    )
    max_per_ip: int = Field(
        default=5, gt=0, description="Signup attempts per client IP per window."
    )
    window_seconds: int = Field(default=3600, gt=0)


class UsageSettings(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="USAGE_")

    metering_enabled: bool = Field(default=True)
    default_daily_token_limit: int | None = Field(
        default=None, gt=0, description="LLM tokens/day per tenant when it has no budget row."
    )
    default_daily_cost_usd_limit: float | None = Field(
        default=None, gt=0, description="Known USD spend/day per tenant when it has no budget row."
    )
    budget_fail_open: bool = Field(
        default=True, description="If the budget check itself errors: allow (True) or block."
    )


class HealthSettings(_EnvSettings):
    """Readiness probe behaviour (Phase 34)."""

    model_config = SettingsConfigDict(env_prefix="HEALTH_")

    check_timeout_seconds: float = Field(
        default=3.0, gt=0, description="Per-dependency bound inside /health/ready."
    )
    kafka_required: bool = Field(
        default=False,
        description="Does an unreachable Kafka make this API instance NOT ready? The API only "
        "writes to the Postgres outbox, so by default a broker outage is reported as degraded "
        "(the outbox absorbs the writes) rather than pulling every pod out of rotation.",
    )
    redis_required: bool | None = Field(
        default=None,
        description="Does a Redis outage make this instance NOT ready? None = derive: required "
        "only when the login throttle is configured to fail closed. Redis is otherwise an "
        "optimisation/protection layer that fails open, and failing readiness on it would "
        "remove every pod from rotation although the service still works.",
    )


class RetrievalSettings(_EnvSettings):
    """
    pgvector HNSW query-time tuning. All unset by default (= database defaults), because the
    right values depend on the pgvector version and on tenant sizes; see
    docs/performance/PHASE_33_PERFORMANCE.md. On pgvector < 0.8 the HNSW index applies the
    tenant/ACL filter AFTER the index scan, so a tenant holding a small share of a large table
    can get far fewer than `limit` results (measured: 3 of 50 at 100K chunks, 10 tenants).
    On pgvector >= 0.8 set hnsw_iterative_scan="relaxed_order". Do NOT set it on 0.6/0.7: the
    parameter does not exist there and the query would fail.
    """

    model_config = SettingsConfigDict(env_prefix="RETRIEVAL_")

    hnsw_ef_search: int | None = Field(default=None, ge=1, le=1000)
    hnsw_iterative_scan: Literal["off", "relaxed_order", "strict_order"] | None = None
    hnsw_max_scan_tuples: int | None = Field(default=None, ge=1)


class Settings(_EnvSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    environment: Environment = Field(default=Environment.production)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)  # type: ignore[arg-type]
    redis: RedisSettings = Field(default_factory=RedisSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    events: EventSettings = Field(default_factory=EventSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    object_store: ObjectStoreSettings = Field(default_factory=ObjectStoreSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)  # type: ignore[arg-type]
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    rate_limits: RateLimitSettings = Field(default_factory=RateLimitSettings)
    usage: UsageSettings = Field(default_factory=UsageSettings)
    hardening: HardeningSettings = Field(default_factory=HardeningSettings)
    signup: SignupSettings = Field(default_factory=SignupSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)

    # ---- derived switches (single source of truth; nothing else inspects ENVIRONMENT) ----

    @property
    def kafka_enabled(self) -> bool:
        if self.kafka.enabled is not None:
            return self.kafka.enabled
        return self.environment.is_hardened

    @property
    def local_event_dispatch_enabled(self) -> bool:
        if self.events.local_dispatch is not None:
            return self.events.local_dispatch
        return self.environment is Environment.development and not self.kafka_enabled

    @property
    def signup_enabled(self) -> bool:
        if self.signup.enabled is not None:
            return self.signup.enabled
        return not self.environment.is_hardened

    @property
    def uses_local_object_store(self) -> bool:
        return bool(self.object_store.local_directory)

    @model_validator(mode="after")
    def _validate_for_environment(self) -> Settings:
        """Mode-specific rules. Messages name settings, never values."""
        self._validate_kafka_switches()
        if self.environment.is_hardened:
            self._validate_hardened()
        elif self.environment is Environment.development:
            self._validate_development()
        return self

    def _validate_kafka_switches(self) -> None:
        kafka_on = self.kafka_enabled
        if self.environment.is_hardened and self.events.local_dispatch:
            raise ValueError(
                "EVENTS_LOCAL_DISPATCH is a development-only substitute for Kafka and is "
                "not permitted in staging/production."
            )
        if kafka_on and not (self.kafka.bootstrap_servers or "").strip():
            raise ValueError(
                "KAFKA_BOOTSTRAP_SERVERS is required when Kafka is enabled "
                "(set it, or set KAFKA_ENABLED=false in development)."
            )
        if self.events.local_dispatch and kafka_on:
            raise ValueError(
                "EVENTS_LOCAL_DISPATCH=true cannot be combined with Kafka enabled: every event "
                "would be processed twice. Disable Kafka (development) or the local dispatcher."
            )

    def _validate_development(self) -> None:
        """A production `.env` copied onto a laptop must fail loudly, not hang on DNS."""
        hosts = {
            "DATABASE_URL": str(self.database.url),
            "DATABASE_READ_ONLY_ROLE_URL": str(self.database.read_only_role_url or ""),
            "REDIS_URL": str(self.redis.url),
            "KAFKA_BOOTSTRAP_SERVERS": (
                (self.kafka.bootstrap_servers or "") if self.kafka_enabled else ""
            ),
        }
        stale = [
            name
            for name, value in hosts.items()
            if any(marker in value for marker in _CLUSTER_HOST_MARKERS)
        ]
        if stale:
            raise ValueError(
                f"ENVIRONMENT=development but {stale} point at cluster-internal hostnames "
                "(*.svc.cluster.local). This looks like a production .env. Use local service "
                "URLs, or set ENVIRONMENT=production if this really is a deployment."
            )
        if not self.object_store.bucket and not self.object_store.local_directory:
            self.object_store.local_directory = _DEFAULT_DEV_OBJECT_DIR
        if self.object_store.bucket and self.object_store.local_directory:
            raise ValueError(
                "OBJECT_STORE_BUCKET and OBJECT_STORE_LOCAL_DIRECTORY are both set; choose one."
            )

    def _validate_hardened(self) -> None:
        """Staging/production: Kafka and every endpoint are mandatory and explicit."""
        if self.kafka.enabled is False:
            raise ValueError(
                "KAFKA_ENABLED=false is not permitted in staging/production: the outbox relay "
                "and ingestion consumer require Kafka."
            )
        if "url" not in self.redis.model_fields_set:
            raise ValueError(
                "REDIS_URL must be set explicitly in staging/production "
                "(the localhost default is development-only)."
            )
        if self.object_store.local_directory:
            raise ValueError(
                "OBJECT_STORE_LOCAL_DIRECTORY is development-only and not permitted in "
                "staging/production; configure OBJECT_STORE_BUCKET."
            )
        from packages.security.known_keys import is_committed_test_key

        if is_committed_test_key(self.security.jwt_public_key):
            raise ValueError(
                "SECURITY_JWT_PUBLIC_KEY is the test key committed to this repository; "
                "its private half is public. Generate a fresh keypair for this environment."
            )

        kafka = self.kafka
        protocol = kafka.security_protocol.upper()
        if protocol.startswith("SASL") and not (
            kafka.sasl_mechanism and kafka.sasl_username and kafka.sasl_password
        ):
            raise ValueError(
                "KAFKA_SECURITY_PROTOCOL is a SASL protocol but KAFKA_SASL_MECHANISM, "
                "KAFKA_SASL_USERNAME and KAFKA_SASL_PASSWORD are not all set."
            )
        if protocol == "PLAINTEXT":
            raise ValueError(
                "KAFKA_SECURITY_PROTOCOL=PLAINTEXT is not permitted in staging/production."
            )

        candidates = {
            "database.url": str(self.database.url),
            "redis.url": str(self.redis.url),
            "kafka.bootstrap_servers": self.kafka.bootstrap_servers or "",
        }
        offenders = [
            name
            for name, value in candidates.items()
            if any(marker in value for marker in _LOCALHOST_MARKERS)
        ]
        if offenders:
            raise ValueError(
                "Localhost-style hosts are not permitted for inter-service "
                f"communication outside development/test environments: {offenders}. "
                "Use Docker Compose service names or Kubernetes DNS."
            )
        if self.hardening.require_dependency_tls:
            self._require_tls_dependencies()

    def _require_tls_dependencies(self) -> None:
        from urllib.parse import parse_qs, urlsplit

        tls_modes = {"require", "verify-ca", "verify-full"}
        problems: list[str] = []
        dsns = {
            "DATABASE_URL": self.database.url,
            "DATABASE_READ_ONLY_ROLE_URL": self.database.read_only_role_url,
        }
        for name, dsn in dsns.items():
            if dsn is None:
                continue
            mode = parse_qs(urlsplit(str(dsn)).query).get("ssl", [""])[0]
            if mode not in tls_modes:
                problems.append(f"{name} must set ?ssl=require (or verify-ca/verify-full)")
        if urlsplit(str(self.redis.url)).scheme != "rediss":
            problems.append("REDIS_URL must use rediss://")
        if problems:
            raise ValueError(
                "Unencrypted dependency connections are not permitted in staging/production: "
                + "; ".join(problems)
                + ". To accept a trusted private network explicitly, set "
                "HARDENING_REQUIRE_DEPENDENCY_TLS=false."
            )

    @field_validator("environment", mode="before")
    @classmethod
    def _normalize_environment(cls, value: object) -> object:
        if isinstance(value, str):
            return value.lower()
        return value


def load_settings() -> Settings:
    """Build Settings, converting a validation failure into a value-free SettingsLoadError.

    pydantic's own message echoes `input_value=...`, which for DSN fields includes the
    password, so it must never reach a log or a terminal."""
    try:
        return Settings()
    except ValidationError as exc:
        problems = []
        for err in exc.errors(include_input=False, include_url=False):
            where = ".".join(str(part) for part in err["loc"])
            prefix = f"{exc.title}.{where}" if where else exc.title
            problems.append(f"{prefix}: {err['msg'].removeprefix('Value error, ')}")
        raise SettingsLoadError(
            "Invalid configuration (values are never printed): " + "; ".join(problems)
        ) from None


@lru_cache
def get_settings() -> Settings:
    """
    Process-wide singleton. Each app entrypoint should call this once at startup
    (not lazily on first request) so misconfiguration is caught before the process
    reports itself as ready. Raises SettingsLoadError (never a raw pydantic error).
    """
    return load_settings()
