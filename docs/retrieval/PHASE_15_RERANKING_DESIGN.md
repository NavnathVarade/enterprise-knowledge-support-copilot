# Reranking — Phase 15

## Scope delivered
- `rag/reranking/base.py` — `RerankerProvider` Protocol
- `rag/reranking/lexical_overlap_reranker.py` — a real, deterministic local reranker
- `rag/reranking/voyage_reranker.py` — a real Voyage AI rerank API client (mock-tested,
  network-unverified — same honest pattern as Phase 11's embedding provider)
- `rag/retrieval/reranked_search.py` — `reranked_hybrid_search`: Phase 14's fused candidates
  → reranker → final order, with every result carrying both `fused_score` and `rerank_score`

## Honest labeling: not a neural cross-encoder
A real cross-encoder reranker needs downloaded model weights, and model hosts (e.g.
huggingface.co) are outside this environment's network allowlist — the same category of gap
as Kafka (Phase 8) and live embeddings (Phase 11). `LexicalOverlapReranker` is a genuine,
deterministic algorithm instead: query-term coverage (what fraction of distinct query terms
appear in the candidate at all) dominates the score, with term density as a tie-breaker among
equal-coverage candidates. It is not a substitute for a trained cross-encoder's semantic
judgment — it's what's honestly buildable and testable here.

## The retry-structure lesson, applied correctly from the start
Phase 11 found a real bug: converting `httpx.TransportError` to a domain error *inside* a
`@retry`-decorated function meant tenacity never saw the exception type it was configured to
retry on. `VoyageReranker` was built with the fix already in place — `_call_api_raw` lets the
raw exception through, `_call_api` converts only after retries are exhausted — and
`test_transport_error_actually_retries` (which asserts exactly 3 calls occurred) passed on its
first run, confirming the lesson genuinely transferred rather than being fixed once and
forgotten.

## A real design gap found and fixed this phase
`reranked_hybrid_search`'s first implementation trusted that a `RerankerProvider`
implementation honors its documented "returns results sorted by score descending" contract,
and used whatever order it returned directly. `test_reranking_genuinely_determines_final_order`
uses a deliberately contract-violating fake reranker (one that assigns correct scores but
returns them in the wrong order) specifically to probe this, and it failed — the wrong order
passed straight through. Fixed by explicitly re-sorting by `rerank_score` before returning,
rather than trusting a single layer to have gotten it right — the same defense-in-depth
instinct this codebase already applies to authorization (Section 11: never let one layer be
the only thing standing between a request and the wrong answer).

## Proof, not assertion
14 new tests. 9 unit tests for `LexicalOverlapReranker` (coverage beats raw frequency, density
breaks ties, empty query/candidates handled, deterministic). 5 unit tests for `VoyageReranker`
via `httpx.MockTransport` (request shape, response-index mapping, the retry regression test).
5 integration tests against real Postgres: the order-inverting-fake proof described above, a
check that every result carries both score fields, the real reranker correctly surfacing a
highly relevant chunk over a tangential one, `limit` respected, and the empty-candidates case.

## Not yet implemented (explicitly, not silently)
- **No real cross-encoder** — flagged above; whether the lexical-overlap heuristic is "good
  enough" versus a trained model is a Phase 28 evaluation question, not assumed either way.
- **No ACL filtering yet** (Phase 16) — reranking still operates on every matching chunk in
  the tenant, same caveat as Phase 14.
- **No caching of rerank results** (Section 52) — every call re-scores from scratch.
