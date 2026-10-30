"""Backup/restore drill against a real PostgreSQL: a restore is only believed if it verifies."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import asyncpg
import pytest
from packages.audit.service import record_audit
from packages.config.settings import get_settings
from packages.database.models import Tenant
from packages.database.session import get_sessionmaker
from scripts import dr_backup
from scripts.dr_backup import DRError
from sqlalchemy.engine import URL, make_url

pytestmark = pytest.mark.integration


def _source() -> URL:
    return make_url(str(get_settings().database.url))


def _target() -> URL:
    return _source().set(database="ekc_test_drill_scratch")


async def _seed_audit() -> uuid.UUID:
    sm = get_sessionmaker(get_settings().database)
    async with sm() as s:
        t = Tenant(name="DR", slug=f"dr-{uuid.uuid4().hex[:8]}")
        s.add(t)
        await s.flush()
        tid = t.id
        for i in range(5):
            await record_audit(
                s, tenant_id=tid, action="dr.test", resource_type="x", outcome="success",
                details={"size_bytes": i},
            )  # fmt: skip
        await s.commit()
    return tid


@pytest.fixture
async def drilled(tmp_path: Path) -> dict[str, object]:
    tid = await _seed_audit()
    report = await dr_backup.drill(_source(), _target(), tmp_path)
    manifests = list(tmp_path.glob("*.manifest.json"))  # noqa: ASYNC240
    assert len(manifests) == 1
    return {"report": report, "manifest": manifests[0], "tenant": tid, "dir": tmp_path}


async def test_drill_restores_and_every_check_passes(drilled: dict[str, object]) -> None:
    report = drilled["report"]
    assert isinstance(report, dict)
    failed = [c for c in report["checks"] if not c["ok"]]
    assert report["ok"] and not failed, failed
    assert {c["name"] for c in report["checks"]} == {
        "alembic_head",
        "table_row_counts",
        "pgvector_extension",
        "no_invalid_indexes",
        "audit_tails_match_manifest",
        "audit_chain_reverifies",
    }
    assert report["restore_seconds"] > 0 and report["rows_total"] > 0


async def test_manifest_has_no_credentials_and_files_are_private(
    drilled: dict[str, object],
) -> None:
    manifest_path = drilled["manifest"]
    assert isinstance(manifest_path, Path)
    text = manifest_path.read_text()
    assert _source().password not in text if _source().password else True
    assert "postgresql://" not in text and "postgresql+asyncpg" not in text
    assert (manifest_path.stat().st_mode & 0o077) == 0
    dump = manifest_path.parent / json.loads(text)["dump_file"]
    assert (dump.stat().st_mode & 0o077) == 0


async def test_corrupt_dump_is_refused_before_restore(drilled: dict[str, object]) -> None:
    manifest_path = drilled["manifest"]
    assert isinstance(manifest_path, Path)
    m = json.loads(manifest_path.read_text())
    dump = manifest_path.parent / m["dump_file"]
    blob = bytearray(dump.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    dump.write_bytes(bytes(blob))
    with pytest.raises(DRError, match="checksum"):
        dr_backup.check_dump_integrity(m, manifest_path)


async def test_restore_refuses_non_empty_target_and_wrong_confirmation(
    drilled: dict[str, object],
) -> None:
    manifest_path = drilled["manifest"]
    assert isinstance(manifest_path, Path)
    with pytest.raises(DRError, match="not empty"):
        await dr_backup.restore(manifest_path, _target(), "ekc_test_drill_scratch")
    with pytest.raises(DRError, match="confirm"):
        await dr_backup.restore(manifest_path, _target(), "something_else")


async def test_extra_rows_in_the_restored_database_fail_the_row_count_check(
    drilled: dict[str, object],
) -> None:
    manifest_path = drilled["manifest"]
    assert isinstance(manifest_path, Path)
    conn = await asyncpg.connect(
        host=_target().host, port=_target().port, user=_target().username,
        password=_target().password, database=_target().database,
    )  # fmt: skip
    try:
        await conn.execute(
            "INSERT INTO tenants (id, name, slug, status, created_at, updated_at) "
            "VALUES ($1, 'x', $2, 'active', now(), now())",
            uuid.uuid4(),
            f"extra-{uuid.uuid4().hex[:8]}",
        )  # fmt: skip
    finally:
        await conn.close()
    checks = {
        c.name: c
        for c in await dr_backup.verify_restored(dr_backup.load_manifest(manifest_path), _target())
    }
    assert not checks["table_row_counts"].ok


async def test_tampered_audit_row_in_a_backup_is_detected(drilled: dict[str, object]) -> None:
    manifest_path = drilled["manifest"]
    assert isinstance(manifest_path, Path)
    conn = await asyncpg.connect(
        host=_target().host, port=_target().port, user=_target().username,
        password=_target().password, database=_target().database,
    )  # fmt: skip
    try:
        await conn.execute("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete")
        await conn.execute(
            "UPDATE audit_events SET outcome='failure' WHERE tenant_id=$1 AND seq=2",
            drilled["tenant"],
        )
    finally:
        await conn.close()
    checks = {
        c.name: c
        for c in await dr_backup.verify_restored(dr_backup.load_manifest(manifest_path), _target())
    }
    assert not checks["audit_chain_reverifies"].ok
    assert "seq 2" in checks["audit_chain_reverifies"].detail


async def test_drill_refuses_non_scratch_or_source_targets(tmp_path: Path) -> None:
    with pytest.raises(DRError, match="scratch/drill/restore"):
        await dr_backup.drill(_source(), _source().set(database="ekc_production"), tmp_path)
    with pytest.raises(DRError, match="must not be the source"):
        await dr_backup.drill(
            _source().set(database="ekc_test_drill_scratch"),
            _source().set(database="ekc_test_drill_scratch"),
            tmp_path,
        )


def test_cli_refuses_missing_env_and_never_takes_urls_from_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DR_TEST_NO_SUCH_VAR", raising=False)
    assert (
        dr_backup.main(
            ["backup", "--out-dir", str(tmp_path), "--source-url-env", "DR_TEST_NO_SUCH_VAR"]
        )
        == 2
    )
    with pytest.raises(SystemExit):  # no --database-url style option exists
        dr_backup.main(
            ["backup", "--out-dir", str(tmp_path), "--database-url", "postgresql://u:p@h/d"]
        )


def test_table_names_are_validated_before_use_in_sql() -> None:
    assert dr_backup._quote_ident("audit_events") == '"audit_events"'
    for bad in ('x"; drop table y;--', "Tenants", "a b", ""):
        with pytest.raises(DRError):
            dr_backup._quote_ident(bad)


def test_manifest_format_version_is_enforced(tmp_path: Path) -> None:
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"format_version": 99}))
    with pytest.raises(DRError, match="unsupported"):
        dr_backup.load_manifest(p)


async def test_verify_audit_cli_passes_on_an_intact_chain_and_fails_on_a_broken_one(
    drilled: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DR_VERIFY_URL", _target().render_as_string(hide_password=False))
    assert (
        await asyncio.to_thread(dr_backup.main, ["verify-audit", "--url-env", "DR_VERIFY_URL"]) == 0
    )
    conn = await asyncpg.connect(
        host=_target().host, port=_target().port, user=_target().username,
        password=_target().password, database=_target().database,
    )  # fmt: skip
    try:
        await conn.execute("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete")
        await conn.execute(
            "DELETE FROM audit_events WHERE tenant_id=$1 AND seq=3", drilled["tenant"]
        )
    finally:
        await conn.close()
    assert (
        await asyncio.to_thread(dr_backup.main, ["verify-audit", "--url-env", "DR_VERIFY_URL"]) == 1
    )
