# Frontend screenshots

Captured by `tests/e2e/test_screenshots.py` from a **real Chromium** talking to the **real gateway nginx → web nginx →
API → Postgres/Redis** stack (`tests.e2e.harness`). Regenerate:

```bash
cd apps/web && npm ci && npm run build && cd ../..
set -a; . ./.env.test; set +a            # a test database + Redis (see HOW_TO_RUN.md Part C)
EKC_SCREENSHOT_DIR=$PWD/docs/screenshots uv run pytest tests/e2e/test_screenshots.py
```

**Stand-ins (same as every E2E test):** loopback Kafka, in-memory object store, an *extractive* answerer instead of a
real LLM (the answer text is an excerpt of the uploaded document), deterministic embeddings. Users and organizations are
synthetic. Nothing here is a production measurement.

| File | Shows |
|---|---|
| `01-login.png`, `02-login-error.png` | sign-in page; generic server error with reference id |
| `03-signup.png`, `04-signup-validation.png`, `05-signup-success-ask-empty.png` | create-organization page, field-level validation, first screen after a successful signup |
| `06-documents-empty.png`, `07-documents-ready.png` | empty state; uploaded document processed to "Ready" |
| `08-ask-answer-with-citation.png` | answered question with a source (title, version, section) and routing details expanded |
| `09-system-status.png` | readiness as the API reports it (Kafka shown as disabled in this stack) |
| `10-ask-abstains-for-other-tenant.png` | another organization asking the same question gets an abstention (tenant isolation) |
| `11-usage-and-budget.png` | admin usage and daily limits |
| `12-audit-verified-with-detail.png` | audit trail, "chain intact" result, event inspection panel |
| `13-sidebar-collapsed.png` | collapsed sidebar |
| `20-dark-login.png`, `21-dark-documents.png`, `22-dark-audit.png` | dark theme |
| `30-mobile-login.png`, `31-mobile-ask.png`, `32-mobile-menu-open.png` | 390 px phone layout and drawer |

Full-page captures of the signed-in app show the sidebar only as tall as the viewport; that is a property of the
screenshot, not of the page.
