# Phase 28 — Ingestion Pipeline: Object Storage, Outbox, Workers (Spec §48–52, §57, §83)

Before this phase an upload created a database row and nothing else happened: no stored
original, no event, no consumer. Now:

```
POST /documents ─┬─ put original → object store   (key derived: tenants/{t}/documents/{d}/{sha256})
                 └─ ONE DB txn: document + ACL + outbox(document.ingested)
outbox relay  ──► Kafka  document.ingested           (acked rows only; SKIP LOCKED; per-key order)
ingestion worker (consumer runner → handle_message → pipeline)
   claim(validating) → fetch+verify hash → validate → parse → chunk → embed   (no txn open)
   → ONE txn: version + chunks + embeddings + publish + lifecycle→active + outbox(completed)
```

## Components
| | File |
|---|---|
| Object store (Protocol, S3, in-memory), derived keys, error model | `packages/storage/object_store.py` |
| Outbox table / repo / relay | `models/outbox.py`, `migrations/versions/0005_outbox_events.py`, `repositories/outbox.py`, `packages/workers/outbox_relay.py` |
| Pipeline | `packages/ingestion/pipeline.py` |
| Polling loop (manual commits, bounded backoff, DLQ) | `packages/kafka/runner.py` |
| Entrypoints | `python -m packages.workers ingestion\|outbox-relay` |
| Event payload schemas | `packages/events/schemas.py` |

## Guarantees (each has a test)
- **No dual write.** The event is committed with the row, or neither is. A failed object put
  commits nothing.
- **At-least-once + idempotent.** Relay may duplicate; the Redis guard skips redelivered
  events, and a *new* event for already-indexed content is a no-op (`ALREADY_INDEXED`).
  The document row is locked during the write transaction.
- **Atomic indexing.** An embedding failure after parsing leaves zero versions/chunks and the
  document in `processing_failed` (retryable via the lifecycle).
- **Tenant safety.** The object key is derived, never read from the event; the stored bytes
  must hash to the recorded `content_hash` (mismatch ⇒ `quarantined` + failure event). An event
  naming another tenant's document gets `DocumentNotFound`.
- **Failure routing.** Retryable errors (storage/embedding outages) are re-raised for backoff
  and, if exhausted, the DLQ; permanent ones (`InvalidDocument`, `ParsingFailed`,
  `ObjectNotFound`, …) skip retries, move the document to a failed state and emit
  `document.processing.failed`.
- **Poison messages are no longer lost** (a Phase 8 gap): schema-invalid bytes go to the DLQ
  topic as a raw base64 record with the error and consumer group.
- **Offsets** are committed only after the final outcome, and after the DLQ record is flushed.

## Verified vs not verified
Verified here: all of the above against real Postgres and Redis; the S3 client's real boto3
code path against `moto` (SSE header, content type, missing key, oversize refusal, service
errors); the runner loop against a fake Kafka client.

**NOT verified (no broker, no real S3 in this environment):** the `confluent_kafka`
Consumer/Producer over the wire (rebalancing, cooperative-sticky assignment, real delivery
reports, ordering under broker failure), real S3/KMS/IAM behavior, and throughput.
`create_confluent_consumer` and `python -m packages.workers` are reviewed, not executed.
Throughput, lag and recovery time are `NOT YET MEASURED`.

## Known limits (explicit)
- Orphan objects remain if the DB commit fails after the put; no sweeper yet.
- A DLQ flush failure raises without committing, but the redelivery is then deduplicated, so
  that one DLQ record can be lost (alert on runner crash).
- Retries are in-process with backoff, blocking the partition meanwhile (preserves order, must
  stay under `max.poll.interval.ms`). The `.retry` topics are named but unused.
- Delivery is asynchronous: permanent failure of message N after N+1 was sent in the same relay
  batch can reorder that key (documented in the relay).
- Only the first upload of a document is wired. Uploading a new *version* of an existing
  document, re-ingestion, and deletion propagation (§71) are not built.
- No content sniffing / malware scanning (unchanged from Phase 7).
- The lifecycle states `embedding_failed` / `indexing_failed` are unused because indexing is atomic.
- No metrics yet (Prometheus/OTel is the next area), so lag and DLQ depth are not exported.

## Config
`OBJECT_STORE_BUCKET` (required in staging/production — startup fails otherwise),
`OBJECT_STORE_REGION|ENDPOINT_URL|KEY_PREFIX|SSE|KMS_KEY_ID`. Credentials via workload
identity, never in env files.
