# ACL-Aware Retrieval — Phase 16

This is the phase the entire architecture brief centers on (Section 11): "Authorization must
happen before context reaches the LLM." Every earlier retrieval phase (11, 13, 14, 15) operated
on all of a tenant's chunks with no ACL filtering, explicitly flagged as a gap each time. This
phase closes it.

## Scope delivered
- `packages/database/repositories/authorization.py` — `build_document_acl_predicate`: a
  correlated SQL `EXISTS` expression, not a Python filter, implementing all five `DocumentVisibility`
  types plus the fail-closed default
- `Identity` extended with `department` (needed for `department_only` ACL, propagated through
  JWT claims end-to-end from login)
- ACL-aware retrieval functions mirroring every existing one: `find_nearest_chunks_authorized`,
  `search_chunks_lexical_authorized`, `hybrid_search_authorized`, `reranked_hybrid_search_authorized`
- `GET /api/v1/documents` now applies the ACL predicate in its query; `POST /api/v1/documents`
  now grants the uploader `user_specific` access to their own upload

## The core design: push authorization into the query, never filter after
`build_document_acl_predicate` returns a SQL boolean expression that gets ANDed directly into
every retrieval query's `WHERE` clause, joined through `DocumentChunk → DocumentVersion →
Document`. An unauthorized document's chunks are **never fetched from Postgres at all** — not
retrieved and then discarded, not hidden in the UI, not deprioritized by ranking. They do not
exist in the result set. `hybrid_search_authorized` additionally re-applies the predicate on its
final bulk chunk load as explicit defense in depth — the same "never trust one layer alone"
instinct this codebase already applies everywhere else (Section 11: "also perform
defense-in-depth authorization after retrieval").

## Fail-closed, not fail-open
A document with **zero** `document_acl` rows is authorized to **nobody** — the absence of a
grant is not an implicit grant. This had a real, immediate consequence: the Phase 7 upload
endpoint didn't create any ACL row, so a freshly uploaded document would have been invisible to
its own uploader. Fixed by having upload grant the uploader `user_specific` access as part of
the same request — not bypassing fail-closed, but giving every document exactly the grant a real
system would want it to have by default.

`visibility='restricted'` is deliberately treated as **no automatic grant** — Section 10 lists it
as its own category, and this reference implementation doesn't invent an implicit meaning for it.
A restricted document is invisible unless some other ACL row on the same document grants access
a different way.

## Proof, not assertion
18 new tests directly in `test_acl_aware_retrieval.py`, following one consistent shape: seed a
document with a specific ACL, seed an authorized and unauthorized `Identity`, assert the
unauthorized identity's result set **does not contain the chunk at all**. Covers all five
visibility types, the fail-closed no-ACL-row case, the `restricted`-has-no-automatic-grant case,
and — critically — a test where the restricted chunk is deliberately the **strongest possible
lexical match** for the query (`"refund refund refund policy policy policy..."`), proving that
even a reranker that would otherwise rank it #1 never sees it, because it was excluded before
reranking ever ran. A dedicated test also proves that two tenants having identically-named roles
(`"security_auditor"` in both) doesn't leak access across the tenant boundary.

## An environment failure and recovery, documented honestly
Partway through this phase, the sandbox had a full container reset — not just the Postgres/Redis
services dying (which had happened periodically throughout this project and was handled by
restarting them), but the entire `/home/claude` working directory being wiped. The periodic zip
backups to `/mnt/user-data/outputs` (taken at the end of every phase) made recovery possible:
the Phase 15 package was restored, Postgres/Redis/pgvector were reinstalled from scratch, and all
of this phase's work was re-applied from the content already composed. The full regression suite
was re-run afterward exactly as it would be for any other phase completion — nothing about the
recovery process was taken on faith.

## Real test-construction issues found and fixed (not system bugs)
1. `DocumentACL.allowed_user_id` has a real foreign-key constraint to `users.id` — the first
   `user_specific` test used a random UUID that doesn't correspond to an actual user, which
   correctly failed the FK constraint. Fixed by creating a real `User` row for the test.
2. The hybrid-search test originally asserted that an authorized-but-topically-unrelated chunk
   must be absent from results — but `hybrid_search` has no relevance threshold, so an
   authorized candidate can legitimately surface when it's one of the only authorized chunks
   available. That's correct retrieval behavior, not an ACL leak. The assertion was narrowed to
   what actually matters: the *restricted* chunk's complete absence.

## Not yet implemented (explicitly, not silently)
- **No admin override path** for `restricted` visibility (Section 82) — a restricted document
  is simply invisible in this reference implementation; a real system would likely want an
  explicit, audited override mechanism.
- **No caching of ACL decisions** (Section 52's warning about ACL-cache correctness is heeded by
  *not* caching at all yet, rather than caching incorrectly).
- **No upload-time visibility choice** — the uploader always gets `user_specific` only; broader
  visibility requires a separate, not-yet-built document-management API.
- **The `GET /documents` ACL check and the retrieval ACL check are the same predicate**, which
  is good for consistency but means there's no tested distinction yet between "can see this
  document exists" and "can retrieve its content" (Section 169's access-UX concern about not
  leaking existence) — both are currently the same question.
