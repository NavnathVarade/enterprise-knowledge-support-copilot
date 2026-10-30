# ADR-004: Transactional outbox, at-least-once Kafka, idempotent consumers

## Context
Document state changes and the events announcing them must not diverge, and a broker outage must not lose or block uploads.

## Decision
Events are written to an `outbox_events` table in the same transaction as the state change; a relay publishes them to Kafka (at-least-once) and marks them published. Consumers are idempotent and route poison messages to a DLQ; `DocumentNotFound`/`ObjectNotFound` are non-retryable.

## Alternatives considered
- Publish to Kafka directly from the request: a crash between commit and publish loses events (or vice versa).
- Kafka transactions / exactly-once: more moving parts and still needs idempotent consumers.
- Change-data-capture (Debezium): robust but an extra system to run.

## Consequences
- Duplicates are normal and must be tolerated; ordering is per key only.
- The relay is a single replica (Recreate) so event order per row is preserved; a gap in its availability delays events but loses none (outbox backlog alert exists).
- **Never exercised against a real broker** in this repository: the relay/consumer logic is tested with in-process stand-ins and the E2E loopback.

## Status
Accepted — Phase 8.
