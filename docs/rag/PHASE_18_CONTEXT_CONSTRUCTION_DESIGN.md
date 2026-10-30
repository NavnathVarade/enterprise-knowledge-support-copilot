# Phase 18 — Context Construction (Spec §28–29)

## Purpose
Turn reranked candidates into the `ContextPackage` that generation (Phase 19+) receives.
The LLM never sees raw retrieval output.

## Design
| Module | Role |
|---|---|
| `rag/context/base.py` | `ContextChunk` (with full citation metadata), `ContextSelectionStats`, `ContextPackage` |
| `rag/context/selection.py` | Pure, I/O-free selection rules |
| `rag/context/builder.py` | DB re-check + citation resolution, then calls selection |

### Selection order (per candidate, best-first)
1. **Version consistency** — first-seen version of a document pins it; other versions of the same document are dropped. The pin survives even if the pinning chunk is dropped later.
2. **Dedup** by `content_hash` (across documents).
3. **Source diversity** — at most `max_chunks_per_document` (default 3).
4. **Token budget** — `max_tokens` (default 4000). An oversized chunk is skipped; smaller later chunks may still fit; `truncated_by_budget` is set.

### Defense in depth (§11)
The builder does not trust upstream. One join query (chunk → version → document) re-applies the tenant filter, `build_document_acl_predicate` and the temporal predicate, and takes citation metadata from the database, not from the candidate objects. Failures are counted (`dropped_unauthorized_count`, `dropped_not_in_effect_count`) and never passed on. `identity.tenant_id` must equal `tenant_id`.

### Observability
`ContextSelectionStats` records why each candidate was dropped, for retrieval debugging.

## Tests (24, all passing)
- 13 unit tests for selection (order, dedup, cap, budget, exact fit, version pin, conflicting docs, counters sum to input, invalid limits, no input mutation).
- 9 integration tests against real Postgres: citation metadata, dedup, diversity, budget, conflicting sources both reach context, smuggled unauthorized candidate dropped, cross-tenant candidate dropped, draft content dropped, identity/tenant mismatch rejected, empty input.
- Full suite on a fresh DB after an alembic up/down/up cycle: **283 passed**. ruff, ruff format and `mypy --strict` clean.

## Known limitations (honest)
- **Contradiction detection is not done here.** Two documents saying "30 days" vs "14 days" both reach the context; surfacing the conflict is Phase 19–20 (generation prompt + citation validation). Source-authority ranking (§30) is not implemented yet.
- Token counts use the ~4 chars/token heuristic from Phase 10; they are not exact for any specific LLM tokenizer.
- Context ordering is rerank order only; no position-aware reordering.
- `rerank_score` is only as meaningful as the reranker: the local one is lexical overlap, and the Voyage client is mock-tested but NOT network-verified.
- Prompt/input/output token accounting (§29) belongs to the LLM gateway (Phase 19) — NOT YET BUILT.
