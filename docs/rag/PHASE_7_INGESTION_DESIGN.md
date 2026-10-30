# Document Ingestion — Phase 7

## Scope delivered
- `rag/ingestion/validation.py` — filename, MIME-type, and size validation (Section 88)
- `POST /api/v1/documents` — real authenticated, tenant-scoped upload endpoint
  (`apps/api/app/routes/documents.py`)
- `IngestionSettings` (`packages/config/settings.py`) — max upload size and MIME-type
  allowlist, both configurable

## Design choices
- **The endpoint returns `202 Accepted` immediately**, without running any processing
  pipeline, per Section 83/48's explicit rule that ingestion must not block on long-running
  work. There is no async worker to hand the document to yet (Phase 8 adds Kafka), so the
  document deliberately stays in `status='uploaded'` after this call.
- **Filenames are validated by allowlist, not sanitized.** Section 88's "do not trust
  user-provided filenames" is honored by rejecting anything containing a path separator, a
  parent-directory reference, or characters outside a conservative safe set, rather than
  attempting to "clean" a suspicious filename into something safe.
- **MIME-type checking is an allowlist against the client-supplied `Content-Type`, not
  content sniffing.** This is an explicit, documented gap (see the module docstring in
  `rag/ingestion/validation.py`) — a client can currently claim `text/plain` for a file that
  isn't actually plain text. Real content-type sniffing is deferred to Phase 9 (parsing).

## Proof, not assertion
`tests/integration/test_document_upload.py` exercises the real HTTP path: login → upload →
confirm the document appears via `GET /documents` with `status='uploaded'`. It also proves,
over real HTTP with a real JWT:
- disallowed MIME types are rejected (`422 UNSUPPORTED_MIME_TYPE`)
- path-traversal filenames are rejected (`422 INVALID_DOCUMENT`)
- unauthenticated uploads are rejected (`401`)
- **cross-tenant isolation holds for uploads specifically**: a document uploaded by Tenant A's
  user never appears in Tenant B's document list, exercised through two real logins and two
  real tokens, not a direct repository call

## Not yet implemented (explicitly, not silently)
- No actual file storage — the endpoint reads the upload into memory, hashes it, and
  discards the bytes after computing the hash. Raw artifact retention (Section 18) requires
  object storage, which isn't part of this phase.
- No duplicate-content detection on upload (Section 19) — the content hash is computed and
  stored, but nothing yet checks it against existing documents before creating a new row.
- No bulk/multi-file upload (Section 84) — one file per request only.
- No content-type sniffing, malware scanning, or zip-bomb protection (Section 88) — flagged
  explicitly above and in the validation module's docstring, not silently absent.
- No Kafka event published on upload — Phase 8 adds the event backbone; today's upload is a
  synchronous database write and nothing else.
