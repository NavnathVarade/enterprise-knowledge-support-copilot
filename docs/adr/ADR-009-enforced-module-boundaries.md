# ADR-009: Module boundaries are enforced by a test, not by convention

## Context
ADR-002 chose a modular monolith and said boundaries would hold "by convention". The Phase 46 audit found the convention had already failed: `packages.domain` imported `packages.database`, the database layer imported `rag.chunking`, and `rag.citations` and `rag.generation` imported each other, forming import cycles across five units.

## Decision
`scripts/architecture_check.py` declares a layer for every unit (`packages.X`, `rag.X`, `apps.X`), builds the runtime import graph (excluding `TYPE_CHECKING`), and fails on cycles, upward imports and unclassified units. `tests/unit/test_architecture_fitness.py` runs it and also pins the short list of reviewed lazy imports. The cycles were removed by moving shared value types down: `DocumentStatus` and `Chunk` into `packages.domain`, `SourceRef` into `rag.citations`.

## Alternatives considered
- Keep the convention: already shown not to work.
- import-linter or similar: reasonable; a small in-repo script avoids a dependency and is tested directly.

## Consequences
- New packages must be added to `LAYERS` (the test says so).
- The check is at unit granularity; it does not police imports inside a unit.
- Lazy imports are the escape hatch; the test lists them so each one is a deliberate review.

## Status
Accepted — Phase 46.
