from __future__ import annotations

import uuid

import boto3
import pytest
from moto import mock_aws
from packages.config.settings import ObjectStoreSettings
from packages.domain.errors import ConfigurationError
from packages.storage.object_store import (
    InMemoryObjectStore,
    ObjectNotFound,
    ObjectStoreError,
    S3ObjectStore,
    document_object_key,
)

pytestmark = pytest.mark.unit
T, D = uuid.uuid4(), uuid.uuid4()


def test_key_is_derived_and_tenant_scoped() -> None:
    key = document_object_key(tenant_id=T, document_id=D, content_hash="ab" * 32, prefix="p/")
    assert key == f"p/tenants/{T}/documents/{D}/{'ab' * 32}"
    other = document_object_key(tenant_id=uuid.uuid4(), document_id=D, content_hash="ab" * 32)
    assert other != key.removeprefix("p/")


def test_s3_store_requires_a_bucket() -> None:
    with pytest.raises(ConfigurationError):
        S3ObjectStore(ObjectStoreSettings(bucket=None))


@pytest.fixture
def s3() -> S3ObjectStore:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="ekc-test")
        yield S3ObjectStore(
            ObjectStoreSettings(bucket="ekc-test", region="us-east-1"), client=client
        )


async def test_s3_round_trip_with_sse_and_content_type(s3: S3ObjectStore) -> None:
    await s3.put("k/1", b"hello", content_type="text/plain")
    assert await s3.exists("k/1")
    assert await s3.get("k/1", max_bytes=100) == b"hello"
    head = s3._client.head_object(Bucket="ekc-test", Key="k/1")
    assert head["ContentType"] == "text/plain"
    assert head["ServerSideEncryption"] == "AES256"  # default from settings
    assert s3.uri_for("k/1") == "s3://ekc-test/k/1"
    await s3.delete("k/1")
    assert not await s3.exists("k/1")


async def test_s3_missing_key_is_permanent_and_oversize_is_refused(s3: S3ObjectStore) -> None:
    with pytest.raises(ObjectNotFound):
        await s3.get("nope", max_bytes=10)
    await s3.put("big", b"x" * 50, content_type="text/plain")
    with pytest.raises(ObjectStoreError):
        await s3.get("big", max_bytes=10)


async def test_s3_service_errors_are_wrapped_as_retryable() -> None:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")  # bucket never created
        store = S3ObjectStore(
            ObjectStoreSettings(bucket="absent", region="us-east-1"), client=client
        )
        with pytest.raises(ObjectStoreError):
            await store.put("k", b"x", content_type="text/plain")


async def test_in_memory_store_contract() -> None:
    s = InMemoryObjectStore()
    await s.put("a", b"123", content_type="text/plain")
    assert await s.get("a", max_bytes=3) == b"123"
    with pytest.raises(ObjectStoreError):
        await s.get("a", max_bytes=2)
    with pytest.raises(ObjectNotFound):
        await s.get("b", max_bytes=3)
