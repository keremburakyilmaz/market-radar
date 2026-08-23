"""Validated snapshot publication with immutable data and a mutable pointer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from market_radar.canonical import canonical_json_bytes as _canonical_json_bytes
from market_radar.canonical import sha256_hex

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
    snapshot_etag: Optional[str]
    latest_etag: Optional[str]
    snapshot_created: bool
    latest_updated: bool
    dry_run: bool


def canonical_json_bytes(value: Any) -> bytes:
    """Use the application's canonical JSON form with publisher errors."""

    try:
        return _canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise SnapshotSerializationError(
            "snapshot is not strict JSON: {}".format(error)
        ) from error


class Publisher:
    """Publish an already-validated snapshot to an :class:`ObjectStore`."""

    def __init__(
        self,
        store: ObjectStore,
        *,
        latest_key: str = LATEST_KEY,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.store = store
        self.latest_key = latest_key
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def publish(
        self, snapshot: Mapping[str, Any], *, dry_run: bool = False
    ) -> PublishResult:
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

    def _ensure_snapshot(
        self, key: str, expected_body: bytes, expected_sha256: str
    ) -> tuple[StoredObject, bool]:
        existing = self.store.get(key)
        if existing is not None:
            self._verify_body(key, existing.body, expected_body, expected_sha256)
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
            raise PublishVerificationError(
                "snapshot is missing after upload: {}".format(key)
            )
        self._verify_body(key, stored.body, expected_body, expected_sha256)
        self._verify_metadata(
            key,
            stored,
            content_type=JSON_CONTENT_TYPE,
            cache_control=SNAPSHOT_CACHE_CONTROL,
        )
        return stored, created

    def _update_latest(self, expected_body: bytes) -> tuple[StoredObject, bool]:
        previous = self.store.get(self.latest_key)
        if previous is not None:
            if previous.body == expected_body or self._same_snapshot_pointer(
                previous.body, expected_body
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
            raise PublishConflictError(
                "latest pointer changed during publication"
            ) from error

        stored = self.store.get(self.latest_key)
        if stored is None:
            raise PublishVerificationError(
                "latest pointer failed readback verification"
            )
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
    def _verify_body(
        key: str,
        actual_body: bytes,
        expected_body: bytes,
        expected_sha256: str,
    ) -> None:
        actual_sha256 = Publisher._sha256(actual_body)
        if actual_sha256 != expected_sha256 or actual_body != expected_body:
            raise PublishVerificationError(
                "object readback hash mismatch for {}".format(key)
            )

    @staticmethod
    def _verify_metadata(
        key: str,
        stored: StoredObject,
        *,
        content_type: str,
        cache_control: str,
    ) -> None:
        if stored.content_type != content_type:
            raise PublishVerificationError(
                "object Content-Type mismatch for {}".format(key)
            )
        if stored.cache_control != cache_control:
            raise PublishVerificationError(
                "object Cache-Control mismatch for {}".format(key)
            )

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
            raise SnapshotSerializationError(
                "snapshot.generatedAt must include a timezone"
            )

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
        return "v1/snapshots/{}/{}-{}.json".format(
            directory, timestamp, digest
        )

    @staticmethod
    def _sha256(body: bytes) -> str:
        return sha256_hex(body)
