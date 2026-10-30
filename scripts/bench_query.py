"""Closed-loop load test of POST /api/v1/query against a RUNNING API (Phase 45).

What this measures: the platform's OWN work per question - authentication, rate limiting,
routing, ACL-aware hybrid retrieval, context construction, citation validation, audit write and
usage-ledger write - plus HTTP. What it does NOT measure: real LLM / embedding / rerank provider
latency, which will normally dominate. Run it against `tests/e2e/harness.py` (extractive LLM
stand-in, deterministic embeddings) and read the result as "platform overhead", never as
"answer latency".

It uploads `--documents` small synthetic markdown documents through the real API first and waits
for ingestion, so retrieval has data. Limits: closed loop (coordinated omission understates
overload latency), client and server share a machine, single run per point.

Usage:
    PYTHONPATH=. python -m scripts.bench_query --base-url http://127.0.0.1:8766 \
        --tenant SLUG --email E --password P --documents 200 --out result.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from scripts.bench_api import _drive
from scripts.bench_retrieval import _hardware

TOPICS = [
    ("refund", "Customers may request a refund within {n} days of purchase."),
    ("shipping", "Standard shipping takes {n} business days within the country."),
    ("warranty", "Hardware is covered by a warranty of {n} months from delivery."),
    ("security", "Passwords must be rotated every {n} days and never shared."),
    ("onboarding", "New employees complete onboarding within {n} working days."),
    ("support", "Priority support tickets receive a first response within {n} hours."),
]
QUESTIONS = [
    "What is the refund policy for customers?",
    "Explain the shipping policy.",
    "What does the warranty cover?",
    "What is the password rotation policy?",
    "Describe the onboarding process for new employees.",
    "How does priority support work?",
]


def make_document(i: int) -> tuple[str, bytes]:
    topic, sentence = TOPICS[i % len(TOPICS)]
    body = (
        f"# {topic.title()} policy {i}\n\n## Details\n\n{sentence.format(n=7 + i % 50)}\n\n"
        + "\n\n".join(
            f"Clause {j}: general terms for the {topic} policy apply to all regions and customers."
            for j in range(8)
        )
    )
    return f"{topic}-{i}.md", body.encode()


async def _seed(c: httpx.AsyncClient, auth: dict[str, str], n: int) -> float:
    t0 = time.perf_counter()
    sem = asyncio.Semaphore(4)

    async def up(i: int) -> None:
        name, body = make_document(i)
        async with sem:
            r = await c.post(
                "/api/v1/documents",
                headers=auth,
                files={"file": (name, body, "text/markdown")},
            )
            r.raise_for_status()

    await asyncio.gather(*(up(i) for i in range(n)))
    deadline = time.perf_counter() + 600
    while time.perf_counter() < deadline:
        rows = (await c.get("/api/v1/documents", headers=auth)).json()
        states = Counter(d["status"] for d in rows)
        bad = states.get("processing_failed", 0) + states.get("quarantined", 0)
        if bad:
            raise RuntimeError(f"{bad} documents failed ingestion: {dict(states)}")
        if states.get("active", 0) >= n:
            return time.perf_counter() - t0
        await asyncio.sleep(1)
    raise TimeoutError("ingestion did not finish in 600 s")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=args.base_url, timeout=60.0) as c:
        r = await c.post(
            "/api/v1/auth/login",
            json={"tenant_slug": args.tenant, "email": args.email, "password": args.password},
        )
        r.raise_for_status()
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
        ingest_s = await _seed(c, auth, args.documents)

    outcomes: Counter[str] = Counter()
    counter = 0

    async def ask(client: httpx.AsyncClient) -> int:
        nonlocal counter
        counter += 1
        resp = await client.post(
            "/api/v1/query", headers=auth, json={"query": QUESTIONS[counter % len(QUESTIONS)]}
        )
        if resp.status_code == 200:
            outcomes[resp.json().get("status", "?")] += 1
        return resp.status_code

    points = [
        await _drive(ask, base_url=args.base_url, concurrency=c, duration=args.duration)
        for c in args.concurrency
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "hardware": _hardware({"note_server": args.server_note}),
        "config": {
            "documents": args.documents,
            "ingest_seconds_for_all_documents": round(ingest_s, 1),
            "duration_seconds_per_point": args.duration,
            "closed_loop": True,
            "server": args.server_note,
            "providers": "stand-ins (extractive LLM, deterministic embeddings, lexical reranker): provider latency EXCLUDED",
        },
        "answer_outcomes_200_only": dict(outcomes),
        "results": {"query": points},
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--tenant", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--documents", type=int, default=200)
    p.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 16])
    p.add_argument("--duration", type=float, default=20.0)
    p.add_argument("--server-note", default="unspecified")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    result = asyncio.run(_run(args))
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    for pt in result["results"]["query"]:
        lat = pt["latency"] or {}
        print(
            f"c={pt['concurrency']:<3d} rps={pt['throughput_rps']:<8} p50={lat.get('p50_ms')} "
            f"p95={lat.get('p95_ms')} p99={lat.get('p99_ms')} errors={pt['errors']} {pt['status_counts']}"
        )
    print(
        "outcomes:",
        result["answer_outcomes_200_only"],
        "ingest_s:",
        result["config"]["ingest_seconds_for_all_documents"],
    )


if __name__ == "__main__":
    main()
