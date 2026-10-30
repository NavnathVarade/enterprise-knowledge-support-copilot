"""
Structured logging configuration.

Every log line is JSON and carries timestamp/service/environment/level/message plus
whatever contextual fields (trace_id, request_id, tenant_id, operation, duration_ms,
error_code) the caller binds in. A redaction processor strips known-sensitive keys so
secrets, tokens, and raw document/prompt content never reach the log sink even if a
caller accidentally includes them (Section 62 — "never log secrets, tokens, passwords,
API keys, sensitive document content, or unrestricted LLM prompts").
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_REDACTED = "***REDACTED***"

# Keys that must never appear in a log line, regardless of which layer produced it.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "authorization",
        "jwt",
        "db_password",
        "private_key",
        "prompt",  # raw prompts may embed retrieved confidential content
        "document_content",
        "chunk_text",
        "raw_context",
    }
)


def _redact_sensitive_fields(
    _logger: structlog.types.WrappedLogger,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = _REDACTED
    return event_dict


def _add_otel_ids(
    logger: structlog.types.WrappedLogger, method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    from packages.observability.tracing import log_correlation_processor

    return log_correlation_processor(logger, method, dict(event_dict))


def configure_logging(*, service_name: str, environment: str, log_level: str = "INFO") -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper(), logging.INFO),
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_otel_ids,
            _redact_sensitive_fields,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    structlog.contextvars.bind_contextvars(service=service_name, environment=environment)


def get_logger(**initial_context: Any) -> structlog.types.FilteringBoundLogger:
    logger: structlog.types.FilteringBoundLogger = structlog.get_logger(**initial_context)
    return logger
