"""
Fault-injection runs against a REAL API process, real Postgres and real Redis (Spec §122).

Each scenario starts uvicorn as a subprocess, probes three endpoints continuously
(`/health/live`, `/health/ready`, authenticated `GET /api/v1/documents`), injects one real
fault, removes it, and reports per-phase status counts, latency and time-to-recovery.

Faults are real: `service postgresql stop`, `redis-cli shutdown`, an ACCESS EXCLUSIVE table
lock held by a second connection, SIGTERM/SIGKILL of the API process, and a TCP listener
that accepts connections and never answers (a hung dependency, i.e. a network black hole).
Needs root (service control), a seeded `ekc_load`-style database, and the .env.test
environment. NOT for use against anything but a disposable sandbox.

    set -a; . ./.env.test; set +a
    DATABASE_URL=.../ekc_load PYTHONPATH=. python -m scripts.chaos_run --out result.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import socket

# Fault injection deliberately shells out (service control, psql); sandbox-only tool.
import subprocess  # nosec B404
import sys
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

_api: subprocess.Popen[bytes] | None = None


def _seed_password() -> str:
    # The synthetic dataset's own fixture credential: read from its source of truth rather than
    # repeated here (this tool only works against a database seeded with that dataset).
    from data.synthetic.generator import generate_dataset

    tenant = next(t for t in generate_dataset().tenants if t.slug == "acme-corp")
    return next(u.password for u in tenant.users if u.email == "alice@example.com")


_SEED_PASSWORD = _seed_password()
PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"
PROBE_TIMEOUT = 45.0
ENDPOINTS = ("live", "ready", "documents", "login")
# Status that means "served correctly" per endpoint (login probes use a wrong password).
OK_STATUS = {"live": "200", "ready": "200", "documents": "200", "login": "401"}


@dataclass
class Sample:
    t: float
    endpoint: str
    status: str
    ms: float


@dataclass
class Run:
    samples: list[Sample] = field(default_factory=list)
    marks: dict[str, float] = field(default_factory=dict)


def _start_api(env_extra: dict[str, str]) -> subprocess.Popen[bytes]:
    env = {**os.environ, "HARDENING_LOGIN_MAX_FAILURES_PER_IP": "100000", **env_extra}
    return subprocess.Popen(  # noqa: S603  # nosec B603 B607
        [
            sys.executable,
            "-m",
            "uvicorn",
            "apps.api.app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--no-access-log",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _wait_live(max_wait: float = 40.0) -> None:
    end = time.monotonic() + max_wait
    async with httpx.AsyncClient(timeout=2.0) as c:
        while time.monotonic() < end:
            with contextlib.suppress(httpx.HTTPError):
                if (await c.get(f"{BASE}/api/v1/health/live")).status_code == 200:
                    return
            await asyncio.sleep(0.3)
    raise RuntimeError("API did not come up")


async def _login() -> str:
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            f"{BASE}/api/v1/auth/login",
            json={
                "tenant_slug": "acme-corp",
                "email": "alice@example.com",
                "password": _SEED_PASSWORD,
            },
        )
        r.raise_for_status()
        return str(r.json()["access_token"])


async def _prober(run: Run, token: str, stop: asyncio.Event, hz: float = 4.0) -> None:
    paths = {
        "live": ("/api/v1/health/live", {}),
        "ready": ("/api/v1/health/ready", {}),
        "documents": ("/api/v1/documents", {"Authorization": f"Bearer {token}"}),
    }

    async def loop(name: str) -> None:
        if name == "documents" and not token:
            return  # no authenticated probe when the scenario cannot log in
        rate = 1.0 if name == "login" else hz
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as c:
            while not stop.is_set():
                t0 = time.monotonic()
                try:
                    if name == "login":
                        resp = await c.post(
                            f"{BASE}/api/v1/auth/login",
                            json={
                                "tenant_slug": "acme-corp",
                                "email": f"probe{uuid.uuid4().hex[:8]}@example.com",
                                "password": "wrong-password-probe",  # nosec B105
                            },
                        )
                    else:
                        path, headers = paths[name]
                        resp = await c.get(f"{BASE}{path}", headers=headers)
                    status = str(resp.status_code)
                except httpx.HTTPError as exc:
                    status = type(exc).__name__
                run.samples.append(Sample(t0, name, status, (time.monotonic() - t0) * 1000))
                await asyncio.sleep(max(0.0, 1 / rate - (time.monotonic() - t0)))

    await asyncio.gather(*(loop(n) for n in ENDPOINTS))


def _summarise(run: Run, phases: list[tuple[str, str, str]]) -> dict[str, Any]:
    """phases: (name, start_mark, end_mark)."""
    out: dict[str, Any] = {"phases": {}}
    for name, a, b in phases:
        lo, hi = run.marks[a], run.marks[b]
        per: dict[str, Any] = {}
        for ep in ENDPOINTS:
            rows = [s for s in run.samples if s.endpoint == ep and lo <= s.t < hi]
            ms = sorted(s.ms for s in rows)
            per[ep] = {
                "requests": len(rows),
                "status": dict(Counter(s.status for s in rows)),
                "p50_ms": round(ms[len(ms) // 2], 1) if ms else None,
                "max_ms": round(ms[-1], 1) if ms else None,
            }
        out["phases"][name] = {"seconds": round(hi - lo, 1), **per}
    return out


def _recovery(run: Run, fault_end: str) -> dict[str, float | None]:
    """Seconds from fault removal to the first 200 that is followed by no later non-200."""
    rec: dict[str, float | None] = {}
    t_end = run.marks[fault_end]
    for ep in ENDPOINTS:
        rows = [s for s in run.samples if s.endpoint == ep and s.t >= t_end]
        first = None
        ok = OK_STATUS[ep]
        for i, s in enumerate(rows):
            if s.status == ok and all(r.status == ok for r in rows[i:]):
                first = round(s.t - t_end, 1)
                break
        rec[ep] = first
    return rec


def _sh(cmd: str) -> None:
    subprocess.run(cmd, shell=True, check=False, capture_output=True)  # noqa: S602  # nosec B602


async def _scenario(
    name: str,
    *,
    inject: Callable[[Run], Awaitable[None]],
    remove: Callable[[Run], Awaitable[None]],
    fault_seconds: float,
    env_extra: dict[str, str] | None = None,
    tail_seconds: float = 25.0,
    needs_token: bool = True,
) -> dict[str, Any]:
    global _api  # noqa: PLW0603
    _api = _start_api(env_extra or {})
    run = Run()
    try:
        await _wait_live()
        token = await _login() if needs_token else ""
        stop = asyncio.Event()
        probe = asyncio.create_task(_prober(run, token, stop))
        await asyncio.sleep(6)
        run.marks["fault_start"] = time.monotonic()
        await inject(run)
        await asyncio.sleep(fault_seconds)
        run.marks["fault_end"] = time.monotonic()
        await remove(run)
        await asyncio.sleep(tail_seconds)
        run.marks["end"] = time.monotonic()
        stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(probe, PROBE_TIMEOUT + 5)
        run.marks.setdefault("start", run.samples[0].t if run.samples else 0.0)
    finally:
        if _api is not None:
            _api.terminate()
            with contextlib.suppress(Exception):
                _api.wait(timeout=15)
            _api.kill()
    run.marks["start"] = min(s.t for s in run.samples)
    result = _summarise(
        run,
        [
            ("before_fault", "start", "fault_start"),
            ("during_fault", "fault_start", "fault_end"),
            ("after_fault", "fault_end", "end"),
        ],
    )
    result["seconds_to_recover_after_fault_removed"] = _recovery(run, "fault_end")
    result["fault_seconds"] = fault_seconds
    return result


# ---- faults -------------------------------------------------------------------------------


async def _noop(_: Run) -> None:
    return None


async def _pg_stop(_: Run) -> None:
    _sh("service postgresql stop")


async def _pg_start(_: Run) -> None:
    _sh("service postgresql start")


async def _redis_stop(_: Run) -> None:
    _sh("redis-cli shutdown nosave")


async def _redis_start(_: Run) -> None:
    _sh("redis-server --daemonize yes")


_lock_proc: subprocess.Popen[bytes] | None = None


def _psql_env() -> dict[str, str]:
    return {**os.environ, "PGPASSWORD": os.environ.get("PGPASSWORD", "postgres")}


def _spawn_lock(seconds: float) -> subprocess.Popen[bytes]:
    db = os.environ["DATABASE_URL"].rsplit("/", 1)[-1]
    sql = (
        "BEGIN; LOCK TABLE users, documents IN ACCESS EXCLUSIVE MODE; "
        f"SELECT pg_sleep({seconds}); COMMIT;"
    )
    return subprocess.Popen(  # noqa: S603  # nosec B603 B607
        ["psql", "-U", "postgres", "-h", "localhost", "-d", db, "-c", sql],  # noqa: S607  # nosec B607
        env=_psql_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _lock_tables(seconds: float) -> Callable[[Run], Awaitable[None]]:
    async def inject(_: Run) -> None:
        global _lock_proc  # noqa: PLW0603
        _lock_proc = _spawn_lock(seconds)
        await asyncio.sleep(1.0)

    return inject


async def _unlock(_: Run) -> None:
    if _lock_proc is not None:
        with contextlib.suppress(Exception):
            _lock_proc.wait(timeout=60)


class _BlackHole:
    """TCP listener that accepts connections and never answers."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self.conns: list[socket.socket] = []
        self._stop = False
        import threading

        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        self.sock.settimeout(0.5)
        while not self._stop:
            with contextlib.suppress(OSError):
                c, _ = self.sock.accept()
                self.conns.append(c)

    def close(self) -> None:
        self._stop = True
        for c in self.conns:
            with contextlib.suppress(OSError):
                c.close()
        self.sock.close()


def _api_signal(sig: int) -> Callable[[Run], Awaitable[None]]:
    async def inject(_: Run) -> None:
        if _api is not None:
            os.kill(_api.pid, sig)

    return inject


async def _api_restart(_: Run) -> None:
    global _api  # noqa: PLW0603
    _api = _start_api({})


async def _main(only: list[str] | None) -> dict[str, Any]:
    results: dict[str, Any] = {}

    def want(n: str) -> bool:
        return only is None or n in only

    if want("postgres_stop_10s"):
        results["postgres_stop_10s"] = await _scenario(
            "postgres_stop_10s", inject=_pg_stop, remove=_pg_start, fault_seconds=10
        )
    if want("redis_stop_10s"):
        results["redis_stop_10s"] = await _scenario(
            "redis_stop_10s", inject=_redis_stop, remove=_redis_start, fault_seconds=10
        )
    if want("db_table_lock_20s"):
        results["db_table_lock_20s"] = await _scenario(
            "db_table_lock_20s",
            inject=_lock_tables(20),
            remove=_unlock,
            fault_seconds=20,
        )
    if want("redis_blackhole_20s"):
        bh_r = _BlackHole()
        try:
            results["redis_blackhole_20s"] = await _scenario(
                "redis_blackhole_20s",
                inject=_noop,
                remove=_noop,
                fault_seconds=20,
                env_extra={"REDIS_URL": f"redis://127.0.0.1:{bh_r.port}/0"},
            )
        finally:
            bh_r.close()
    if want("api_sigterm_restart"):
        results["api_sigterm_restart"] = await _scenario(
            "api_sigterm_restart",
            inject=_api_signal(signal.SIGTERM),
            remove=_api_restart,
            fault_seconds=3,
        )
    if want("api_sigkill_restart"):
        results["api_sigkill_restart"] = await _scenario(
            "api_sigkill_restart",
            inject=_api_signal(signal.SIGKILL),
            remove=_api_restart,
            fault_seconds=3,
        )
    if want("db_blackhole_20s"):
        bh = _BlackHole()
        db_url = os.environ["DATABASE_URL"]
        head, _, tail = db_url.partition("@")
        new_url = f"{head}@127.0.0.1:{bh.port}/{db_url.rsplit('/', 1)[-1]}"

        # The API is started pointed at the hole (a dependency that accepts and never answers).
        try:
            results["db_blackhole_20s"] = await _scenario(
                "db_blackhole_20s",
                inject=_noop,
                remove=_noop,
                fault_seconds=20,
                env_extra={"DATABASE_URL": new_url},
                needs_token=False,
            )
        finally:
            bh.close()
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    res = asyncio.run(_main(args.only))
    doc = {
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": "disposable sandbox: real Postgres 16, Redis, uvicorn (1 worker), 4 req/s per endpoint",
        "scenarios": res,
    }
    args.out.write_text(json.dumps(doc, indent=2))
    for name, r in res.items():
        print("==", name, "recovery(s):", r["seconds_to_recover_after_fault_removed"])
        for ph, v in r["phases"].items():
            print(" ", ph, {ep: (v[ep]["status"], v[ep]["max_ms"]) for ep in ENDPOINTS})


if __name__ == "__main__":
    main()
