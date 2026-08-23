from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SNAPSHOT = PROJECT_ROOT / "examples" / "snapshot.v1.json"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from market_radar.publishing import (  # noqa: E402
    JSON_CONTENT_TYPE,
    LATEST_KEY,
    POINTER_CACHE_CONTROL,
    SNAPSHOT_CACHE_CONTROL,
    LocalObjectStore,
    ObjectStoreConflictError,
    PublishConflictError,
    Publisher,
    PublishVerificationError,
    StoredObject,
    canonical_json_bytes,
)
from market_radar.validation import validate_manifest  # noqa: E402


def snapshot(value: int = 1):
    return {
        "schemaVersion": 1,
        "id": "mr-20260823t123456z",
        "generatedAt": "2026-08-23T12:34:56Z",
        "validUntil": "2026-08-23T20:34:56Z",
        "status": "healthy",
        "nested": {"z": value, "a": [3, 2, 1]},
    }


def valid_snapshot():
    return json.loads(EXAMPLE_SNAPSHOT.read_text(encoding="utf-8"))


def valid_publisher(store):
    return Publisher(
        store,
        clock=lambda: datetime(2030, 1, 15, 13, 0, tzinfo=timezone.utc),
    )


class DelegatingStore:
    def __init__(self, inner: LocalObjectStore) -> None:
        self.inner = inner

    def get(self, key: str) -> StoredObject | None:
        return self.inner.get(key)

    def put(self, key: str, body: bytes, **kwargs) -> StoredObject:
        return self.inner.put(key, body, **kwargs)


class CorruptingSnapshotStore(DelegatingStore):
    def __init__(self, inner: LocalObjectStore) -> None:
        super().__init__(inner)
        self.corrupt_snapshot_reads = False

    def put(self, key: str, body: bytes, **kwargs) -> StoredObject:
        stored = super().put(key, body, **kwargs)
        if key.startswith("v1/snapshots/"):
            self.corrupt_snapshot_reads = True
        return stored

    def get(self, key: str) -> StoredObject | None:
        stored = super().get(key)
        if stored is not None and self.corrupt_snapshot_reads and key.startswith("v1/snapshots/"):
            return StoredObject(
                key=stored.key,
                body=stored.body + b"corrupt",
                etag=stored.etag,
                content_type=stored.content_type,
                cache_control=stored.cache_control,
            )
        return stored


class ConflictingLatestStore(DelegatingStore):
    def __init__(self, inner: LocalObjectStore) -> None:
        super().__init__(inner)
        self.conflict_on_next_latest_put = False

    def get(self, key: str) -> StoredObject | None:
        stored = super().get(key)
        if key == LATEST_KEY and stored is not None:
            self.conflict_on_next_latest_put = True
        return stored

    def put(self, key: str, body: bytes, **kwargs) -> StoredObject:
        if key == LATEST_KEY and self.conflict_on_next_latest_put:
            self.conflict_on_next_latest_put = False
            current = self.inner.get(key)
            assert current is not None
            self.inner.put(
                key,
                b'{"writer":"other"}',
                content_type=JSON_CONTENT_TYPE,
                cache_control=POINTER_CACHE_CONTROL,
                if_match=current.etag,
            )
        return super().put(key, body, **kwargs)


class PublishingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = LocalObjectStore(Path(self.temporary_directory.name))

    def test_canonical_bytes_are_deterministic(self) -> None:
        first = {
            "nested": {"z": 1, "a": [3, 2, 1]},
            "generatedAt": "2026-08-23T12:34:56Z",
            "validUntil": "2026-08-23T20:34:56Z",
            "id": "mr-20260823t123456z",
            "schemaVersion": 1,
            "status": "healthy",
        }
        second = {
            "status": "healthy",
            "schemaVersion": 1,
            "id": "mr-20260823t123456z",
            "validUntil": "2026-08-23T20:34:56Z",
            "generatedAt": "2026-08-23T12:34:56Z",
            "nested": {"a": [3, 2, 1], "z": 1},
        }

        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        first_result = Publisher(self.store).publish(first, dry_run=True)
        second_result = Publisher(self.store).publish(second, dry_run=True)
        self.assertEqual(first_result.snapshot_sha256, second_result.snapshot_sha256)
        self.assertEqual(first_result.snapshot_key, second_result.snapshot_key)
        self.assertIsNone(self.store.get(first_result.snapshot_key))
        self.assertIsNone(self.store.get(LATEST_KEY))

    def test_publish_is_idempotent_and_sets_cache_metadata(self) -> None:
        publisher = Publisher(self.store)

        first = publisher.publish(snapshot())
        second = publisher.publish(snapshot())

        self.assertTrue(first.snapshot_created)
        self.assertTrue(first.latest_updated)
        self.assertFalse(second.snapshot_created)
        self.assertFalse(second.latest_updated)
        self.assertEqual(first.snapshot_key, second.snapshot_key)
        self.assertEqual(first.snapshot_etag, second.snapshot_etag)
        self.assertEqual(first.latest_etag, second.latest_etag)

        stored_snapshot = self.store.get(first.snapshot_key)
        stored_latest = self.store.get(LATEST_KEY)
        assert stored_snapshot is not None
        assert stored_latest is not None
        self.assertEqual(stored_snapshot.cache_control, SNAPSHOT_CACHE_CONTROL)
        self.assertEqual(stored_latest.cache_control, POINTER_CACHE_CONTROL)
        self.assertEqual(stored_snapshot.content_type, JSON_CONTENT_TYPE)
        pointer = json.loads(stored_latest.body.decode("utf-8"))
        validate_manifest(pointer)
        self.assertEqual(pointer["snapshot"]["path"], first.snapshot_key)
        self.assertEqual(pointer["snapshot"]["sha256"], first.snapshot_sha256)

    def test_corrupt_readback_fails_before_pointer_update(self) -> None:
        corrupting_store = CorruptingSnapshotStore(self.store)

        with self.assertRaises(PublishVerificationError):
            Publisher(corrupting_store).publish(snapshot())

        self.assertIsNone(self.store.get(LATEST_KEY))

    def test_latest_pointer_conflict_fails_closed(self) -> None:
        Publisher(self.store).publish(snapshot(1))
        conflicting_store = ConflictingLatestStore(self.store)

        with self.assertRaises(PublishConflictError):
            Publisher(conflicting_store).publish(snapshot(2))

        latest = self.store.get(LATEST_KEY)
        assert latest is not None
        self.assertEqual(latest.body, b'{"writer":"other"}')

    def test_local_store_enforces_conditional_writes(self) -> None:
        first = self.store.put(
            "v1/example.json",
            b"first",
            content_type=JSON_CONTENT_TYPE,
            cache_control=POINTER_CACHE_CONTROL,
            if_none_match=True,
        )
        with self.assertRaises(ObjectStoreConflictError):
            self.store.put(
                "v1/example.json",
                b"second",
                content_type=JSON_CONTENT_TYPE,
                cache_control=POINTER_CACHE_CONTROL,
                if_match='"stale"',
            )
        second = self.store.put(
            "v1/example.json",
            b"second",
            content_type=JSON_CONTENT_TYPE,
            cache_control=POINTER_CACHE_CONTROL,
            if_match=first.etag,
        )
        self.assertNotEqual(first.etag, second.etag)

    def test_promotes_only_a_verified_existing_immutable_snapshot(self) -> None:
        publisher = valid_publisher(self.store)
        first_snapshot = valid_snapshot()
        first = publisher.publish(first_snapshot)
        second_snapshot = deepcopy(first_snapshot)
        second_snapshot["digest"]["summary"] = "A later interpretation of the same inputs."
        second = publisher.publish(second_snapshot)

        promoted = publisher.promote_existing(first.snapshot_key)

        self.assertTrue(promoted.latest_updated)
        self.assertEqual(promoted.snapshot_key, first.snapshot_key)
        self.assertEqual(promoted.previous_snapshot_key, second.snapshot_key)
        latest = self.store.get(LATEST_KEY)
        assert latest is not None
        manifest = validate_manifest(json.loads(latest.body.decode("utf-8")))
        self.assertEqual(manifest["snapshot"]["path"], first.snapshot_key)
        self.assertEqual(manifest["snapshot"]["sha256"], first.snapshot_sha256)

    def test_promotion_rejects_missing_unsafe_or_non_contract_objects(self) -> None:
        publisher = Publisher(self.store)
        with self.assertRaisesRegex(PublishVerificationError, "unsafe"):
            publisher.promote_existing("../snapshot.json")
        missing_key = "v1/snapshots/2026/08/23/2026-08-23T12-34-56Z-" + "a" * 64 + ".json"
        with self.assertRaisesRegex(PublishVerificationError, "does not exist"):
            publisher.promote_existing(missing_key)

        non_contract = publisher.publish(snapshot())
        with self.assertRaisesRegex(PublishVerificationError, "public contract"):
            publisher.promote_existing(non_contract.snapshot_key)

    def test_promotion_rejects_a_key_that_does_not_match_stored_bytes(self) -> None:
        key = "v1/snapshots/2026/08/23/2026-08-23T12-34-56Z-" + "a" * 64 + ".json"
        self.store.put(
            key,
            b"{}",
            content_type=JSON_CONTENT_TYPE,
            cache_control=SNAPSHOT_CACHE_CONTROL,
            if_none_match=True,
        )

        with self.assertRaisesRegex(PublishVerificationError, "hash"):
            Publisher(self.store).promote_existing(key)

    def test_promotion_latest_pointer_conflict_fails_closed(self) -> None:
        publisher = valid_publisher(self.store)
        first_snapshot = valid_snapshot()
        first = publisher.publish(first_snapshot)
        second_snapshot = deepcopy(first_snapshot)
        second_snapshot["digest"]["summary"] = "A competing current interpretation."
        publisher.publish(second_snapshot)
        conflicting_store = ConflictingLatestStore(self.store)

        with self.assertRaises(PublishConflictError):
            valid_publisher(conflicting_store).promote_existing(first.snapshot_key)

        latest = self.store.get(LATEST_KEY)
        assert latest is not None
        self.assertEqual(latest.body, b'{"writer":"other"}')


if __name__ == "__main__":
    unittest.main()
