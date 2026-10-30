# Phase 26 — Document Version Comparison

Spec §15/§16: "what changed between v2 and v3?" and "current vs previous".

## Flow
1. **Resolve** (orchestrator, `packages/orchestration/query_service.py`):
   - Document: explicit UUID → exact quoted title → top ACL-enforced hybrid search.
     The payload's `resolved_document_by` is `document_id | title | search`.
     **`search` is an inference and may pick the wrong document; verify `document_title`.**
   - Versions: two `vN` references, or "previous/prior/older/earlier/last version" →
     current version vs the highest earlier non-draft version. Otherwise `NEEDS_INPUT`.
2. **Fetch** (`get_chunks_for_version_comparison`): tenant filter + ACL predicate in SQL,
   **drafts excluded**.
3. **Diff** (`rag/comparison/diff.py`, pure): match by `content_hash` (multiset, so
   duplicates are counted); unmatched chunks pair by identical non-null `heading_path` →
   *modified*; rest are *added* / *removed*. Reordering is not a change.
4. **No changes → no model call.** The answer states the versions are identical.
5. **Explain**: only differing excerpts (each labelled with its version) go to the model
   under `rag_compare_versions` v1. Modified first, then added, then removed. HIGH-risk
   injection excerpts are quarantined; the token budget applies. Output policy and citation
   validation are the same as for ordinary answers.

## Security
- A missing version, a draft, another tenant's document, an unauthorized document, and a
  missing document are all `ResourceNotFound` — comparison is not an existence oracle.
- **Defect found and fixed this phase:** the Phase 12 comparison query did not exclude
  draft versions, so an unpublished draft could be diffed. Fixed in the repository, with a
  regression test.
- **Router gap found and fixed:** natural phrasings such as "…compared to the previous
  version" were routed to LEXICAL_SEARCH when a quoted title was present. Added the
  `relative_version` and `differ_from` rules (classifier `rules-v1`, still heuristic).

## Limits (honest)
- Granularity is the **chunk**. A one-word edit marks the whole chunk modified; re-chunking
  between versions can turn unchanged text into modified/added/removed. The model sees the
  excerpts and states what actually changed, but the diff is a candidate set only.
- No semantic alignment of renamed headings; they appear as removed + added.
- Only the two versions' *differing* excerpts are sent; context outside them is not.
- Quality of explanations with a real LLM: **NOT YET MEASURED** (tests use a scripted
  model and verify wiring, selection, isolation and budget, not explanation quality).

## Tests
`tests/unit/test_version_diff.py` (6), `tests/integration/test_document_comparison.py` (11),
router cases (3), updated Phase 25 expectation (comparison with no resolvable document is
`NEEDS_INPUT`, not `NOT_SUPPORTED`). Full suite: 553 passing on a fresh database, including
alembic up/down/up.
