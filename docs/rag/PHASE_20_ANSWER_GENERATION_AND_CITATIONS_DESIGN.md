# Phase 20 — Answer Generation, Citations, Abstention, Prompt Versioning (Spec §32–36)

## Flow
`ContextPackage` (Phase 18, already authorized) → numbered sources `S1..Sn` → versioned system prompt + escaped user message → `LLMGateway` (Phase 19) → output check → `Answer`.

## Modules
| File | Role |
|---|---|
| `rag/generation/prompts.py` | `PromptTemplate` (name, version, SHA-256 of text); `rag_answer` v1 |
| `rag/generation/prompt_builder.py` | Source blocks with title/version/section/page/classification; HTML-escapes text and attributes |
| `rag/generation/answer.py` | `generate_answer` → `Answer` (ANSWERED / ABSTAINED) |
| `rag/citations/validator.py` | Extracts `[S1]`, `[S1][S2]`, `[S1, S2]`; validates against the sources actually supplied |
| `packages/database/models/prompts.py`, `repositories/prompts.py`, migration `0003_prompt_versions` | Prompt registry; (name, version) is immutable |

## Rules enforced
- **No fabricated citations:** any cited id not among the supplied sources → `CitationValidationFailed`; the answer is not returned.
- **Non-abstaining answers must cite** at least one source. Optional strict mode requires a citation in every paragraph of 40+ characters (off by default to avoid false rejections).
- **Abstention, never a made-up answer:** `NO_CONTEXT` (empty package; no LLM call), `LOW_EVIDENCE` (top rerank score below an optional configured threshold; no LLM call), `MODEL_ABSTAINED` (reply is exactly `INSUFFICIENT_EVIDENCE`). The sentinel mixed with other text is *not* treated as an abstention.
- **Prompt-injection hardening (partial):** sources are marked untrusted in the system prompt; text and attributes are escaped so a document cannot close the `<source>` tag.
- **Conflicts:** the prompt instructs the model to say "The available sources contain conflicting information." and cite each side. Tests show a conflicting, both-cited answer passes validation.
- **Traceability:** every `Answer` carries `prompt_name`, `prompt_version`, `prompt_hash`, context tokens, and the gateway result (input/output tokens, cost, attempts, fallback).
- **Prompt immutability:** `register_prompt` is idempotent for identical text; the same version with different text raises `ConfigurationError` (bump the version instead).

## Tests (21): 19 unit + 2 integration
Citation parsing, fabricated/missing/per-paragraph citations, prompt metadata, break-out escaping, attribute escaping, required prompt rules, hash stability, answered/abstained paths (including "no LLM call" proofs), sentinel edge cases, conflicting sources, registry idempotency and immutability. Full suite after a fresh-DB alembic up/down/up: **337 passed**; ruff, format, `mypy --strict` clean.

## Not verified / not built (honest)
- **Citation validity ≠ citation support.** We prove the cited source was in the context, NOT that it supports the sentence. Entailment/LLM-judge checks belong to Phase 28 — NOT BUILT.
- **Escaping and prompt rules reduce but do not eliminate prompt injection.** A dedicated injection-defense phase (27) will add detection and tests against the seeded injection document.
- Conflict handling relies on the model following the prompt; there is no programmatic contradiction detector, and source-authority ranking (§30) is not built.
- No regeneration on validation failure (one attempt; failure is surfaced). No streaming.
- The model's actual compliance with the prompt has NOT been tested against a real LLM (no provider reachable); tests use a scripted fake provider.
- Abstention thresholds are uncalibrated: rerank scores from the local reranker are lexical overlap, not calibrated probabilities. NOT YET MEASURED.
