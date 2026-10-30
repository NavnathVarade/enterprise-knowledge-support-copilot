"""
Document processing lifecycle state machine (Section 14, 49).

Deliberately pure functions with no I/O: given a current status and a requested next
status, either return successfully or raise IllegalStatusTransition. Callers (the
ingestion pipeline's repository/service layer, Phase 7+) are responsible for actually
persisting the new status once this function has approved the move -- this module never
touches the database, which is what makes it exhaustively unit-testable.
"""

from __future__ import annotations

from packages.domain.document_status import DocumentStatus
from packages.domain.errors import IllegalStatusTransition

# Every key is a status; its value is the set of statuses that may be transitioned TO
# from it. This is the single source of truth for "what's a legal move" -- Section 49
# requires illegal transitions to be tested, which requires them to be well-defined
# here rather than scattered across call sites.
_ALLOWED_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.uploaded: frozenset({DocumentStatus.validating, DocumentStatus.quarantined}),
    DocumentStatus.validating: frozenset(
        {DocumentStatus.processing, DocumentStatus.quarantined, DocumentStatus.processing_failed}
    ),
    DocumentStatus.processing: frozenset({DocumentStatus.parsed, DocumentStatus.processing_failed}),
    DocumentStatus.parsed: frozenset({DocumentStatus.chunked, DocumentStatus.processing_failed}),
    DocumentStatus.chunked: frozenset({DocumentStatus.embedding, DocumentStatus.embedding_failed}),
    DocumentStatus.embedding: frozenset({DocumentStatus.indexing, DocumentStatus.embedding_failed}),
    DocumentStatus.indexing: frozenset({DocumentStatus.active, DocumentStatus.indexing_failed}),
    DocumentStatus.active: frozenset(
        {DocumentStatus.archived, DocumentStatus.deleted, DocumentStatus.validating}
    ),  # validating: re-processing an active document (e.g. re-chunking after a config change)
    # Failure states may be retried (back to validating) or given up on (quarantined).
    DocumentStatus.processing_failed: frozenset(
        {DocumentStatus.validating, DocumentStatus.quarantined}
    ),
    DocumentStatus.embedding_failed: frozenset(
        {DocumentStatus.validating, DocumentStatus.quarantined}
    ),
    DocumentStatus.indexing_failed: frozenset(
        {DocumentStatus.validating, DocumentStatus.quarantined}
    ),
    DocumentStatus.quarantined: frozenset({DocumentStatus.validating, DocumentStatus.deleted}),
    DocumentStatus.archived: frozenset({DocumentStatus.active, DocumentStatus.deleted}),
    DocumentStatus.deleted: frozenset(),  # terminal -- see Section 71, deletion propagation
}


def assert_valid_transition(current: str, target: str) -> None:
    """
    Raises IllegalStatusTransition if `target` is not reachable from `current`.
    Accepts plain strings (as stored in the database column) rather than requiring the
    caller to already hold a DocumentStatus enum member.
    """
    try:
        current_status = DocumentStatus(current)
        target_status = DocumentStatus(target)
    except ValueError as exc:
        raise IllegalStatusTransition(
            f"Unknown document status in transition check: {exc}"
        ) from exc

    allowed = _ALLOWED_TRANSITIONS.get(current_status, frozenset())
    if target_status not in allowed:
        raise IllegalStatusTransition(
            f"Cannot transition document from '{current_status.value}' to '{target_status.value}'."
        )


def is_valid_transition(current: str, target: str) -> bool:
    try:
        assert_valid_transition(current, target)
        return True
    except IllegalStatusTransition:
        return False
