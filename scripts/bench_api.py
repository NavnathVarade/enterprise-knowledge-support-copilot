"""
Closed-loop HTTP load test against a RUNNING API (Spec §119).

Each of C workers sends requests back to back for `--duration` seconds. Reports throughput
and latency percentiles per scenario and concurrency, and counts every non-2xx response.

Limits (state these next to any number it produces):
  * Closed loop: latency under overload is understated (coordinated omission); use the
    throughput/latency curve across concurrency levels, not one point.
  * Client and server normally share one machine here, so they compete for CPU.
  * It does not exercise /api/v1/query (needs a live LLM) or uploads.
  * Login uses the seeded synthetic accounts, so it only runs against dev/test data.

Usage:
    PYTHONPATH=. python -m scripts.bench_api --base-url http://127.0.0.1:8000 \
        --tenant acme-corp --email alice@example.com --password alice-synthetic-pw-1 \
        --concurrency 1 8 32 --duration 15 --out result.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from scripts.bench_retrieval import _hardware, _percentiles


async def _drive(
    call: Callable[[httpx.AsyncClient], Awaitable[int]],
    *,
    base_url: str,
    concurrency: int,
    duration: float,
) -> dict[str, Any]:
    latencies: list[float] = []
    statuses: Counter[str] = Counter()
    stop = time.perf_counter() + duration
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=30.0) as client:
        for _ in range(3):  # warm-up
            await call(client)

        async def worker() -> None:
            while time.perf_counter() < stop:
                t = time.perf_counter()
                try:
                    code = await call(client)
                    statuses[str(code)] += 1
                except httpx.HTTPError as exc:
                    statuses[type(exc).__name__] += 1
                    continue
                latencies.append((time.perf_counter() - t) * 1000)

        t0 = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        elapsed = time.perf_counter() - t0

    ok = sum(v for k, v in statuses.items() if k.startswith("2"))
    total = sum(statuses.values())
    return {
        "concurrency": concurrency,
        "requests": total,
        "success_2xx": ok,
        "errors": total - ok,
        "status_counts": dict(statuses),
        "throughput_rps": round(total / elapsed, 1),
        "latency": _percentiles(latencies) if latencies else None,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as c:
        r = await c.post(
            "/api/v1/auth/login",
            json={"tenant_slug": args.tenant, "email": args.email, "password": args.password},
        )
        r.raise_for_status()
        token = r.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    async def live(c: httpx.AsyncClient) -> int:
        return (await c.get("/api/v1/health/live")).status_code

    async def ready(c: httpx.AsyncClient) -> int:
        return (await c.get("/api/v1/health/ready")).status_code

    async def docs(c: httpx.AsyncClient) -> int:
        return (await c.get("/api/v1/documents", headers=auth)).status_code

    async def login(c: httpx.AsyncClient) -> int:
        return (
            await c.post(
                "/api/v1/auth/login",
                json={"tenant_slug": args.tenant, "email": args.email, "password": args.password},
            )
        ).status_code

    scenarios: dict[str, Callable[[httpx.AsyncClient], Awaitable[int]]] = {
        "health_live": live,
        "health_ready": ready,
        "documents_list_authenticated": docs,
        "login_bcrypt": login,
    }
    results: dict[str, list[dict[str, Any]]] = {}
    for name, fn in scenarios.items():
        results[name] = [
            await _drive(fn, base_url=args.base_url, concurrency=c, duration=args.duration)
            for c in args.concurrency
        ]
    # Head-of-line blocking probe: health latency while logins (CPU-heavy bcrypt) are running.
    login_c, probe_c = 4, 4
    probe: dict[str, Any]
    _login_load, probe = await asyncio.gather(
        _drive(login, base_url=args.base_url, concurrency=login_c, duration=args.duration),
        _drive(live, base_url=args.base_url, concurrency=probe_c, duration=args.duration),
    )
    results["health_live_while_4_clients_log_in"] = [probe]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "hardware": _hardware({"note_server": args.server_note}),
        "config": {
            "base_url_host_only": "local",
            "duration_seconds_per_point": args.duration,
            "concurrency_levels": args.concurrency,
            "closed_loop": True,
            "server": args.server_note,
        },
        "results": results,
        "median_of_p50_ms_all_points": round(
            statistics.median(
                p["latency"]["p50_ms"] for pts in results.values() for p in pts if p["latency"]
            ),
            2,
        ),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--tenant", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--concurrency", type=int, nargs="+", default=[1, 8, 32])
    p.add_argument("--duration", type=float, default=15.0)
    p.add_argument("--server-note", default="unspecified")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    result = asyncio.run(_run(args))
    args.out.write_text(json.dumps(result, indent=2))
    for name, pts in result["results"].items():
        for pt in pts:
            lat = pt["latency"] or {}
            print(
                f"{name:32s} c={pt['concurrency']:<3d} rps={pt['throughput_rps']:<8} "
                f"p50={lat.get('p50_ms')} p95={lat.get('p95_ms')} p99={lat.get('p99_ms')} "
                f"errors={pt['errors']}"
            )


if __name__ == "__main__":
    main()
