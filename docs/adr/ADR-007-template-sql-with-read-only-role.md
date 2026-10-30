# ADR-007: SQL analytics through validated intents on a read-only role, not text-to-SQL

## Context
Analytics questions need numbers from structured tables without letting a model write arbitrary SQL.

## Decision
The model only translates a question into a JSON *intent* over a fixed catalogue of entities; the platform renders parameterised SQL from templates and runs it under a dedicated read-only role (`SET LOCAL ROLE`, identifier-validated) with a statement timeout. Unsupported intents return "not supported".

## Alternatives considered
- Free-form text-to-SQL: flexible, but injection, cost and cross-tenant risks are hard to bound.

## Consequences
- Safe and testable, but only as capable as the template catalogue.
- Analytics is disabled when no read-only DSN is configured (as in the E2E stack).

## Status
Accepted — Phase 22.
