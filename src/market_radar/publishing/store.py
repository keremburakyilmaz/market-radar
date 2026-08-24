"""Object storage abstractions used by the publishing pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class ObjectStoreConflictError(RuntimeError):
    """Raised when a conditional object write loses a race."""


@dataclass(frozen=True)
class StoredObject:
    """An object body and the metadata needed by the publisher."""

    key: str
    body: bytes
    etag: str
    content_type: str | None = None
    cache_control: str | None = None


class ObjectStore(Protocol):
    """Minimal object-store contract required for atomic publication."""

    def get(self, key: str) -> StoredObject | None:
        """Return an object, or ``None`` when the key does not exist."""

        ...

    def put(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str,
        cache_control: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> StoredObject:
        """Store an object, enforcing any supplied write precondition."""

        ...


class LocalObjectStore:
    """Filesystem-backed object storage for local runs and tests.

    Objects are written beneath ``root`` using their object key. Metadata lives
    in a private sidecar directory so it cannot collide with published keys.
    Writes and precondition checks are serialized with a process-wide file lock
    and committed with ``os.replace``.
    """

    _METADATA_DIRECTORY = ".market-radar-object-metadata"
    _LOCK_FILE = ".market-radar-object-store.lock"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> StoredObject | None:
        object_path = self._object_path(key)
        if not object_path.is_file():
            return None

        body = object_path.read_bytes()
        metadata = self._read_metadata(key)
        return StoredObject(
            key=key,
            body=body,
            etag=str(metadata.get("etag") or self._etag(body)),
            content_type=self._optional_string(metadata.get("content_type")),
            cache_control=self._optional_string(metadata.get("cache_control")),
        )

    def put(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str,
        cache_control: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> StoredObject:
        if if_match is not None and if_none_match:
            raise ValueError("if_match and if_none_match are mutually exclusive")
        if not isinstance(body, bytes):
            raise TypeError("object body must be bytes")

        object_path = self._object_path(key)
        metadata_path = self._metadata_path(key)
        object_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)

        with self._exclusive_lock():
            existing = self.get(key)
            if if_none_match and existing is not None:
                raise ObjectStoreConflictError(
                    f"object already exists while If-None-Match was required: {key}"
                )
            if if_match is not None and (existing is None or existing.etag != if_match):
                raise ObjectStoreConflictError(
                    f"object ETag changed before conditional write: {key}"
                )

            etag = self._etag(body)
            metadata = {
                "key": key,
                "etag": etag,
                "content_type": content_type,
                "cache_control": cache_control,
            }
            self._atomic_write(object_path, body)
            self._atomic_write(
                metadata_path,
                json.dumps(
                    metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )

        return StoredObject(
            key=key,
            body=body,
            etag=etag,
            content_type=content_type,
            cache_control=cache_control,
        )

    def _object_path(self, key: str) -> Path:
        self._validate_key(key)
        return self.root.joinpath(*key.split("/"))

    def _metadata_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / self._METADATA_DIRECTORY / f"{digest}.json"

    def _read_metadata(self, key: str) -> dict[str, Any]:
        metadata_path = self._metadata_path(key)
        if not metadata_path.is_file():
            return {}
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(metadata, dict) or metadata.get("key") != key:
            return {}
        return metadata

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        # fcntl is available on both supported execution targets: macOS and
        # GitHub's Linux runners. Importing it here keeps module import cheap.
        import fcntl

        lock_path = self.root / self._LOCK_FILE
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _atomic_write(path: Path, body: bytes) -> None:
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}-"
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as temporary_file:
                temporary_file.write(body)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(str(temporary_path), str(path))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _validate_key(key: str) -> None:
        if not key or key.startswith("/") or "\\" in key:
            raise ValueError("object key must be a non-empty relative POSIX path")
        parts = key.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("object key contains an unsafe path segment")

    @staticmethod
    def _etag(body: bytes) -> str:
        # Quoting mirrors the ETag representation returned by S3/R2 clients.
        return f'"{hashlib.sha256(body).hexdigest()}"'

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return value if isinstance(value, str) else None


class Boto3R2ObjectStore:
    """Cloudflare R2 adapter using its S3-compatible API.

    ``boto3`` is imported only when the first operation needs a client, so the
    local publisher and its tests do not require the optional dependency.
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self._client = client

    def get(self, key: str) -> StoredObject | None:
        try:
            response = self._get_client().get_object(Bucket=self.bucket, Key=key)
        except Exception as error:
            if self._is_missing(error):
                return None
            raise

        body = response["Body"].read()
        return StoredObject(
            key=key,
            body=body,
            etag=str(response["ETag"]),
            content_type=self._optional_string(response.get("ContentType")),
            cache_control=self._optional_string(response.get("CacheControl")),
        )

    def put(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str,
        cache_control: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> StoredObject:
        if if_match is not None and if_none_match:
            raise ValueError("if_match and if_none_match are mutually exclusive")

        arguments: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
            "CacheControl": cache_control,
        }
        if if_match is not None:
            arguments["IfMatch"] = if_match
        elif if_none_match:
            arguments["IfNoneMatch"] = "*"

        try:
            response = self._get_client().put_object(**arguments)
        except Exception as error:
            if self._is_conflict(error):
                raise ObjectStoreConflictError(f"conditional R2 write failed for {key}") from error
            raise

        return StoredObject(
            key=key,
            body=body,
            etag=str(response["ETag"]),
            content_type=content_type,
            cache_control=cache_control,
        )

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as error:
                raise RuntimeError("boto3 is required to use Boto3R2ObjectStore") from error

            self._client = boto3.client(
                service_name="s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name="auto",
            )
        return self._client

    @classmethod
    def _is_missing(cls, error: Exception) -> bool:
        status, code = cls._error_details(error)
        return status == 404 or code in ("NoSuchKey", "NotFound")

    @classmethod
    def _is_conflict(cls, error: Exception) -> bool:
        status, code = cls._error_details(error)
        return status in (409, 412) or code in (
            "ConditionalRequestConflict",
            "PreconditionFailed",
        )

    @staticmethod
    def _error_details(error: Exception) -> tuple[int | None, str | None]:
        response = getattr(error, "response", None)
        if not isinstance(response, dict):
            return None, None
        metadata = response.get("ResponseMetadata")
        status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
        error_payload = response.get("Error")
        code = error_payload.get("Code") if isinstance(error_payload, dict) else None
        return status, code

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return value if isinstance(value, str) else None
