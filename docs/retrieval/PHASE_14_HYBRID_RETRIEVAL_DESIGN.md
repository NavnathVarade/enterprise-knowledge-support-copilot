# Hybrid Retrieval — Phase 14

## Scope delivered
- `rag/retrieval/fusion.py` — pure Reciprocal Rank Fusion (RRF), no I/O
- `rag/retrieval/hybrid.py` — `hybrid_search`: runs Phase 11's vector search and Phase 13's
  lexical search in parallel, fuses via RRF, and loads the final chunk rows

## Why RRF (ADR-006, applied)
pgvector's cosine distance and Postgres full-text's `ts_rank_cd` live on entirely different,
incomparable numeric scales — there's no principled way to average or weight-sum them without
calibration data this reference implementation doesn't have. RRF sidesteps that by only ever
looking at *rank position* within each retriever's own list, never the raw score. This also
means deduplication falls out of the fusion math for free: a chunk appearing in both retrievers'
results is one dictionary key in `reciprocal_rank_fusion`, receiving both contributions summed —
never two separate entries that need reconciling afterward.

## Proof, not assertion
14 tests total. The 9 pure unit tests (`tests/unit/test_fusion.py`) prove the fusion math
directly: rank-1-in-both beats rank-1-in-one, a symmetric two-list case produces equal scores
for symmetric positions, smaller `k` sharpens rank differences, output is fully deterministic.

The 5 integration tests, against a real Postgres running both real retrieval legs:
- `test_chunk_matched_by_both_legs_ranks_first_and_is_not_duplicated` — queries with a chunk's
  *exact* text, which is guaranteed (by the deterministic provider's own definition) to be the
  vector leg's top hit and naturally the lexical leg's top hit too (it IS the text). Confirms
  the chunk ranks first in the fused result and, critically, appears **exactly once** — the
  concrete proof that RRF-driven dedup works, not just claimed.
- `test_lexical_only_match_still_appears_via_fusion` — a chunk with no meaningful vector signal
  (since the deterministic provider carries none) still surfaces because the lexical leg alone
  is enough to earn it an RRF score, proving fusion — not just the vector leg — drives inclusion.
- Tenant isolation and `limit` behavior, following the established pattern from every prior
  retrieval-adjacent phase.

## Real defects found this phase
None — this phase built cleanly on Phases 11 and 13's already-tested retrieval legs; all 14 new
tests passed on their first run. Worth noting honestly rather than manufacturing a story: not
every phase surfaces a bug, and a clean phase built on well-tested foundations is itself a
signal that those foundations were solid.

## Not yet implemented (explicitly, not silently)
- **No reranker yet** (Phase 15) — `hybrid_search`'s output is the final RRF-fused order, not
  yet re-scored by a cross-encoder. `candidate_limit` exists specifically so Phase 15 has more
  than `limit` candidates to work with once it's built.
- **No ACL filtering yet** (Phase 16) — `hybrid_search` returns every matching chunk in the
  tenant regardless of document-level ACL. This is retrieval-quality plumbing only; it must not
  be mistaken for an authorization boundary, which Section 11 is explicit does not yet exist at
  this layer.
- **No temporal filtering** (Phase 17) — a chunk from a superseded document version can appear
  in results with equal standing to a chunk from the current version.
- **`candidate_limit` is the same value for both legs** — a real tuning pass might want
  different candidate depths per retriever; not explored here absent evaluation data.
