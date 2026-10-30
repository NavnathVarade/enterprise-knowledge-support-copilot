# Portfolio summary

## One paragraph
A multi-tenant, permission-aware RAG platform for enterprise knowledge: documents are ingested through a transactional outbox and Kafka, indexed in Postgres/pgvector, and answered with citations. Authorization is enforced in the retrieval SQL itself, so content a user may not see never reaches the model. The project was built phase by phase with a strict rule: nothing is claimed without a command that produced it, and everything not run is labelled so.

## Scale of the work (counted from the repository)
| | |
|---|---|
| Python source | about 18,000 lines (`packages`, `apps`, `rag`, `data`, `scripts`, `migrations`) |
| Python tests | about 15,000 lines; **1,071 backend tests** passing (including 18 real-browser tests), plus 33 frontend tests (counts from the last full runs) |
| Frontend | React + TypeScript SPA, about 1,600 lines; strict typecheck, lint, 183 KB JS (59.6 KB gzip) |
| Infrastructure code | Docker, Docker Compose, Kustomize base + 2 overlays, Helm chart, OpenTofu/Terraform for AWS (4 modules, 2 environments), 3 GitHub Actions workflows |
| Documentation | about 60 markdown documents, 6 ADRs, generated config/API references, per-alert runbooks |

## What is genuinely proven (and where)
| Claim | Evidence |
|---|---|
| Cross-tenant and ACL isolation holds end to end, including through a real browser | `tests/integration/test_acl_aware_retrieval.py`, `tests/e2e/test_browser_flows.py` |
| Every API route rejects missing/forged tokens; a new unauthenticated route fails CI | `tests/integration/test_route_authentication_audit.py` |
| Tamper-evident audit log (hash chain, append-only triggers) detects edits and deletions | `tests/integration/test_dr_backup.py`, `docs/security/PHASE_44_SECURITY_AUDIT.md` |
| Backup/restore drill passes on a seeded database | `docs/disaster-recovery/results/drill_ekc_load.json` |
| Measured performance findings that changed the product | `docs/performance/PHASE_33_PERFORMANCE.md`, `docs/performance/PHASE_45_PERFORMANCE_AUDIT.md` |
| No import cycles; layering enforced | `scripts/architecture_check.py`, `tests/unit/test_architecture_fitness.py` |
| Docs cannot drift from code (paths, error codes, metrics, generated references) | `tests/unit/test_docs_integrity.py` |

## What is deliberately not claimed
It has **never run in production or against a real LLM, Kafka broker, S3 or cluster**. Docker images, the Helm chart, Terraform plans and the CI workflows were never executed (the build sandbox could not reach those registries). Recovery objectives and real-hardware performance are not measured. The production-readiness review's verdict is **no-go today**, with 11 listed blockers (`docs/operations/PHASE_43_PRODUCTION_READINESS.md`). Saying this plainly is part of the portfolio piece.

## Resume bullets that the evidence supports
- Designed and built a multi-tenant RAG platform (FastAPI, Postgres/pgvector, Kafka outbox pattern, React) with authorization enforced inside retrieval SQL; 1,000+ automated tests including real-browser end-to-end tests.
- Found and fixed a connection-pool deadlock on the query path through load testing (16 concurrent queries: 1.6 → 31 requests/s on a shared 2-core test machine, with stand-in model providers) and a 20–70× query-plan regression in vector retrieval.
- Built tamper-evident audit logging (hash chain + append-only triggers) with a verified backup/restore drill.
- Introduced executable architecture fitness checks after finding a five-package import cycle; wrote ADRs and generated reference docs that tests keep in sync with code.
- Authored a production-readiness review as executable checks plus a documented no-go verdict with evidence-backed blockers.

Do not add numbers that are not in these documents (users, QPS in production, cost savings): there are none.
