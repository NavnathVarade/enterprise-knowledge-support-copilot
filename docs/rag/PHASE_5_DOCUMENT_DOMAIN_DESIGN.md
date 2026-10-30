# Document Domain & Versioning — Phase 5

## Scope delivered
- `packages/domain/document_lifecycle.py` — the document processing state machine, pure and
  exhaustively unit-tested (every valid transition, a representative set of illegal ones, and
  the "deleted is terminal" invariant from Section 71)
- `packages/domain/hashing.py` — SHA-256 content hashing (Section 19)
- `packages/database/repositories/documents.py` — document/version creation, status
  transitions (enforced through the state machine), publish/supersede logic, and temporal
  "current version" resolution (Sections 15-17)

## Temporal resolution — proven against Section 128's exact scenario
`tests/integration/test_document_versioning.py::
test_temporal_resolution_current_vs_historical_vs_comparison` builds a document with three
published versions across different effective windows and proves, against a real Postgres:
- "What's the current policy?" resolves to the version actually in effect *now*, not the
  highest version number
- "What was the policy during v1's effective window?" resolves to v1, not v3 — even though v3
  is the most recently created row
- v2 and v3 are independently retrievable as distinct rows for a future comparison prompt to
  consume
- A published-then-superseded version is never silently returned for the wrong time window (the
  bug below), and a draft version is never returned as "current" no matter how high its version
  number is

## Real defect found and fixed during this phase
**Historical queries were excluding versions that genuinely were in effect during the queried
window.** The first implementation of `get_current_version` filtered on
`status == 'published'` — but by the time a historical query is asked ("what was the policy
during v1's window?"), v1 has typically since been superseded (`status == 'superseded'`) by a
later version. Filtering strictly on the *current* status therefore silently returned nothing
for a perfectly valid historical question — a correctness bug that would have broken exactly
the temporal RAG scenario Section 16 exists to guarantee.

Caught by `test_temporal_resolution_current_vs_historical_vs_comparison` actually asserting on
the historical-query result rather than only the "current" one. Fixed by filtering on
`status != 'draft'` instead (a version that was ever actually published — whether or not it has
since been superseded — represents a real historical effective period; only a draft, which was
never in effect, should be excluded), combined with the existing `effective_from`/
`effective_until` window check.

## Not yet implemented (explicitly, not silently)
- No document-comparison *prompt/generation* logic yet — this phase proves the data layer
  supports retrieving two specific versions cleanly; turning that into a "what changed" LLM
  answer is Phase 20/23 (citation engine, SQL+RAG) territory.
- No ingestion pipeline calling this repository yet (Phase 7-11: parsing, chunking, embedding,
  indexing) — `create_document`/`create_draft_version`/`publish_version` exist and are tested in
  isolation, not yet wired to an upload endpoint.
- `transition_document_status` is tested for the illegal-transition case but the full pipeline
  (uploaded → ... → active) isn't exercised end-to-end yet, since the intermediate steps
  (parsing, chunking, embedding) don't exist as code yet.
