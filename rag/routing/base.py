"""Query routing types (Section 12-13)."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime


class QueryRoute(enum.StrEnum):
    RAG_KNOWLEDGE = "RAG_KNOWLEDGE"
    LEXICAL_SEARCH = "LEXICAL_SEARCH"
    HYBRID_SEARCH = "HYBRID_SEARCH"
    SQL_ANALYTICS = "SQL_ANALYTICS"
    RAG_PLUS_SQL = "RAG_PLUS_SQL"
    DOCUMENT_LOOKUP = "DOCUMENT_LOOKUP"
    DOCUMENT_COMPARISON = "DOCUMENT_COMPARISON"
    INCIDENT_ANALYSIS = "INCIDENT_ANALYSIS"
    SUPPORT_ANALYSIS = "SUPPORT_ANALYSIS"
    ACCESS_CHECK = "ACCESS_CHECK"
    UNKNOWN = "UNKNOWN"


class TimeMode(enum.StrEnum):
    NONE = "NONE"  # no temporal hint: default retrieval behavior (currently effective)
    CURRENT = "CURRENT"  # "current", "latest", "now"
    RANGE = "RANGE"  # an explicit [start, end) window, past or to-date


@dataclass(frozen=True)
class TimeRange:
    mode: TimeMode
    start: datetime | None = None
    end: datetime | None = None
    matched_text: str | None = None


@dataclass(frozen=True)
class ExtractedEntities:
    version_refs: tuple[int, ...] = ()
    quoted_terms: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()  # error codes / ticket ids such as ERR-4021, INC-123


@dataclass(frozen=True)
class RoutingDecision:
    route: QueryRoute
    confidence: float  # heuristic rule score in [0,1]; NOT a calibrated probability
    classifier_version: str
    reason: str
    latency_ms: float
    tenant_id: uuid.UUID
    normalized_query: str
    time_range: TimeRange
    entities: ExtractedEntities
    matched_signals: tuple[str, ...] = field(default_factory=tuple)
    fallback_used: bool = False
