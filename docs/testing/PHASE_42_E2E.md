# Phase 42 — Browser end-to-end tests

## What runs

```
Chromium (Playwright) -> gateway nginx (real infrastructure/docker/gateway/nginx.conf, upstreams swapped)
                           |- /api/* -> uvicorn running the real API   (tests/e2e/harness.py)
                           `- /      -> web nginx (real apps/web/nginx/default.conf) serving the real `npm run build`
```

Run: `pytest tests/e2e` (needs Postgres+pgvector, Redis, nginx, Chromium, Node; tests **skip**, not fail, when nginx/Chromium/frontend build are unavailable). CI job `e2e` in `.github/workflows/ci.yml`.

Users are seeded with random passwords written (0600) to a per-run state file; nothing is committed. The harness refuses to touch an outbox unless `ENVIRONMENT` is test/development and the DB name contains `test`.

## Stand-ins (disclosed)

| Real in production | In E2E |
|---|---|
| Kafka broker | In-process loopback behind the **real** `EventProducer`, `OutboxRelay` and `handle_document_ingested` |
| S3 object store | `InMemoryObjectStore` |
| LLM provider | Extractive stand-in echoing the first non-heading source line with a citation |
| Embedding model / reranker | Deterministic local embeddings / lexical reranker |
| Analytics executor | Disabled (analytics questions return "not supported") |

So E2E proves **wiring and the user-visible pipeline**. It does not prove broker semantics (rebalances, redelivery, DLQ), S3 behaviour, or answer quality.

## Verified (17 tests, all passing locally)

Upload → ingestion → cited answer; tenant B sees none of tenant A's documents and cannot retrieve them; wrong password, unknown tenant vs unknown user (identical message); deep-link login redirect; sign-out; expired session; server-rejected tampered token; RBAC (analyst has no admin nav, API 403); auditor nav; admin audit-chain verify; budget save + persistence across reload; invalid budget; unsupported file type rejected client-side, fake PDF rejected server-side; security headers and CSP blocking inline script; the deployment smoke test against the full stack. Any CSP violation or page error fails a test at teardown.

## Defects found by E2E and fixed

1. **Usage form lost typed input** when the initial budget fetch resolved after the user started typing (and on period change). The form is now seeded once and never overwrites edits.
2. **Sign-out left a return path**: the next person to sign in on that browser was sent to the previous user's page. Explicit sign-out now resets it to `/`.

## Not verified

Real Kafka/S3/LLM; Firefox/WebKit/mobile viewports; accessibility audit; visual regression; load; the CI `e2e` job itself (never run on GitHub); a question routed to analytics with a real executor.

## Screenshots
`EKC_SCREENSHOT_DIR=/some/dir pytest tests/e2e/test_screenshots.py` walks the real UI (login, error, ask, documents, cited answer, tenant isolation, usage/budget, audit verification) and saves full-page PNGs. It is skipped when the variable is unset.
