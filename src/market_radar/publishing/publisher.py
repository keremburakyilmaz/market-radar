"""Validated snapshot publication with immutable data and a mutable pointer."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from market_radar.canonical import canonical_json_bytes as _canonical_json_bytes
from market_radar.canonical import sha256_hex
from market_radar.validation import SNAPSHOT_PATH_PATTERN, validate_manifest, validate_snapshot

from .store import ObjectStore, ObjectStoreConflictError, StoredObject

SNAPSHOT_CACHE_CONTROL = "public,max-age=31536000,immutable"
POINTER_CACHE_CONTROL = "no-store"
JSON_CONTENT_TYPE = "application/json; charset=utf-8"
LATEST_KEY = "v1/latest.json"


class PublishingError(RuntimeError):
    """Base class for publisher failures."""


class SnapshotSerializationError(PublishingError):
    """Raised when a purportedly validated snapshot cannot be serialized."""


class PublishConflictError(PublishingError):
    """Raised when another writer changes a conditional publication target."""


class PublishVerificationError(PublishingError):
    """Raised when object-store readback differs from what was written."""


@dataclass(frozen=True)
class PublishResult:
    snapshot_key: str
    snapshot_sha256: str
    snapshot_bytes: int
    latest_key: str
    snapshot_etag: str | None
    latest_etag: str | None
    snapshot_created: bool
    latest_updated: bool
    dry_run: bool


@dataclass(frozen=True)
class PromotionResult:
    snapshot_key: str
    snapshot_sha256: str
    previous_snapshot_key: str | None
    latest_etag: str
    latest_updated: bool


def canonical_json_bytes(value: Any) -> bytes:
    """Use the application's canonical JSON form with publisher errors."""

    try:
        return _canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise SnapshotSerializationError(f"snapshot is not strict JSON: {error}") from error


class Publisher:
    """Publish an already-validated snapshot to an :class:`ObjectStore`."""

    def __init__(
        self,
        store: ObjectStore,
        *,
        latest_key: str = LATEST_KEY,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.latest_key = latest_key
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def publish(self, snapshot: Mapping[str, Any], *, dry_run: bool = False) -> PublishResult:
        snapshot_body = canonical_json_bytes(snapshot)
        snapshot_sha256 = self._sha256(snapshot_body)
        generated_at, generated_at_text = self._generated_at(snapshot)
        snapshot_key = self._snapshot_key(generated_at, snapshot_sha256)
        latest_body = self._manifest_body(
            snapshot,
            snapshot_key=snapshot_key,
            snapshot_sha256=snapshot_sha256,
            snapshot_size=len(snapshot_body),
            generated_at_text=generated_at_text,
        )

        if dry_run:
            return PublishResult(
                snapshot_key=snapshot_key,
                snapshot_sha256=snapshot_sha256,
                snapshot_bytes=len(snapshot_body),
                latest_key=self.latest_key,
                snapshot_etag=None,
                latest_etag=None,
                snapshot_created=False,
                latest_updated=False,
                dry_run=True,
            )

        snapshot_object, snapshot_created = self._ensure_snapshot(
            snapshot_key, snapshot_body, snapshot_sha256
        )
        latest_object, latest_updated = self._update_latest(latest_body)

        return PublishResult(
            snapshot_key=snapshot_key,
            snapshot_sha256=snapshot_sha256,
            snapshot_bytes=len(snapshot_body),
            latest_key=self.latest_key,
            snapshot_etag=snapshot_object.etag,
            latest_etag=latest_object.etag,
            snapshot_created=snapshot_created,
            latest_updated=latest_updated,
            dry_run=False,
        )

    def promote_existing(self, snapshot_key: str) -> PromotionResult:
        """Conditionally point ``latest`` at a verified immutable snapshot.

        Promotion is the only supported public rollback primitive. It never
        copies or rewrites snapshot bytes and refuses objects that do not match
        the v1 contract, their content-addressed key, or immutable metadata.
        """

        match = SNAPSHOT_PATH_PATTERN.fullmatch(snapshot_key)
        if match is None:
            raise PublishVerificationError("snapshot promotion key is unsafe")

        stored_snapshot = self.store.get(snapshot_key)
        if stored_snapshot is None:
            raise PublishVerificationError("snapshot promotion target does not exist")
        self._verify_metadata(
            snapshot_key,
            stored_snapshot,
            content_type=JSON_CONTENT_TYPE,
            cache_control=SNAPSHOT_CACHE_CONTROL,
        )

        digest = self._sha256(stored_snapshot.body)
        if match.group("sha256") != digest:
            raise PublishVerificationError("snapshot promotion target hash does not match its key")

        snapshot = self._validated_snapshot(stored_snapshot.body)
        generated_at, generated_at_text = self._generated_at(snapshot)
        if self._snapshot_key(generated_at, digest) != snapshot_key:
            raise PublishVerificationError(
                "snapshot promotion target path does not match its payload"
            )

        latest_body = self._manifest_body(
            snapshot,
            snapshot_key=snapshot_key,
            snapshot_sha256=digest,
            snapshot_size=len(stored_snapshot.body),
            generated_at_text=generated_at_text,
        )
        validate_manifest(self._parse_json_object(latest_body, "rollback manifest"))

        previous = self.store.get(self.latest_key)
        previous_snapshot_key = self._manifest_snapshot_key(previous)
        latest_object, latest_updated = self._update_latest(
            latest_body,
            previous=previous,
            previous_loaded=True,
        )
        return PromotionResult(
            snapshot_key=snapshot_key,
            snapshot_sha256=digest,
            previous_snapshot_key=previous_snapshot_key,
            latest_etag=latest_object.etag,
            latest_updated=latest_updated,
        )

    def _ensure_snapshot(
        self, key: str, expected_body: bytes, expected_sha256: str
    ) -> tuple[StoredObject, bool]:
        existing = self.store.get(key)
        if existing is not None:
            self._verify_body(key, existing.body, expected_body, expected_sha256)
            self._verify_metadata(
                key,
                existing,
                content_type=JSON_CONTENT_TYPE,
                cache_control=SNAPSHOT_CACHE_CONTROL,
            )
            return existing, False

        try:
            self.store.put(
                key,
                expected_body,
                content_type=JSON_CONTENT_TYPE,
                cache_control=SNAPSHOT_CACHE_CONTROL,
                if_none_match=True,
            )
            created = True
        except ObjectStoreConflictError:
            # A concurrent writer may have created the same content-addressed
            # object. It is safe only when readback proves the bytes identical.
            created = False

        stored = self.store.get(key)
        if stored is None:
            raise PublishVerificationError(f"snapshot is missing after upload: {key}")
        self._verify_body(key, stored.body, expected_body, expected_sha256)
        self._verify_metadata(
            key,
            stored,
            content_type=JSON_CONTENT_TYPE,
            cache_control=SNAPSHOT_CACHE_CONTROL,
        )
        return stored, created

    def _update_latest(
        self,
        expected_body: bytes,
        *,
        previous: StoredObject | None = None,
        previous_loaded: bool = False,
    ) -> tuple[StoredObject, bool]:
        if not previous_loaded:
            previous = self.store.get(self.latest_key)
        if previous is not None and (
            previous.body == expected_body
            or self._same_snapshot_pointer(previous.body, expected_body)
        ):
            self._verify_metadata(
                self.latest_key,
                previous,
                content_type=JSON_CONTENT_TYPE,
                cache_control=POINTER_CACHE_CONTROL,
            )
            return previous, False

        try:
            self.store.put(
                self.latest_key,
                expected_body,
                content_type=JSON_CONTENT_TYPE,
                cache_control=POINTER_CACHE_CONTROL,
                if_match=previous.etag if previous is not None else None,
                if_none_match=previous is None,
            )
        except ObjectStoreConflictError as error:
            raise PublishConflictError("latest pointer changed during publication") from error

        stored = self.store.get(self.latest_key)
        if stored is None:
            raise PublishVerificationError("latest pointer failed readback verification")
        self._verify_body(
            self.latest_key,
            stored.body,
            expected_body,
            self._sha256(expected_body),
        )
        self._verify_metadata(
            self.latest_key,
            stored,
            content_type=JSON_CONTENT_TYPE,
            cache_control=POINTER_CACHE_CONTROL,
        )
        return stored, True

    @staticmethod
    def _parse_json_object(body: bytes, label: str) -> dict[str, Any]:
        try:
            value = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError) as error:
            raise PublishVerificationError(f"{label} is not strict JSON") from error
        if not isinstance(value, dict):
            raise PublishVerificationError(f"{label} root is not an object")
        return value

    @classmethod
    def _validated_snapshot(cls, body: bytes) -> dict[str, Any]:
        snapshot = cls._parse_json_object(body, "snapshot promotion target")
        try:
            validate_snapshot(snapshot)
        except ValueError as error:
            raise PublishVerificationError(
                "snapshot promotion target does not satisfy the public contract"
            ) from error
        if canonical_json_bytes(snapshot) != body:
            raise PublishVerificationError("snapshot promotion target is not canonical JSON")
        return snapshot

    @staticmethod
    def _manifest_snapshot_key(stored: StoredObject | None) -> str | None:
        if stored is None:
            return None
        try:
            manifest = json.loads(stored.body.decode("utf-8"))
            pointer = manifest.get("snapshot") if isinstance(manifest, dict) else None
            path = pointer.get("path") if isinstance(pointer, dict) else None
            return path if isinstance(path, str) else None
        except (UnicodeDecodeError, ValueError):
            return None

    @staticmethod
    def _verify_body(
        key: str,
        actual_body: bytes,
        expected_body: bytes,
        expected_sha256: str,
    ) -> None:
        actual_sha256 = Publisher._sha256(actual_body)
        if actual_sha256 != expected_sha256 or actual_body != expected_body:
            raise PublishVerificationError(f"object readback hash mismatch for {key}")

    @staticmethod
    def _verify_metadata(
        key: str,
        stored: StoredObject,
        *,
        content_type: str,
        cache_control: str,
    ) -> None:
        if stored.content_type != content_type:
            raise PublishVerificationError(f"object Content-Type mismatch for {key}")
        if stored.cache_control != cache_control:
            raise PublishVerificationError(f"object Cache-Control mismatch for {key}")

    @staticmethod
    def _generated_at(snapshot: Mapping[str, Any]) -> tuple[datetime, str]:
        raw_value = snapshot.get("generatedAt")
        if not isinstance(raw_value, str) or not raw_value:
            raise SnapshotSerializationError(
                "snapshot.generatedAt must be a timezone-aware ISO-8601 string"
            )

        parse_value = raw_value[:-1] + "+00:00" if raw_value.endswith("Z") else raw_value
        try:
            parsed = datetime.fromisoformat(parse_value)
        except ValueError as error:
            raise SnapshotSerializationError(
                "snapshot.generatedAt is not valid ISO-8601"
            ) from error
        if parsed.tzinfo is None:
            raise SnapshotSerializationError("snapshot.generatedAt must include a timezone")

        generated_at = parsed.astimezone(timezone.utc)
        generated_at_text = generated_at.isoformat().replace("+00:00", "Z")
        return generated_at, generated_at_text

    def _manifest_body(
        self,
        snapshot: Mapping[str, Any],
        *,
        snapshot_key: str,
        snapshot_sha256: str,
        snapshot_size: int,
        generated_at_text: str,
    ) -> bytes:
        snapshot_id = snapshot.get("id")
        valid_until = snapshot.get("validUntil")
        schema_version = snapshot.get("schemaVersion")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise SnapshotSerializationError("snapshot.id must be a non-empty string")
        if not isinstance(valid_until, str) or not valid_until:
            raise SnapshotSerializationError(
                "snapshot.validUntil must be a timezone-aware ISO-8601 string"
            )
        if schema_version != 1:
            raise SnapshotSerializationError("snapshot.schemaVersion must be 1")

        published_at = self.clock()
        if published_at.tzinfo is None:
            raise SnapshotSerializationError("publisher clock must be timezone-aware")
        published_at_text = (
            published_at.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        return canonical_json_bytes(
            {
                "manifestVersion": 1,
                "publishedAt": published_at_text,
                "snapshot": {
                    "generatedAt": generated_at_text,
                    "id": snapshot_id,
                    "path": snapshot_key,
                    "schemaVersion": 1,
                    "sha256": snapshot_sha256,
                    "sizeBytes": snapshot_size,
                    "validUntil": valid_until,
                },
            }
        )

    @staticmethod
    def _same_snapshot_pointer(previous_body: bytes, expected_body: bytes) -> bool:
        try:
            previous = json.loads(previous_body.decode("utf-8"))
            expected = json.loads(expected_body.decode("utf-8"))
            return (
                isinstance(previous, dict)
                and isinstance(expected, dict)
                and set(previous) == {"manifestVersion", "publishedAt", "snapshot"}
                and previous.get("manifestVersion") == 1
                and isinstance(previous.get("publishedAt"), str)
                and previous.get("snapshot") == expected.get("snapshot")
            )
        except (AttributeError, UnicodeDecodeError, ValueError):
            return False

    @staticmethod
    def _snapshot_key(generated_at: datetime, digest: str) -> str:
        directory = generated_at.strftime("%Y/%m/%d")
        timestamp = generated_at.strftime("%Y-%m-%dT%H-%M-%SZ")
        return f"v1/snapshots/{directory}/{timestamp}-{digest}.json"

    @staticmethod
    def _sha256(body: bytes) -> str:
        return sha256_hex(body)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")
