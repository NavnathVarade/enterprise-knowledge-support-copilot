"""
Topic catalog (Section 44-45, 153).

Each topic's partition key is documented here as a function, not just a comment,
because the partition key is what determines ordering guarantees: events for the same
document must land in the same partition (so a worker never processes
`document.processing.completed` before `document.ingested` for the same document) by
using `tenant_id:document_id` as the key.
"""

from __future__ import annotations

import uuid


class Topics:
    DOCUMENT_INGESTED = "document.ingested"
    DOCUMENT_PROCESSING_COMPLETED = "document.processing.completed"
    DOCUMENT_PROCESSING_FAILED = "document.processing.failed"

    # Retry/DLQ topic naming convention (Section 47): every main topic gets a
    # `.retry` and a `.dlq` topic. Derived rather than hand-maintained so the two
    # can never drift out of sync with the main topic list above.
    @staticmethod
    def retry_topic_for(main_topic: str) -> str:
        return f"{main_topic}.retry"

    @staticmethod
    def dlq_topic_for(main_topic: str) -> str:
        return f"{main_topic}.dlq"


def document_partition_key(*, tenant_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """Partition key for every document-lifecycle topic (Section 45's table)."""
    return f"{tenant_id}:{document_id}"


def consumer_group_name(*, prefix: str, logical_name: str) -> str:
    return f"{prefix}-{logical_name}"
