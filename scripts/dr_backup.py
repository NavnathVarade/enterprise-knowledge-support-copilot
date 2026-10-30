"""Logical backup, restore and restore-drill tooling for the primary PostgreSQL database.

    # connection strings come from environment variables, never from argv (process lists leak)
    python -m scripts.dr_backup backup  --out-dir /backups                      # DATABASE_URL
    python -m scripts.dr_backup restore --manifest M.json --confirm-target-database ekc_restored
    python -m scripts.dr_backup drill   --work-dir /tmp/drill --out report.json  # backup+restore+verify

What a backup is: one consistent `pg_dump -Fc` (an exported snapshot, so the row counts and
audit-chain tails in the manifest describe exactly the dumped state) plus a manifest with the
SHA-256, Alembic head, per-table row counts and the tail of every tenant's audit hash chain.

What a *verified* restore is: checksum matches, restore into an EMPTY database succeeds, Alembic
head and every table's row count equal the manifest, every tenant's audit chain re-verifies and
ends at the manifested tail, pgvector is present and no index is invalid.

This complements, and does not replace, RDS automated backups / point-in-time recovery (see
docs/disaster-recovery). A logical dump is slow to restore at scale and is a portability and
corruption-escape copy, not the primary RPO mechanism. Restore time here is measured on whatever
database you point it at; extrapolating it to production size is NOT valid.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
from packages.audit.service import verify_chain
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

FORMAT_VERSION = 1
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
# A drill may only (re)create a database that is obviously disposable.
_SCRATCH = re.compile(r"(^|[_-])(scratch|drill|restore|restored)([_-]|$)")


class DRError(Exception):
    """A backup/restore precondition or integrity check failed."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


# ------------------------------------------------------------------ connection helpers
def _url_from_env(var: str) -> URL:
    raw = os.environ.get(var)
    if not raw:
        raise DRError(f"environment variable {var} is not set")
    return make_url(raw)


def _pg_env(url: URL) -> dict[str, str]:
    env = {**os.environ}
    if url.host:
        env["PGHOST"] = url.host
    if url.port:
        env["PGPORT"] = str(url.port)
    if url.username:
        env["PGUSER"] = url.username
    if url.password:
        env["PGPASSWORD"] = url.password
    if url.database:
        env["PGDATABASE"] = url.database
    sslmode = url.query.get("sslmode") or url.query.get("ssl")
    if isinstance(sslmode, str):
        env["PGSSLMODE"] = sslmode
    return env


async def _connect(url: URL, database: str | None = None) -> asyncpg.Connection:
    sslmode = url.query.get("sslmode") or url.query.get("ssl")
    return await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database=database or url.database,
        ssl=sslmode if isinstance(sslmode, str) else None,
        timeout=15,
    )


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise DRError(f"{name} not found on PATH (install the PostgreSQL client tools)")
    return path


def _run(argv: list[str], env: dict[str, str]) -> str:
    proc = subprocess.run(argv, env=env, capture_output=True, text=True)  # noqa: S603  # nosec B603
    if proc.returncode != 0:
        # stderr from pg tools never contains the password (it travels via PGPASSWORD)
        raise DRError(
            f"{Path(argv[0]).name} failed ({proc.returncode}): {proc.stderr.strip()[-800:]}"
        )
    return proc.stdout


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _quote_ident(name: str) -> str:
    if not _IDENT.match(name):
        raise DRError(f"refusing unexpected table name {name!r}")
    return f'"{name}"'


# ------------------------------------------------------------------ state inspection
async def _public_tables(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    )
    return [r["tablename"] for r in rows]


async def _table_counts(conn: asyncpg.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in await _public_tables(conn):
        counts[name] = int(await conn.fetchval(f"SELECT count(*) FROM {_quote_ident(name)}"))  # noqa: S608  # nosec B608
    return counts


async def _audit_tails(conn: asyncpg.Connection) -> dict[str, dict[str, Any]]:
    if "audit_events" not in await _public_tables(conn):
        return {}
    rows = await conn.fetch(
        "SELECT DISTINCT ON (tenant_id) tenant_id::text AS t, seq, row_hash "
        "FROM audit_events ORDER BY tenant_id, seq DESC"
    )
    return {r["t"]: {"seq": int(r["seq"]), "row_hash": r["row_hash"]} for r in rows}


async def _alembic_head(conn: asyncpg.Connection) -> str | None:
    if "alembic_version" not in await _public_tables(conn):
        return None
    return await conn.fetchval("SELECT version_num FROM alembic_version")  # type: ignore[no-any-return]


# ------------------------------------------------------------------ backup
async def backup(source: URL, out_dir: Path) -> dict[str, Any]:
    await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dump = out_dir / f"ekc-{stamp}.dump"
    manifest_path = out_dir / f"ekc-{stamp}.manifest.json"
    pg_dump = _tool("pg_dump")
    started = time.monotonic()

    conn = await _connect(source)
    try:
        tx = conn.transaction(isolation="repeatable_read", readonly=True)
        await tx.start()
        try:
            await conn.execute("SET LOCAL row_security = off")  # error, never silent filtering
            snapshot = await conn.fetchval("SELECT pg_export_snapshot()")
            server_version = await conn.fetchval("SHOW server_version")
            await asyncio.to_thread(
                _run,
                [
                    pg_dump,
                    "--format=custom",
                    "--compress=6",
                    "--no-owner",
                    "--no-privileges",
                    f"--snapshot={snapshot}",
                    f"--file={dump}",
                ],
                _pg_env(source),
            )
            tables = await _table_counts(conn)
            tails = await _audit_tails(conn)
            head = await _alembic_head(conn)
        finally:
            await tx.rollback()  # read-only: nothing to commit
    finally:
        await conn.close()

    dump.chmod(0o600)
    version_out = _run([pg_dump, "--version"], os.environ.copy()).strip()
    manifest: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "source_database": source.database,
        "dump_file": dump.name,
        "sha256": _sha256(dump),
        "size_bytes": dump.stat().st_size,
        "pg_dump_version": version_out,
        "server_version": server_version,
        "alembic_head": head,
        "tables": tables,
        "audit_tails": tails,
        "backup_seconds": round(time.monotonic() - started, 3),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    manifest_path.chmod(0o600)
    manifest["manifest_path"] = str(manifest_path)
    return manifest


# ------------------------------------------------------------------ restore
def load_manifest(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    if data.get("format_version") != FORMAT_VERSION:
        raise DRError(f"unsupported manifest format {data.get('format_version')!r}")
    return data


def check_dump_integrity(manifest: dict[str, Any], manifest_path: Path) -> Path:
    dump = manifest_path.parent / str(manifest["dump_file"])
    if not dump.is_file():
        raise DRError(f"dump file {dump.name} is missing")
    if dump.stat().st_size != manifest["size_bytes"] or _sha256(dump) != manifest["sha256"]:
        raise DRError("dump does not match its manifest checksum (corrupt or tampered)")
    return dump


async def restore(manifest_path: Path, target: URL, confirm_database: str) -> float:
    """Restore into an EMPTY database. Refuses anything else: drop/recreate it first."""
    if confirm_database != target.database:
        raise DRError("--confirm-target-database must equal the target database name")
    manifest = load_manifest(manifest_path)
    dump = check_dump_integrity(manifest, manifest_path)
    conn = await _connect(target)
    try:
        existing = await _public_tables(conn)
    finally:
        await conn.close()
    if existing:
        raise DRError(
            f"target database {target.database!r} is not empty ({len(existing)} tables); "
            "restore only into a freshly created database"
        )
    started = time.monotonic()
    await asyncio.to_thread(
        _run,
        [
            _tool("pg_restore"),
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-privileges",
            f"--dbname={target.database}",
            str(dump),
        ],
        _pg_env(target),
    )
    return time.monotonic() - started


# ------------------------------------------------------------------ verification
async def verify_audit_chains(target: URL, tenants: list[str]) -> Check:
    """Recompute every listed tenant's audit hash chain (also usable on a live/PITR database)."""
    engine = create_async_engine(target.render_as_string(hide_password=False))
    bad: list[str] = []
    total = 0
    try:
        async with engine.connect() as c:
            await c.execute(text("SET row_security = off"))
            session = AsyncSession(bind=c)
            for tenant in tenants:
                v = await verify_chain(session, uuid.UUID(tenant))
                total += v.checked
                if not v.ok:
                    bad.append(f"{tenant}: seq {v.first_bad_seq} {v.reason}")
    finally:
        await engine.dispose()
    return Check("audit_chain_reverifies", not bad, f"{total} events checked; broken={bad}")


async def verify_restored(manifest: dict[str, Any], target: URL) -> list[Check]:
    checks: list[Check] = []
    conn = await _connect(target)
    try:
        await conn.execute("SET row_security = off")
        head = await _alembic_head(conn)
        checks.append(
            Check(
                "alembic_head",
                head == manifest["alembic_head"],
                f"restored={head} manifest={manifest['alembic_head']}",
            )
        )
        counts = await _table_counts(conn)
        want = manifest["tables"]
        missing = sorted(set(want) - set(counts))
        extra = sorted(set(counts) - set(want))
        wrong = {t: (counts[t], want[t]) for t in want if t in counts and counts[t] != want[t]}
        checks.append(
            Check(
                "table_row_counts",
                not (missing or extra or wrong),
                f"{len(want)} tables; missing={missing} extra={extra} mismatched={wrong}",
            )
        )
        ext = await conn.fetchval("SELECT count(*) FROM pg_extension WHERE extname='vector'")
        checks.append(Check("pgvector_extension", ext == 1, f"installed={ext}"))
        invalid = await conn.fetchval("SELECT count(*) FROM pg_index WHERE NOT indisvalid")
        checks.append(Check("no_invalid_indexes", invalid == 0, f"invalid={invalid}"))
        tails = await _audit_tails(conn)
    finally:
        await conn.close()

    want_tails = manifest["audit_tails"]
    checks.append(
        Check(
            "audit_tails_match_manifest",
            tails == want_tails,
            f"{len(tails)} tenants with audit rows",
        )
    )
    checks.append(await verify_audit_chains(target, list(tails)))
    return checks


# ------------------------------------------------------------------ drill
async def _recreate_scratch(admin_source: URL, target: URL) -> None:
    name = target.database or ""
    if not _SCRATCH.search(name):
        raise DRError(
            f"refusing to recreate {name!r}: a drill target must contain scratch/drill/restore in its name"
        )
    if (target.host, target.port, name) == (
        admin_source.host,
        admin_source.port,
        admin_source.database,
    ):
        raise DRError("drill target must not be the source database")
    conn = await _connect(target, database="postgres")
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')  # noqa: S608  # nosec B608
        await conn.execute(f'CREATE DATABASE "{name}"')  # noqa: S608  # nosec B608
    finally:
        await conn.close()


async def drill(source: URL, target: URL, work_dir: Path) -> dict[str, Any]:
    await _recreate_scratch(source, target)
    manifest = await backup(source, work_dir)
    manifest_path = Path(manifest.pop("manifest_path"))
    restore_s = await restore(manifest_path, target, target.database or "")
    checks = await verify_restored(manifest, target)
    return {
        "ok": all(c.ok for c in checks),
        "measured_on": "the database named below, on this host: NOT representative of production size",
        "source_database": source.database,
        "dump_size_bytes": manifest["size_bytes"],
        "tables": len(manifest["tables"]),
        "rows_total": sum(manifest["tables"].values()),
        "audit_tenants": len(manifest["audit_tails"]),
        "backup_seconds": manifest["backup_seconds"],
        "restore_seconds": round(restore_s, 3),
        "checks": [asdict(c) for c in checks],
    }


# ------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", help="consistent dump + manifest")
    b.add_argument("--out-dir", type=Path, required=True)
    b.add_argument("--source-url-env", default="DATABASE_URL")
    r = sub.add_parser("restore", help="restore into an EMPTY database, then verify")
    r.add_argument("--manifest", type=Path, required=True)
    r.add_argument("--target-url-env", default="DR_TARGET_DATABASE_URL")
    r.add_argument("--confirm-target-database", required=True)
    v = sub.add_parser("verify-audit", help="re-verify every tenant's audit chain in a database")
    v.add_argument("--url-env", default="DATABASE_URL")
    d = sub.add_parser("drill", help="backup source, restore into a scratch DB, verify, report")
    d.add_argument("--work-dir", type=Path, required=True)
    d.add_argument("--source-url-env", default="DATABASE_URL")
    d.add_argument("--target-url-env", default="DR_TARGET_DATABASE_URL")
    d.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    try:
        if args.cmd == "backup":
            m = asyncio.run(backup(_url_from_env(args.source_url_env), args.out_dir))
            print(
                json.dumps(
                    {k: m[k] for k in ("manifest_path", "dump_file", "size_bytes", "sha256")},
                    indent=2,
                )
            )
            return 0
        if args.cmd == "restore":
            target = _url_from_env(args.target_url_env)

            async def _go() -> tuple[float, list[Check]]:
                secs = await restore(args.manifest, target, args.confirm_target_database)
                return secs, await verify_restored(load_manifest(args.manifest), target)

            secs, checks = asyncio.run(_go())
            print(
                json.dumps(
                    {"restore_seconds": round(secs, 3), "checks": [asdict(c) for c in checks]},
                    indent=2,
                )
            )
            return 0 if all(c.ok for c in checks) else 1
        if args.cmd == "verify-audit":
            url = _url_from_env(args.url_env)

            async def _audit() -> Check:
                conn = await _connect(url)
                try:
                    await conn.execute("SET row_security = off")
                    tenants = list(await _audit_tails(conn))
                finally:
                    await conn.close()
                return await verify_audit_chains(url, tenants)

            check = asyncio.run(_audit())
            print(json.dumps(asdict(check), indent=2))
            return 0 if check.ok else 1
        report = asyncio.run(
            drill(
                _url_from_env(args.source_url_env),
                _url_from_env(args.target_url_env),
                args.work_dir,
            )
        )
        out = json.dumps(report, indent=2)
        if args.out:
            args.out.write_text(out + "\n")
        print(out)
        return 0 if report["ok"] else 1
    except DRError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
