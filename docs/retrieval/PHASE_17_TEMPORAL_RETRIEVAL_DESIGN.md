# Temporal Retrieval — Phase 17

## The gap this phase closes
Every retrieval phase built so far (11, 13, 14, 15, 16) operated on whatever chunks happened to
be embedded/indexed for a document, with **no awareness of which version was actually in
effect**. If a document had a superseded v1 and a current v2 both chunked and embedded — exactly
what a real ingestion pipeline would produce — retrieval would return chunks from *both*,
mixed together with no way to distinguish current from historical content. Section 16 is
explicit that this must not happen: "current" questions must resolve to the currently effective
version, "historical" questions to whatever was effective then, and this gap would have broken
that guarantee silently once Phase 9-11's pipeline was eventually wired to ingestion.

## Scope delivered
- `packages/database/repositories/temporal.py` — `build_version_temporal_predicate(as_of)`: the
  SQL-query counterpart to Phase 5's `get_current_version`, usable inside a join/WHERE clause
- Every `_authorized` retrieval function (`find_nearest_chunks_authorized`,
  `search_chunks_lexical_authorized`, `hybrid_search_authorized`,
  `reranked_hybrid_search_authorized`) now accepts `as_of: datetime | None = None` and ANDs the
  temporal predicate alongside the ACL predicate
- `get_chunks_for_version_comparison` — "what changed between v2 and v3," ACL-enforced but
  deliberately bypassing the temporal filter (a comparison explicitly wants a non-current version)

## Design: the same rule as Phase 5, now enforced in SQL
`build_version_temporal_predicate` mirrors Phase 5's `get_current_version` logic exactly: a
version is in effect at `as_of` if `status != 'draft'` and `effective_from <= as_of` and
(`effective_until IS NULL OR effective_until > as_of`). A version that was published and has
since been superseded still correctly satisfies this for a past `as_of` — only a draft (never
actually in effect) is unconditionally excluded, regardless of how far back `as_of` reaches.

## Proof, not assertion
Six integration tests build a document through a real create → chunk → embed → publish →
supersede cycle (reusing Phases 5, 10, and 11's real machinery) and prove, against real
Postgres:
- Default retrieval (`as_of=None`) returns only the current version's chunk, even though both
  v1 and v2 are genuinely chunked and embedded in the database
- An explicit historical `as_of` resolves to whichever version was in effect at that instant
- A draft's chunks are never returned, not at `now`, not at any arbitrarily distant past `as_of`
- Both the lexical and hybrid retrieval legs independently respect the same temporal filtering,
  not just the vector leg
- Version comparison returns both versions' chunks correctly, and — critically — returns nothing
  for an identity that isn't authorized to see the document at all, proving comparison doesn't
  become a backdoor around ACL enforcement just because its request shape differs from search

## Real defects found this phase
None — every new test passed on its first run, and the existing Phase 14-16 retrieval test
suites (21 tests) continued passing unmodified, confirming the new `as_of` parameter is fully
backward compatible (defaulting to "now" preserves every prior test's existing behavior, since
all of them seeded single, currently-effective versions).

## Not yet implemented (explicitly, not silently)
- **No query-time detection of "is this a historical question?"** — a caller must explicitly
  pass `as_of`; nothing yet parses "what was the policy in 2024?" out of natural language and
  resolves it to a timestamp. That's query understanding/routing territory (Phase 21).
- **No explicit "what changed" generation** — `get_chunks_for_version_comparison` supplies the
  raw material (both versions' chunks); turning that into an LLM-generated diff/summary is
  Phase 19-20 territory.
- **Document-level `GET /documents` listing is still "current state only"** — it doesn't yet
  expose an `as_of` parameter for browsing historical document lists, only retrieval does.
