# Documentation index

Start with the root `README.md` (overview and the status table of what is verified and what is not) and `HOW_TO_RUN.md` (every command, in order, for development and production). Documents are grouped by what you are trying to do. Files under `docs/*/results/` are raw measurement output referenced by the documents next to them.

## Understand the system
- `docs/architecture/PHASE_46_ARCHITECTURE_AS_BUILT.md` - the architecture as built: deployables, layers, flows, deviations from the original plan
- `docs/architecture/PHASE_0_ARCHITECTURE.md` - original design intent (service boundaries amended in Phase 46)
- `docs/adr/ADR-001-pgvector-in-postgres.md`, `docs/adr/ADR-002-modular-monolith.md`, `docs/adr/ADR-003-authorization-in-the-query.md`, `docs/adr/ADR-004-outbox-and-at-least-once-kafka.md`, `docs/adr/ADR-007-template-sql-with-read-only-role.md`, `docs/adr/ADR-009-enforced-module-boundaries.md`, `docs/adr/ADR-010-uv-for-dependency-management.md`, `docs/adr/ADR-011-environment-aware-configuration.md` (development without Kafka, production with it), `docs/adr/ADR-012-self-service-signup.md` (gated organization signup) - decisions and their consequences
- `docs/database/PHASE_3_DATABASE_DESIGN.md` - schema
- `docs/data/PHASE_6_SYNTHETIC_DATASET.md` - the synthetic dataset (no real data is used)

## Pipeline and features (design per phase)
- Ingestion: `docs/rag/PHASE_5_DOCUMENT_DOMAIN_DESIGN.md`, `docs/rag/PHASE_7_INGESTION_DESIGN.md`, `docs/rag/PHASE_9_PARSING_DESIGN.md`, `docs/rag/PHASE_10_CHUNKING_DESIGN.md`, `docs/rag/PHASE_11_EMBEDDING_DESIGN.md`, `docs/ingestion/PHASE_28_INGESTION_PIPELINE_DESIGN.md`
- Events: `docs/kafka/PHASE_8_KAFKA_DESIGN.md`
- Retrieval: `docs/retrieval/PHASE_13_LEXICAL_SEARCH_DESIGN.md`, `docs/retrieval/PHASE_14_HYBRID_RETRIEVAL_DESIGN.md`, `docs/retrieval/PHASE_15_RERANKING_DESIGN.md`, `docs/retrieval/PHASE_16_ACL_AWARE_RETRIEVAL_DESIGN.md`, `docs/retrieval/PHASE_17_TEMPORAL_RETRIEVAL_DESIGN.md`
- Answering: `docs/rag/PHASE_18_CONTEXT_CONSTRUCTION_DESIGN.md`, `docs/llm/PHASE_19_LLM_GATEWAY_DESIGN.md`, `docs/rag/PHASE_20_ANSWER_GENERATION_AND_CITATIONS_DESIGN.md`, `docs/rag/PHASE_21_QUERY_ROUTER_DESIGN.md`, `docs/orchestration/PHASE_25_QUERY_ORCHESTRATION_DESIGN.md`, `docs/rag/PHASE_26_DOCUMENT_COMPARISON_DESIGN.md`
- Analytics and support: `docs/analytics/PHASE_22_SQL_ANALYTICS_DESIGN.md`, `docs/investigation/PHASE_23_SUPPORT_AND_INCIDENT_INVESTIGATION_DESIGN.md`
- Evaluation: `docs/evaluation/PHASE_27_EVALUATION_FRAMEWORK_DESIGN.md`
- Frontend: `docs/frontend/PHASE_41_FRONTEND.md` (includes the Phase 51 redesign and signup); screenshots: `docs/screenshots/README.md`

## Security
- `docs/security/THREAT_MODEL.md`, `docs/security/PHASE_4_AUTHORIZATION_DESIGN.md`, `docs/security/PHASE_24_PROMPT_INJECTION_DESIGN.md`, `docs/security/PHASE_32_SECURITY_HARDENING.md`, `docs/security/PHASE_44_SECURITY_AUDIT.md` (audit, residual risks)
- Root: `SECURITY.md` (reporting), `CONTRIBUTING.md`

## Deploy and operate
- Deploy: `docs/deployment/PHASE_30_DEPLOYMENT_DESIGN.md`, `docs/deployment/DOCKER.md`, `docs/deployment/KUBERNETES.md`, `docs/deployment/HELM.md`, `docs/deployment/TERRAFORM.md`, `docs/deployment/CICD.md`
- Go-live gate: `docs/operations/PHASE_43_PRODUCTION_READINESS.md` (verdict and blockers)
- Run: `docs/operations/PHASE_29_OBSERVABILITY.md` (metrics, alerts, per-alert runbooks), `docs/operations/PHASE_31_COST_MONITORING.md`, `docs/troubleshooting/TROUBLESHOOTING.md` (symptom-first)
- Recover: `docs/disaster-recovery/PHASE_40_DISASTER_RECOVERY.md`, `docs/resilience/PHASE_34_RESILIENCE.md`
- Performance: `docs/performance/PHASE_33_PERFORMANCE.md`, `docs/performance/PHASE_45_PERFORMANCE_AUDIT.md`

## Reference (generated from code; checked in CI)
- `docs/reference/CONFIGURATION.md` - every environment variable
- `docs/reference/API.md` - every HTTP operation and body
- Regenerate with `python scripts/gen_reference_docs.py`

## Testing
- `tests/README.md` - how the suites are organised and run
- `docs/testing/PHASE_42_E2E.md` - browser end-to-end tests and their stand-ins

## Portfolio and interviews
- `docs/interview/PORTFOLIO_SUMMARY.md` - what the project is, what is proven, what is not claimed, evidence-backed resume bullets
- `docs/interview/INTERVIEW_GUIDE.md` - stories with evidence, design Q&A, what not to say
- `docs/interview/DEMO_SCRIPT.md` - five-minute demo on the real stack with stand-in providers
- `docs/system-design/SYSTEM_DESIGN_WALKTHROUGH.md` - 45-minute whiteboard script with measured numbers labelled
