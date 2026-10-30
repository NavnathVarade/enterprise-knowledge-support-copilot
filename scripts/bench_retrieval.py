"""
Retrieval benchmark against a real Postgres + pgvector (Spec §119, §120).

What it measures: the ACL-aware, temporally-correct retrieval queries the product actually
runs (`find_nearest_chunks_authorized`, `search_chunks_lexical_authorized`,
`hybrid_search_authorized`) over a synthetic corpus, plus the recall of the HNSW vector leg
against exact (sequential-scan) search under the same ACL filter.

What it does NOT measure: embedding or LLM provider latency, network hops, API overhead,
concurrency across many clients, or real-text/real-embedding distributions. Vectors are
synthetic (a Gaussian mixture), so recall figures here describe THIS data, not production.

Safety: refuses to run unless the database name ends in `_bench` (it bulk-inserts and the
tables are left populated), so it cannot touch a real database by accident.

Usage:
    DATABASE_URL=postgresql+asyncpg://.../ekc_bench PYTHONPATH=. \
        python -m scripts.bench_retrieval --chunks 10000 --tenants 10 --out result.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import statistics
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from packages.auth.identity import Identity
from packages.config.settings import get_settings
from packages.database.repositories.embeddings import find_nearest_chunks_authorized
from packages.database.repositories.lexical_search import search_chunks_lexical_authorized
from packages.database.session import get_sessionmaker
from rag.retrieval.hybrid import hybrid_search_authorized
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DIM = 1024
CHUNKS_PER_DOC = 10
CLUSTERS = 200
VOCAB = 3000
WORDS_PER_CHUNK = 70
RESTRICTED_FRACTION = 0.2  # share of documents visible only to the 'finance' department


def _percentiles(samples_ms: list[float]) -> dict[str, float]:
    s = sorted(samples_ms)

    def pct(p: float) -> float:
        return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]

    return {
        "n": float(len(s)),
        "p50_ms": round(pct(50), 2),
        "p95_ms": round(pct(95), 2),
        "p99_ms": round(pct(99), 2),
        "mean_ms": round(statistics.fmean(s), 2),
        "max_ms": round(s[-1], 2),
    }


def _vec_literal(v: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.5f}" for x in v) + "]"


class _FixedQueryEmbedder:
    """Returns a pre-chosen vector per query text, so the real hybrid function can be timed
    without a provider. Provider latency is therefore excluded by construction."""

    model_name = "bench-fixed"
    dimensions = DIM

    def __init__(self) -> None:
        self.vectors: dict[str, list[float]] = {}

    async def embed_query(self, query: str) -> list[float]:
        return self.vectors[query]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        raise NotImplementedError


def _hardware(pg: dict[str, str]) -> dict[str, Any]:
    mem_kb = 0
    cpu = "unknown"
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                mem_kb = int(line.split()[1])
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {
        "cpu": cpu,
        "logical_cores": os.cpu_count(),
        "memory_gb": round(mem_kb / 1024 / 1024, 1),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "note": "Client, Postgres and Redis share this one machine; numbers are not a "
        "production-hardware prediction.",
        **pg,
    }


async def _scalar(session: AsyncSession, sql: str) -> str:
    return str((await session.execute(text(sql))).scalar_one())


async def _load(
    session: AsyncSession, *, chunks: int, tenants: int, seed: int
) -> tuple[list[uuid.UUID], np.ndarray, list[str], float]:
    """Bulk-load a deterministic corpus. Returns (tenant_ids, centroids, vocab, seconds)."""
    t0 = time.perf_counter()
    rng = np.random.default_rng(seed)
    centroids = rng.standard_normal((CLUSTERS, DIM)).astype(np.float32)
    centroids /= np.linalg.norm(centroids, axis=1, keepdims=True)
    vocab = [f"w{i}x" for i in range(VOCAB)]
    zipf = 1.0 / np.arange(1, VOCAB + 1)
    zipf /= zipf.sum()

    tenant_ids = [uuid.uuid4() for _ in range(tenants)]
    for i, tid in enumerate(tenant_ids):
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug, status, created_at, updated_at) "
                "VALUES (:i, :n, :s, 'active', now(), now())"
            ),
            {"i": tid, "n": f"bench-{i}", "s": f"bench-{i}-{uuid.uuid4().hex[:6]}"},
        )
    await session.commit()

    docs_per_tenant = max(1, chunks // tenants // CHUNKS_PER_DOC)
    for tid in tenant_ids:
        doc_ids = [uuid.uuid4() for _ in range(docs_per_tenant)]
        ver_ids = [uuid.uuid4() for _ in range(docs_per_tenant)]
        restricted = rng.random(docs_per_tenant) < RESTRICTED_FRACTION
        await session.execute(
            text(
                "INSERT INTO documents (id, tenant_id, source_type, title, mime_type, content_hash,"
                " status, security_classification, doc_metadata, created_at, updated_at) "
                "SELECT d, :t, 'upload', 'bench ' || d::text, 'text/plain', repeat('a', 64),"
                " 'active', 'INTERNAL', '{}'::jsonb, now(), now() FROM unnest(CAST(:ids AS uuid[])) d"
            ),
            {"t": tid, "ids": doc_ids},
        )
        await session.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, document_id, version, status,"
                " content_hash, effective_from, published_at, created_at, updated_at) "
                "SELECT v, :t, d, 1, 'published', repeat('b', 64), now() - interval '1 day',"
                " now(), now(), now() "
                "FROM unnest(CAST(:vids AS uuid[]), CAST(:dids AS uuid[])) AS x(v, d)"
            ),
            {"t": tid, "vids": ver_ids, "dids": doc_ids},
        )
        for rows, vis in (
            (
                [d for d, r in zip(doc_ids, restricted, strict=True) if not r],
                "public_within_tenant",
            ),
            ([d for d, r in zip(doc_ids, restricted, strict=True) if r], "department_only"),
        ):
            if not rows:
                continue
            await session.execute(
                text(
                    "INSERT INTO document_acl (id, tenant_id, document_id, visibility, department,"
                    " created_at, updated_at) "
                    "SELECT gen_random_uuid(), :t, d, CAST(:v AS varchar), CAST(:dep AS varchar), now(), now() FROM unnest(CAST(:ids AS uuid[])) d"
                ),
                {
                    "t": tid,
                    "v": vis,
                    "dep": "finance" if vis == "department_only" else None,
                    "ids": rows,
                },
            )
        await session.commit()

        # Chunks and embeddings in batches.
        batch = 2000
        total = docs_per_tenant * CHUNKS_PER_DOC
        for start in range(0, total, batch):
            n = min(batch, total - start)
            chunk_ids = [uuid.uuid4() for _ in range(n)]
            cl = rng.integers(0, CLUSTERS, size=n)
            vecs = centroids[cl] + (0.5 / np.sqrt(DIM)) * rng.standard_normal((n, DIM)).astype(
                np.float32
            )
            vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
            texts = [
                " ".join(vocab[j] for j in rng.choice(VOCAB, size=WORDS_PER_CHUNK, p=zipf))
                for _ in range(n)
            ]
            await session.execute(
                text(
                    "INSERT INTO document_chunks (id, tenant_id, document_version_id, sequence,"
                    " text, token_count, char_start, char_end, content_hash, chunking_version,"
                    " chunk_metadata, created_at, updated_at) "
                    "SELECT c, :t, v, (:s + o)::int, tx, 90, 0, length(tx), repeat('c', 64), 'bench',"
                    " '{}'::jsonb, now(), now() "
                    "FROM unnest(CAST(:cids AS uuid[]), CAST(:vids AS uuid[]), CAST(:txs AS text[]),"
                    " CAST(:ords AS int[])) AS x(c, v, tx, o)"
                ),
                {
                    "t": tid,
                    "s": 0,
                    "cids": chunk_ids,
                    "vids": [ver_ids[(start + k) // CHUNKS_PER_DOC] for k in range(n)],
                    "txs": texts,
                    "ords": [(start + k) for k in range(n)],
                },
            )
            await session.execute(
                text(
                    "INSERT INTO embeddings (id, tenant_id, chunk_id, embedding, embedding_model,"
                    " embedding_version, provider, created_at, updated_at) "
                    "SELECT gen_random_uuid(), :t, c, CAST(e AS vector), 'bench', 'v1', 'bench',"
                    " now(), now() FROM unnest(CAST(:cids AS uuid[]), CAST(:vecs AS text[]))"
                    " AS x(c, e)"
                ),
                {"t": tid, "cids": chunk_ids, "vecs": [_vec_literal(v) for v in vecs]},
            )
            await session.commit()
    await session.execute(text("ANALYZE"))
    await session.commit()
    return tenant_ids, centroids, vocab, time.perf_counter() - t0


async def _exact_ids(
    session: AsyncSession, tenant_id: uuid.UUID, identity: Identity, qv: list[float], k: int
) -> list[uuid.UUID]:
    """Ground truth: same ACL-filtered query with index scans disabled (exact scan)."""
    await session.execute(text("SET LOCAL enable_indexscan = off"))
    await session.execute(text("SET LOCAL enable_bitmapscan = off"))
    rows = await find_nearest_chunks_authorized(
        session, tenant_id=tenant_id, identity=identity, query_vector=qv, limit=k
    )
    ids = [e.chunk_id for e, _ in rows]  # read before rollback expires the ORM objects
    await session.rollback()  # discards the SET LOCALs
    return ids


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    if not str(settings.database.url).rsplit("/", 1)[-1].split("?")[0].endswith("_bench"):
        raise SystemExit("Refusing: database name must end in '_bench'.")
    sessionmaker = get_sessionmaker(settings.database)
    rng = np.random.default_rng(args.seed + 1)

    async with sessionmaker() as session:
        pg = {
            "postgres": (await _scalar(session, "show server_version")),
            "pgvector": await _scalar(
                session, "select extversion from pg_extension where extname='vector'"
            ),
            "shared_buffers": await _scalar(session, "show shared_buffers"),
        }
        existing = int(await _scalar(session, "select count(*) from document_chunks"))
        if args.reuse:
            # Re-measure an already loaded database. Centroids/vocab are deterministic in --seed.
            r0 = np.random.default_rng(args.seed)
            centroids = r0.standard_normal((CLUSTERS, DIM)).astype(np.float32)
            centroids /= np.linalg.norm(centroids, axis=1, keepdims=True)
            vocab = [f"w{i}x" for i in range(VOCAB)]
            tenant_ids = [
                r[0]
                for r in await session.execute(
                    text("select id from tenants where name like 'bench-%' order by name")
                )
            ]
            load_s = float("nan")
        else:
            if existing:
                raise SystemExit("Refusing: bench database is not empty; recreate it.")
            tenant_ids, centroids, vocab, load_s = await _load(
                session, chunks=args.chunks, tenants=args.tenants, seed=args.seed
            )
        total_chunks = int(await _scalar(session, "select count(*) from document_chunks"))
        tenant_chunks = int(
            (
                await session.execute(
                    text("select count(*) from document_chunks where tenant_id = :t"),
                    {"t": tenant_ids[0]},
                )
            ).scalar_one()
        )
        index_mb = round(
            int(await _scalar(session, "select pg_relation_size('ix_embeddings_embedding_hnsw')"))
            / 1e6,
            1,
        )
        table_mb = round(
            int(await _scalar(session, "select pg_total_relation_size('document_chunks')")) / 1e6, 1
        ) + round(
            int(await _scalar(session, "select pg_total_relation_size('embeddings')")) / 1e6, 1
        )

    tenant = tenant_ids[0]
    identity = Identity(
        user_id=uuid.uuid4(),
        tenant_id=tenant,
        email="bench@example.invalid",
        roles=frozenset(),
        groups=frozenset(),
        department="engineering",
    )
    queries: list[tuple[list[float], str]] = []
    for _ in range(args.queries):
        c = centroids[rng.integers(0, CLUSTERS)]
        q = c + 0.05 * rng.standard_normal(DIM).astype(np.float32)
        q /= np.linalg.norm(q)
        words = " ".join(vocab[j] for j in rng.integers(0, 60, size=3))
        queries.append((q.tolist(), words))

    out: dict[str, Any] = {}
    k = args.k
    extra = [
        tuple(x.split("=", 1)) for x in args.session_setting
    ]  # e.g. hnsw.iterative_scan=relaxed_order

    async def timed(fn: Any) -> list[float]:
        samples: list[float] = []
        async with sessionmaker() as session:
            for qv, words in queries[:5]:  # warm-up, discarded
                await fn(session, qv, words)
            for qv, words in queries:
                t = time.perf_counter()
                await fn(session, qv, words)
                samples.append((time.perf_counter() - t) * 1000)
        return samples

    # Vector leg: latency and recall at several ef_search values.
    vec: dict[str, Any] = {}
    for ef in args.ef_search:

        async def vector_leg(
            session: AsyncSession, qv: list[float], _w: str, _ef: int = ef
        ) -> list[Any]:
            await session.execute(text(f"SET hnsw.ef_search = {int(_ef)}"))
            for name, value in extra:
                await session.execute(text(f"SET {name} = {value}"))
            return await find_nearest_chunks_authorized(
                session, tenant_id=tenant, identity=identity, query_vector=qv, limit=k
            )

        lat = await timed(vector_leg)
        recalls: list[float] = []
        returned: list[int] = []
        async with sessionmaker() as session:
            await session.execute(text(f"SET hnsw.ef_search = {int(ef)}"))
            for name, value in extra:
                await session.execute(text(f"SET {name} = {value}"))
            for qv, _w in queries[: args.recall_queries]:
                approx = await find_nearest_chunks_authorized(
                    session, tenant_id=tenant, identity=identity, query_vector=qv, limit=k
                )
                async with sessionmaker() as s2:
                    exact = await _exact_ids(s2, tenant, identity, qv, k)
                got = {e.chunk_id for e, _ in approx}
                returned.append(len(got))
                recalls.append(len(got & set(exact)) / max(1, len(exact)))
        vec[str(ef)] = {
            "latency": _percentiles(lat),
            f"recall_at_{k}_mean": round(statistics.fmean(recalls), 3),
            f"recall_at_{k}_min": round(min(recalls), 3),
            "results_returned_mean": round(statistics.fmean(returned), 1),
            "results_requested": k,
        }
    out["vector_leg_by_ef_search"] = vec

    async def lexical_leg(session: AsyncSession, _qv: list[float], words: str) -> list[Any]:
        return await search_chunks_lexical_authorized(
            session, tenant_id=tenant, identity=identity, query=words, limit=k
        )

    out["lexical_leg"] = _percentiles(await timed(lexical_leg))

    embedder = _FixedQueryEmbedder()
    for qv, words in queries:
        embedder.vectors[words] = qv

    async def hybrid(session: AsyncSession, _qv: list[float], words: str) -> list[Any]:
        return await hybrid_search_authorized(
            session,
            tenant_id=tenant,
            identity=identity,
            query=words,
            embedding_provider=embedder,
            limit=10,
        )

    out["hybrid_authorized"] = {
        "ef_search": "database default (40)",
        **_percentiles(await timed(hybrid)),
    }

    # Query plans for the record.
    plans: dict[str, str] = {}
    async with sessionmaker() as session:
        qv_lit = _vec_literal(np.asarray(queries[0][0], dtype=np.float32))
        plans["vector_acl_filtered"] = "\n".join(
            re.sub(r"'\[[-0-9.e, ]+\]'", "'<query vector>'", r[0])
            for r in await session.execute(
                text(
                    "EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) SELECT e.chunk_id FROM embeddings e "
                    "JOIN document_chunks c ON c.id = e.chunk_id "
                    "JOIN document_versions v ON v.id = c.document_version_id "
                    "JOIN documents d ON d.id = v.document_id "
                    "WHERE e.tenant_id = :t AND v.status <> 'draft' AND v.effective_from <= now() "
                    "AND EXISTS (SELECT 1 FROM document_acl a WHERE a.tenant_id = :t AND "
                    "a.document_id = d.id AND (a.visibility = 'public_within_tenant' OR "
                    "(a.visibility = 'department_only' AND a.department = 'engineering'))) "
                    "ORDER BY e.embedding <=> CAST(:q AS vector) LIMIT 10"
                ),
                {"t": tenant, "q": qv_lit},
            )
        )
    out["plans"] = plans

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "hardware": _hardware(pg),
        "dataset": {
            "synthetic": True,
            "vectors": f"Gaussian mixture, {CLUSTERS} clusters, {DIM} dims (not real embeddings)",
            "text": f"Zipf over a {VOCAB}-word vocabulary, {WORDS_PER_CHUNK} words/chunk",
            "total_chunks": total_chunks,
            "tenants": args.tenants,
            "querying_tenant_chunks": tenant_chunks,
            "restricted_document_fraction": RESTRICTED_FRACTION,
            "load_seconds": round(load_s, 1),
            "hnsw_index_mb": index_mb,
            "chunks_plus_embeddings_total_mb": table_mb,
        },
        "config": {
            "k": k,
            "queries": args.queries,
            "recall_queries": args.recall_queries,
            "hnsw_index": "pgvector defaults (m=16, ef_construction=64)",
            "single_client_sequential": True,
            "session_settings": args.session_setting,
        },
        "results": out,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--chunks", type=int, default=0)
    p.add_argument("--tenants", type=int, default=10)
    p.add_argument("--queries", type=int, default=100)
    p.add_argument("--recall-queries", type=int, default=30)
    p.add_argument("--k", type=int, default=50)
    p.add_argument("--ef-search", type=int, nargs="+", default=[40, 100, 200, 400])
    p.add_argument(
        "--session-setting",
        action="append",
        default=[],
        help="extra SET name=value applied to vector-leg sessions (repeatable)",
    )
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--reuse", action="store_true", help="measure the already-loaded database")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    result = asyncio.run(_run(args))
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result["results"]["vector_leg_by_ef_search"], indent=2))


if __name__ == "__main__":
    main()
