"""
Evaluation dataset schemas and strict JSONL loaders (Spec §28).

Loaders fail loudly (line number + reason) on malformed rows, unknown routes, duplicate
case ids, or empty label sets: a silently-skipped row would change the denominator of
every reported metric.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from rag.routing.base import QueryRoute


class DatasetError(ValueError):
    pass


@dataclass(frozen=True)
class RoutingCase:
    id: str
    query: str
    expected_route: QueryRoute
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorpusDocument:
    key: str  # stable id used by retrieval labels
    title: str
    text: str
    restricted: bool = False  # no ACL entry -> must never be retrievable


@dataclass(frozen=True)
class RetrievalCase:
    id: str
    query: str
    relevant_docs: frozenset[str]  # CorpusDocument.key values
    forbidden_docs: frozenset[str] = frozenset()  # must NOT appear (leakage check)


@dataclass(frozen=True)
class AnswerCase:
    id: str
    query: str
    answerable: bool
    expected_facts: tuple[str, ...] = ()  # substrings that a correct answer must contain
    relevant_docs: frozenset[str] = frozenset()


def _rows(path: Path) -> list[tuple[int, dict[str, object]]]:
    rows: list[tuple[int, dict[str, object]]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{path.name}:{n}: invalid JSON ({exc.msg})") from exc
        if not isinstance(obj, dict):
            raise DatasetError(f"{path.name}:{n}: each line must be a JSON object")
        rows.append((n, obj))
    if not rows:
        raise DatasetError(f"{path.name}: no cases")
    return rows


def _str(path: Path, n: int, obj: dict[str, object], key: str) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        raise DatasetError(f"{path.name}:{n}: '{key}' must be a non-empty string")
    return v


def _str_list(path: Path, n: int, obj: dict[str, object], key: str, *, required: bool) -> list[str]:
    v = obj.get(key, [] if not required else None)
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise DatasetError(f"{path.name}:{n}: '{key}' must be a list of non-empty strings")
    if required and not v:
        raise DatasetError(f"{path.name}:{n}: '{key}' must not be empty")
    return v


def _unique(path: Path, ids: list[str]) -> None:
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise DatasetError(f"{path.name}: duplicate case ids {sorted(dupes)}")


def load_routing_cases(path: Path) -> list[RoutingCase]:
    out: list[RoutingCase] = []
    for n, obj in _rows(path):
        route = _str(path, n, obj, "expected_route")
        try:
            expected = QueryRoute(route)
        except ValueError as exc:
            raise DatasetError(f"{path.name}:{n}: unknown route '{route}'") from exc
        out.append(
            RoutingCase(
                _str(path, n, obj, "id"),
                _str(path, n, obj, "query"),
                expected,
                tuple(_str_list(path, n, obj, "roles", required=False)),
            )
        )
    _unique(path, [c.id for c in out])
    return out


def load_corpus(path: Path) -> list[CorpusDocument]:
    out = [
        CorpusDocument(
            _str(path, n, o, "key"),
            _str(path, n, o, "title"),
            _str(path, n, o, "text"),
            bool(o.get("restricted", False)),
        )
        for n, o in _rows(path)
    ]
    keys = [d.key for d in out]
    if len(set(keys)) != len(keys):
        raise DatasetError(f"{path.name}: duplicate document keys")
    return out


def load_retrieval_cases(path: Path, corpus: list[CorpusDocument]) -> list[RetrievalCase]:
    known = {d.key for d in corpus}
    out: list[RetrievalCase] = []
    for n, o in _rows(path):
        forbidden = frozenset(_str_list(path, n, o, "forbidden_docs", required=False))
        # A leakage-only case (forbidden docs, no relevant docs) is allowed: it asserts
        # that restricted content never appears, and contributes no ranking metric.
        rel = frozenset(_str_list(path, n, o, "relevant_docs", required=not forbidden))
        unknown = (rel | forbidden) - known
        if unknown:
            raise DatasetError(f"{path.name}:{n}: unknown document keys {sorted(unknown)}")
        if rel & forbidden:
            raise DatasetError(f"{path.name}:{n}: a document cannot be relevant and forbidden")
        out.append(RetrievalCase(_str(path, n, o, "id"), _str(path, n, o, "query"), rel, forbidden))
    _unique(path, [c.id for c in out])
    return out


def load_answer_cases(path: Path, corpus: list[CorpusDocument]) -> list[AnswerCase]:
    known = {d.key for d in corpus}
    out: list[AnswerCase] = []
    for n, o in _rows(path):
        answerable = o.get("answerable")
        if not isinstance(answerable, bool):
            raise DatasetError(f"{path.name}:{n}: 'answerable' must be true or false")
        facts = _str_list(path, n, o, "expected_facts", required=answerable)
        rel = frozenset(_str_list(path, n, o, "relevant_docs", required=answerable))
        if not answerable and (facts or rel):
            raise DatasetError(f"{path.name}:{n}: unanswerable case must not list facts/docs")
        unknown = rel - known
        if unknown:
            raise DatasetError(f"{path.name}:{n}: unknown document keys {sorted(unknown)}")
        out.append(
            AnswerCase(
                _str(path, n, o, "id"), _str(path, n, o, "query"), answerable, tuple(facts), rel
            )
        )
    _unique(path, [c.id for c in out])
    return out
