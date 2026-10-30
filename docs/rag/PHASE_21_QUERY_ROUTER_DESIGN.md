# Phase 21 — Query Router (Spec §12–13)

**Status: REFERENCE IMPLEMENTATION** — deterministic rules, no model call.

## Pipeline
`normalize → time-range extraction → entity extraction → rule scoring → combination rules → route selection → logged RoutingDecision`

| File | Role |
|---|---|
| `rag/routing/base.py` | `QueryRoute` (11 classes), `TimeRange`, `ExtractedEntities`, `RoutingDecision` |
| `rag/routing/normalize.py` | NFKC, control-char removal, whitespace collapse; empty or >2000 chars → `InvalidQuery` |
| `rag/routing/time_extraction.py` | current/latest, yesterday/today, last N days/weeks/months, this/last week/month/quarter/year, "in 2024", "Q3 2025", "March 2025" → half-open UTC ranges; `now` is injected |
| `rag/routing/entities.py` | version refs (`v3`, `version 4`), quoted terms, identifiers (`INC-1234`, `ERR-4021`) |
| `rag/routing/classifier.py` | weighted regex signals + compound rules + fixed tie-break priority |

## Decision record (spec §13)
Every decision carries `route`, `confidence`, `classifier_version` (`rules-v1`), `reason`, `latency_ms`, `matched_signals`, `fallback_used`, plus the time range and entities. The same fields are logged. Logs contain a 12-char query hash and length, **never the query text**.

## Rules worth knowing
- Compound `RAG_PLUS_SQL`: a metrics signal AND an explanation signal ("explain", "causes", …).
- "Compare incidents this month vs last month" is analytics, not a document diff: comparison needs documents/versions as the subject, or two version refs.
- Counting words ("how many") beat incident/support words, so "how many tickets…" is `SQL_ANALYTICS`.
- **Deterministic fallback:** no signal, or confidence < 0.34 → `HYBRID_SEARCH` (ACL-enforced retrieval is the safe general default), `fallback_used=true`. No alphabetic content → `UNKNOWN`.
- `confidence = top / (top + runner_up + 1)` is a **heuristic rule score, not a calibrated probability.**

## Security
The router only decides. It never executes a route or touches data; each executor must re-enforce tenant + ACL. `tenant_id` on the decision comes from the authenticated `Identity`, never from the query text (tested: a tenant id typed in the query is ignored). Routing never changes what a user is authorized to see.

## Tests (44, unit)
All 17 spec-style example queries, generic non-document compare, fallback, UNKNOWN, observability fields, tenant-from-identity, determinism, **log contains no query text**, normalization edge cases, every supported time form including year/quarter wraparounds (Jan→Dec, Q1→Q4), entities. Full suite: see README/CI.

## Not verified / not built (honest)
- **Routing accuracy is NOT YET MEASURED.** The tests prove the rules behave as designed on hand-written queries, not that they generalize. Phase 28 needs a labeled routing set (a good use of real support/incident questions).
- English only; keyword rules miss paraphrases and can be fooled (e.g. a query using "incident" in a non-incident sense).
- **Most routes have no executor yet.** `SQL_ANALYTICS`, `RAG_PLUS_SQL`, `INCIDENT_ANALYSIS`, `SUPPORT_ANALYSIS`, `ACCESS_CHECK` and `DOCUMENT_LOOKUP` are decisions only until the SQL layer, support/incident workflows and orchestrator are built.
- No query-rewriting, no entity-to-database resolution (service names, customer names), no LLM classifier, no relative-date handling beyond the forms above (e.g. "since the last release").
- Month ranges use 30 days for "last N months".
