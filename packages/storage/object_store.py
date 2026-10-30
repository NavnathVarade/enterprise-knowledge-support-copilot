"""
Object storage for uploaded originals (Spec §83).

`ObjectStore` is a small Protocol (put/get/exists/delete). Production uses
`S3ObjectStore`; tests use `InMemoryObjectStore` for logic and `moto` for the real boto3
code path. Keys are DERIVED, never taken from user input or event payloads:
`document_object_key(tenant, document, sha256)` -- so an event can never point a worker
at another tenant's object, and a re-upload of identical bytes maps to the same key
(idempotent put).

Error model: `ObjectNotFound` is permanent (non-retryable); `ObjectStoreError` is
transient/unknown and retryable by the consumer's backoff.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Protocol

from packages.config.settings import ObjectStoreSettings
from packages.domain.errors import ConfigurationError, DomainError


class ObjectNotFound(DomainError):
    code = "object_not_found"
    http_status = 404
    safe_message = "Stored object not found."


class ObjectStoreError(DomainError):
    code = "object_store_unavailable"
    http_status = 503
    safe_message = "Object storage is temporarily unavailable."


def document_object_key(
    *, tenant_id: uuid.UUID, document_id: uuid.UUID, content_hash: str, prefix: str = ""
) -> str:
    return f"{prefix}tenants/{tenant_id}/documents/{document_id}/{content_hash}"


class ObjectStore(Protocol):
    def uri_for(self, key: str) -> str: ...
    async def put(self, key: str, data: bytes, *, content_type: str) -> None: ...
    async def get(self, key: str, *, max_bytes: int) -> bytes: ...
    async def exists(self, key: str) -> bool: ...
    async def delete(self, key: str) -> None: ...


class InMemoryObjectStore:
    """Test double. Not for production (no durability, no sharing between processes)."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def uri_for(self, key: str) -> str:
        return f"memory://{key}"

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def get(self, key: str, *, max_bytes: int) -> bytes:
        if key not in self.objects:
            raise ObjectNotFound()
        data = self.objects[key][0]
        if len(data) > max_bytes:
            raise ObjectStoreError()
        return data

    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


class LocalFileObjectStore:
    """DEVELOPMENT-ONLY store: objects are files under one root directory.

    Settings reject it in staging/production. It gives a laptop (no S3, no Docker) a real
    durable store across restarts, with the same key scheme and error model as S3. It is NOT
    shared between machines, has no encryption at rest and no versioning. Keys are derived by
    `document_object_key`, but the path is still re-checked so a hostile key can never escape
    the root (`..`, absolute paths, drive letters, backslashes)."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not key or "\\" in key or key.startswith("/") or ":" in key:
            raise ObjectStoreError()
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            raise ObjectStoreError()
        return path

    def uri_for(self, key: str) -> str:
        return f"local://{key}"

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename: a crash never leaves a truncated object under the real key.
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".part-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise

        try:
            await asyncio.to_thread(write)
        except OSError as exc:
            raise ObjectStoreError() from exc

    async def get(self, key: str, *, max_bytes: int) -> bytes:
        path = self._path(key)

        def read() -> bytes:
            with path.open("rb") as handle:
                return handle.read(max_bytes + 1)

        try:
            data = await asyncio.to_thread(read)
        except FileNotFoundError as exc:
            raise ObjectNotFound() from exc
        except OSError as exc:
            raise ObjectStoreError() from exc
        if len(data) > max_bytes:
            raise ObjectStoreError()
        return data

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            await asyncio.to_thread(path.unlink, True)
        except OSError as exc:
            raise ObjectStoreError() from exc


class S3ObjectStore:
    """boto3 is synchronous; every call runs in a worker thread so the event loop (and
    other in-flight requests) are never blocked. Retries/timeouts are botocore's
    `standard` mode, bounded by settings."""

    def __init__(self, settings: ObjectStoreSettings, client: Any | None = None) -> None:
        if not settings.bucket:
            raise ConfigurationError("OBJECT_STORE_BUCKET is required.")
        self._bucket = settings.bucket
        self._sse = settings.sse or None
        self._kms = settings.kms_key_id
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                region_name=settings.region,
                endpoint_url=settings.endpoint_url,
                config=Config(
                    retries={"max_attempts": settings.max_attempts, "mode": "standard"},
                    connect_timeout=settings.request_timeout_seconds,
                    read_timeout=settings.request_timeout_seconds,
                ),
            )
        self._client = client

    def uri_for(self, key: str) -> str:
        return f"s3://{self._bucket}/{key}"

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        args: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
        }
        if self._sse:
            args["ServerSideEncryption"] = self._sse
            if self._sse == "aws:kms" and self._kms:
                args["SSEKMSKeyId"] = self._kms
        await self._call(lambda: self._client.put_object(**args))

    async def get(self, key: str, *, max_bytes: int) -> bytes:
        def read() -> bytes:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            if int(resp.get("ContentLength", 0)) > max_bytes:
                raise ObjectStoreError()
            body: bytes = resp["Body"].read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ObjectStoreError()
            return body

        result: bytes = await self._call(read)
        return result

    async def exists(self, key: str) -> bool:
        try:
            await self._call(lambda: self._client.head_object(Bucket=self._bucket, Key=key))
        except ObjectNotFound:
            return False
        return True

    async def delete(self, key: str) -> None:
        await self._call(lambda: self._client.delete_object(Bucket=self._bucket, Key=key))

    async def _call(self, fn: Any) -> Any:
        try:
            return await asyncio.to_thread(fn)
        except DomainError:
            raise
        except Exception as exc:  # noqa: BLE001 -- classify every SDK failure
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise ObjectNotFound() from exc
            raise ObjectStoreError() from exc


def build_object_store(settings: ObjectStoreSettings) -> ObjectStore:
    """S3 when a bucket is configured; the local-disk store only when explicitly configured
    (settings guarantee that never happens in staging/production)."""
    if settings.local_directory and not settings.bucket:
        return LocalFileObjectStore(settings.local_directory)
    return S3ObjectStore(settings)
