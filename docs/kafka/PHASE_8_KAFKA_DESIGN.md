# Kafka Event Infrastructure — Phase 8

> **Update (Phase 50):** Kafka is mandatory in staging/production and optional in development (`KAFKA_ENABLED`). Only the two workers and the reachability probes build Kafka clients; the API never does. See `docs/adr/ADR-011-environment-aware-configuration.md`.

## Scope delivered
- `packages/events/schemas.py` — the `EventEnvelope` every message uses, with the
  `event_id`/`correlation_id`/`trace_id` fields Section 46 requires
- `packages/kafka/topics.py` — topic catalog, partition-key derivation, retry/DLQ topic
  naming, consumer group naming
- `packages/kafka/retry.py` — bounded exponential backoff with jitter, retryable-error
  classification, and the `DLQEnvelope` shape (Section 47)
- `packages/kafka/idempotency.py` — Redis-backed idempotency guard (Section 46)
- `packages/kafka/producer.py` — `EventProducer`, with real idempotent-producer config for
  `confluent_kafka.Producer` construction
- `packages/kafka/consumer.py` — `handle_message`, the full per-message decision logic:
  parse → idempotency claim → handle → retry-or-DLQ

## The honest gap: no live Kafka broker in this environment
This sandbox has real, persistent Postgres and Redis (installed via `apt` in earlier
phases), but **no Kafka broker**. Apache Kafka's distribution isn't reachable through this
environment's allowed network (package registries and GitHub only), and Redpanda's GitHub
releases only publish the `rpk` client CLI, not a server binary, through channels this
environment can reach. This is architecturally the same category of gap as Kubernetes and
Terraform in earlier phases: real, reviewed code that cannot be executed against live
infrastructure here.

**What was done about it, concretely:** every module in this phase was deliberately
factored so its actual *logic* — not just its interface — is testable without a broker
socket:
- `EventProducer` depends on a `KafkaProducerClient` Protocol, not `confluent_kafka.Producer`
  directly, so `tests/unit/test_kafka_producer.py` exercises the real serialization/keying
  logic against a fake client
- `IdempotencyGuard` is backed by Redis, which **is** real and running here —
  `tests/integration/test_kafka_idempotency.py` proves Section 46's exact requirement
  ("same event twice must not cause duplicate side effects") against a real Redis, not a mock
- `handle_message` — the actual per-message decision logic (parse, dedup, retry-vs-DLQ) — is
  a plain async function taking raw bytes and a handler callback, with no broker dependency
  at all. `tests/integration/test_kafka_consumer_decision_logic.py` drives it through all
  five real outcomes (processed, duplicate-skipped, retry, dead-lettered, poison-message)
  using the real Redis-backed guard and a fake DLQ producer

**What is genuinely NOT verified**: an actual `confluent_kafka.Consumer`/`Producer` talking
to a real broker over the wire — connection handling, partition assignment, consumer-group
rebalancing, real message ordering guarantees, and real network-failure behavior are all
`NOT YET MEASURED`. `create_confluent_producer` constructs a real client with the correct
config but has no test exercising an actual connection.

## Real defects found and fixed during this phase
None functionally — this phase built cleanly on the existing config/logging/error/Redis
infrastructure without needing changes to it. Two real mypy type errors were caught and
fixed (a `redis.set()` return-type narrowing issue in the idempotency guard, and an
`Any`-leak from `random.uniform()` in the backoff calculation) — strict-typing correctness
issues, not behavioral bugs, but listed here since the phase cycle treats a failing
type-check the same as a failing test.

## Not yet implemented (explicitly, not silently)
- No actual `confluent_kafka.Consumer` polling loop wired to `handle_message` — the function
  exists and is fully tested, but nothing calls it from a real consumer loop yet, since
  there's no worker service (Phase 10+) to run one in.
- Poison messages (schema-invalid bytes) are logged and marked dead-lettered but NOT actually
  published to a DLQ topic — see the comment in `packages/kafka/consumer.py`: a malformed
  message has no valid `EventEnvelope` to wrap in a `DLQEnvelope`, and this phase chose not
  to paper over that with placeholder field values.
- No outbox pattern yet (Section 57) — `POST /documents` (Phase 7) writes to Postgres only;
  it does not yet publish a `document.ingested` event, since there's no consumer for that
  event yet either. Wiring ingestion to Kafka is Phase 9's job.
- Kafka connection/auth (`security.protocol=SASL_SSL` in the production config template) is
  configured but never exercised against a real broker in tests.


## Update (Phase 28)
The consumer polling loop (`packages/kafka/runner.py`), the outbox pattern and the
ingestion worker now exist, and poison messages are published to the DLQ topic as raw
records. See `docs/ingestion/PHASE_28_INGESTION_PIPELINE_DESIGN.md`. The broker-level
verification gap above is unchanged.
