# Chunking & Document Lineage — Phase 10

## Scope delivered
- `rag/chunking/tokenizer.py` — token-count approximation
- `rag/chunking/chunker.py` — the chunking engine: heading-aware, paragraph-boundary-aware,
  token-budgeted with paragraph-granularity overlap, sentence-level fallback for oversized
  paragraphs
- `packages/database/repositories/chunks.py` — persists chunks tied to a `document_version_id`
  via the composite tenant-aware foreign key, completing the lineage chain from Section 21

## Design choices
- **Not "chunk_size=N, overlap=M and nothing else"** (the exact anti-pattern Section 20 calls
  out): heading boundaries take priority over the token budget — a chunk is flushed when the
  next paragraph is under a different heading and the current buffer already has substantial
  content, even if the token budget hasn't been hit yet.
- **A real infrastructure decision, made honestly**: `tiktoken` (the originally planned
  tokenizer) fetches its vocabulary file from a remote host at first use
  (`openaipublic.blob.core.windows.net`), which is both outside this environment's network
  allowlist and an unwanted runtime dependency on an external file host in production this
  brief never asked for. Replaced with a self-contained ~4-characters-per-token heuristic — the
  same approximation OpenAI's and Anthropic's own documentation suggest when an exact tokenizer
  isn't available, and Claude's tokenizer isn't published as a library regardless.
- **Overlap is paragraph-granularity, not token-slice-granularity** — the tail paragraph(s) of a
  flushed chunk are repeated at the start of the next one, up to the overlap budget. This keeps
  every chunk's text a clean concatenation of whole paragraphs rather than a token-boundary
  mid-sentence cut.

## Proof, not assertion
`tests/integration/test_chunking_pipeline.py::test_full_parse_chunk_persist_pipeline` runs the
complete real pipeline: generate a genuine multi-page PDF (via `reportlab`) → parse it (Phase 9)
→ chunk it → persist via the repository → reload from the database → verify the reloaded chunks
match what was chunked, in order, with correct page attribution.
`test_chunks_are_tenant_isolated_at_the_schema_level` extends Phase 3's composite-FK
tenant-isolation proof down to `document_chunks`.

## Real defect found and fixed during this phase
**The heading-boundary-flush threshold was scaled as a fraction of `target_tokens`, not an
absolute value.** The intent was "don't flush on a trivial one-line intro immediately after a
heading" — but scaling that threshold to the token *budget* meant that with a large budget, two
entirely separate, genuinely substantial sections could still be merged into a single chunk,
because neither individually cleared a threshold that had grown along with the budget. This
defeats the actual point of heading-aware chunking. Caught by
`test_chunks_do_not_straddle_heading_boundaries_when_substantial` (which deliberately used a
large budget to make the bug visible), fixed with a small absolute token threshold instead.

## Not yet implemented / documented gaps (explicitly, not silently)
- **Page boundaries are not hard-enforced the way heading boundaries are.** Two short pages can
  legitimately merge into one chunk if they fit the token budget together; the chunk's
  `page_number` in that case reflects only its first paragraph's page — a real limitation for
  citation precision near page boundaries, documented here rather than discovered later.
- **Table and code-block-aware chunking** (Section 22) isn't implemented — a Markdown table or
  fenced code block is currently just paragraph text and can be split mid-table if large.
- **Not wired to ingestion** — same gap as Phase 9: no worker calls this yet, since there's no
  object storage holding raw bytes for a worker to parse and chunk from.
