"""LocalFileObjectStore (development only): same contract as the other stores, and it cannot be
tricked into touching files outside its root."""

from __future__ import annotations

from pathlib import Path

import pytest
from packages.config.settings import ObjectStoreSettings
from packages.storage.object_store import (
    LocalFileObjectStore,
    ObjectNotFound,
    ObjectStoreError,
    S3ObjectStore,
    build_object_store,
)

pytestmark = pytest.mark.unit
KEY = "tenants/t1/documents/d1/abc"


async def test_roundtrip_exists_delete(tmp_path: Path) -> None:
    store = LocalFileObjectStore(tmp_path)
    assert not await store.exists(KEY)
    await store.put(KEY, b"hello", content_type="text/plain")
    assert await store.exists(KEY)
    assert await store.get(KEY, max_bytes=100) == b"hello"
    await store.delete(KEY)
    assert not await store.exists(KEY)
    await store.delete(KEY)  # idempotent


async def test_put_is_idempotent_and_leaves_no_partial_files(tmp_path: Path) -> None:
    store = LocalFileObjectStore(tmp_path)
    await store.put(KEY, b"v1", content_type="text/plain")
    await store.put(KEY, b"v1", content_type="text/plain")
    import os

    leftovers = [n for _, _, names in os.walk(tmp_path) for n in names if n.startswith(".part-")]
    assert leftovers == []


async def test_missing_object_is_the_permanent_not_found_error(tmp_path: Path) -> None:
    with pytest.raises(ObjectNotFound):
        await LocalFileObjectStore(tmp_path).get(KEY, max_bytes=10)


async def test_size_cap_is_enforced(tmp_path: Path) -> None:
    store = LocalFileObjectStore(tmp_path)
    await store.put(KEY, b"0123456789", content_type="text/plain")
    with pytest.raises(ObjectStoreError):
        await store.get(KEY, max_bytes=5)


@pytest.mark.parametrize(
    "bad", ["../escape", "a/../../escape", "/etc/passwd", "C:/Windows/x", "a\\b", ""]
)
async def test_keys_cannot_escape_the_root(tmp_path: Path, bad: str) -> None:
    store = LocalFileObjectStore(tmp_path / "root")
    with pytest.raises(ObjectStoreError):
        await store.put(bad, b"x", content_type="text/plain")
    assert not (tmp_path / "escape").exists()


def test_factory_picks_the_local_store_only_when_configured(tmp_path: Path) -> None:
    local = build_object_store(ObjectStoreSettings(local_directory=str(tmp_path)))
    assert isinstance(local, LocalFileObjectStore)
    s3 = build_object_store(ObjectStoreSettings(bucket="b", region="us-east-1"))
    assert isinstance(s3, S3ObjectStore)


def test_a_bucket_always_wins_over_the_local_directory(tmp_path: Path) -> None:
    settings = ObjectStoreSettings(bucket="b", region="us-east-1", local_directory=str(tmp_path))
    assert isinstance(build_object_store(settings), S3ObjectStore)
