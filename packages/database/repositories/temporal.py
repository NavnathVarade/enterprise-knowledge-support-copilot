"""
Temporal effectiveness predicate (Section 16-17).

Phase 5's `get_current_version` resolves one document's effective version in Python,
given a `document_id` and an `as_of` instant. This module is its SQL-query counterpart:
a boolean expression on `DocumentVersion` columns, usable inside a JOIN/WHERE clause
alongside the ACL predicate, so retrieval queries only ever return chunks belonging to
a version that was/is ACTUALLY IN EFFECT at `as_of` -- never a draft (never in effect)
and never, by default, a superseded version (unless the caller explicitly asks for a
historical `as_of`).

Same rule as Phase 5's documentation: a version that was published and has since been
superseded still represents a real historical effective period and is correctly
returned for a past `as_of` -- only `status == 'draft'` is unconditionally excluded.

Without this predicate, every retrieval function built in Phases 11-16 would return
chunks from EVERY version of a document that happens to be chunked/embedded -- current
and superseded content mixed together with no way to tell them apart. That gap is
closed here, not discovered later.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import ColumnElement, and_, or_

from packages.database.models.documents import DocumentVersion, DocumentVersionStatus


def build_version_temporal_predicate(as_of: datetime | None = None) -> ColumnElement[bool]:
    """
    True when the (unaliased) `DocumentVersion` row referenced by the enclosing query
    was in effect at `as_of` (default: now). The caller's query must already join the
    unaliased `DocumentVersion` table.
    """
    as_of = as_of or datetime.now(UTC)
    return and_(
        DocumentVersion.status != DocumentVersionStatus.draft.value,
        DocumentVersion.effective_from.isnot(None),
        DocumentVersion.effective_from <= as_of,
        or_(
            DocumentVersion.effective_until.is_(None),
            DocumentVersion.effective_until > as_of,
        ),
    )
