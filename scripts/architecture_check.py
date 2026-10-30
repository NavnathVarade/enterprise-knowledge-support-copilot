"""Architecture fitness checks over the import graph (Phase 46).

Builds the module-level import graph of `packages`, `rag` and `apps` (imports executed at import
time, i.e. not inside functions and not under `if TYPE_CHECKING`), collapses it to the unit
"top-level package" (`packages.X`, `rag.X`, `apps.X`), and reports:

* strongly connected components with more than one member (import cycles between units);
* violations of the declared layering (`LAYERS` below: a unit may import only from its own layer
  or lower ones).

Lazy (in-function) imports are listed separately: they are how cycles are usually hidden, so a
reader can judge whether each is a deliberate cycle-breaker or a smell.

Usage: python scripts/architecture_check.py [--json PATH]   (exit 1 if any rule is violated)
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOTS = ("packages", "rag", "apps")

# Lower number = lower layer. A unit may import units of the same or a lower layer only.
LAYERS: dict[str, int] = {
    # 0 - leaves: configuration and cross-cutting primitives
    "packages.config": 0,
    "packages.domain": 0,
    "packages.events": 0,
    "packages.observability": 0,
    "packages.redis": 0,
    "packages.security": 0,
    # 1 - infrastructure adapters and persistence
    "packages.database": 1,
    "packages.auth": 1,
    "packages.storage": 1,
    "packages.kafka": 1,
    "packages.audit": 1,
    "packages.usage": 1,
    "packages.llm": 1,
    # 2 - pure RAG building blocks (no persistence of their own)
    "rag.parsing": 2,
    "rag.chunking": 2,
    "rag.embeddings": 2,
    "rag.reranking": 2,
    "rag.security": 2,
    "rag.routing": 2,
    "rag.ingestion": 2,
    # 3 - RAG stages that use persistence
    "rag.retrieval": 3,
    "rag.context": 3,
    "rag.citations": 3,
    "rag.generation": 4,
    "rag.comparison": 5,
    # 4 - application services
    "packages.analytics": 4,
    "packages.investigation": 4,
    "packages.ingestion": 5,
    "packages.orchestration": 6,
    "packages.providers": 6,
    "rag.evaluation": 7,
    # 5 - entry points
    "packages.workers": 8,
    "apps.api": 8,
}


def _is_type_checking(node: ast.If) -> bool:
    t = node.test
    return (isinstance(t, ast.Name) and t.id == "TYPE_CHECKING") or (
        isinstance(t, ast.Attribute) and t.attr == "TYPE_CHECKING"
    )


def _unit(module: str) -> str | None:
    parts = module.split(".")
    return ".".join(parts[:2]) if parts[0] in SOURCE_ROOTS and len(parts) >= 2 else None


def _imports(tree: ast.Module, eager_only: bool) -> set[str]:
    found: set[str] = set()

    def visit(nodes: list[ast.stmt], lazy: bool) -> None:
        for n in nodes:
            if isinstance(n, ast.If) and _is_type_checking(n):
                continue  # typing-only; no runtime edge
            if isinstance(n, ast.Import | ast.ImportFrom) and lazy != eager_only:
                names = (
                    [a.name for a in n.names]
                    if isinstance(n, ast.Import)
                    else [n.module or ""]
                    if not n.level
                    else []
                )
                found.update(names)
            for field in ("body", "orelse", "finalbody", "handlers"):
                for child in getattr(n, field, []) or []:
                    if isinstance(child, ast.stmt):
                        visit(
                            [child], lazy or isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
                        )
                    elif isinstance(child, ast.ExceptHandler):
                        visit(child.body, lazy)

    visit(tree.body, False)
    return found


def build_graph(*, eager: bool) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for root in SOURCE_ROOTS:
        for f in (ROOT / root).rglob("*.py"):
            rel = f.relative_to(ROOT).with_suffix("")
            src = _unit(".".join(rel.parts))
            if src is None:
                continue
            for mod in _imports(ast.parse(f.read_text()), eager_only=eager):
                dst = _unit(mod)
                if dst and dst != src:
                    graph[src].add(dst)
    return graph


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan strongly connected components with more than one node."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    counter = 0

    def strong(v: str) -> None:
        nonlocal counter
        index[v] = low[v] = counter
        counter += 1
        stack.append(v)
        on.add(v)
        for w in sorted(graph.get(v, ())):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(comp))

    for v in sorted(set(graph) | {w for ws in graph.values() for w in ws}):
        if v not in index:
            strong(v)
    return sorted(out)


def layer_violations(graph: dict[str, set[str]]) -> list[str]:
    bad = []
    for src, dsts in sorted(graph.items()):
        for dst in sorted(dsts):
            if src in LAYERS and dst in LAYERS and LAYERS[dst] > LAYERS[src]:
                bad.append(f"{src} (layer {LAYERS[src]}) imports {dst} (layer {LAYERS[dst]})")
    return bad


def unknown_units(graph: dict[str, set[str]]) -> list[str]:
    seen = set(graph) | {w for ws in graph.values() for w in ws}
    return sorted(u for u in seen if u not in LAYERS)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    eager = build_graph(eager=True)
    lazy = build_graph(eager=False)
    report = {
        "cycles": cycles(eager),
        "layer_violations": layer_violations(eager),
        "unclassified_units": unknown_units(eager),
        "lazy_imports": {
            k: sorted(v - eager.get(k, set()))
            for k, v in sorted(lazy.items())
            if v - eager.get(k, set())
        },
    }
    for k, v in report.items():
        print(k, json.dumps(v, indent=1) if v else "none")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n")
    return (
        1 if (report["cycles"] or report["layer_violations"] or report["unclassified_units"]) else 0
    )


if __name__ == "__main__":
    sys.exit(main())
