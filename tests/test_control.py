import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from market_radar.publishing import (
    JSON_CONTENT_TYPE,
    POINTER_CACHE_CONTROL,
    PUBLICATION_CONTROL_KEY,
    PUBLICATION_CONTROL_MAX_BYTES,
    LocalObjectStore,
    PublicationControlConflictError,
    PublicationControlIntegrityError,
    PublicationControlRepository,
)

NOW = datetime(2026, 8, 23, 12, 34, 56, 123456, tzinfo=timezone.utc)


class PublicationControlRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = LocalObjectStore(Path(self.temporary_directory.name))
        self.repository = PublicationControlRepository(self.store)

    def test_missing_control_bootstraps_as_enabled_without_writing(self):
        loaded = self.repository.load()

        self.assertTrue(loaded.control.enabled)
        self.assertFalse(loaded.control.paused)
        self.assertIsNone(loaded.control.updated_at)
        self.assertIsNone(loaded.control.reason)
        self.assertIsNone(loaded.control.actor)
        self.assertIsNone(loaded.etag)
        self.assertIsNone(self.store.get(PUBLICATION_CONTROL_KEY))

    def test_pause_is_sanitized_bounded_and_verified(self):
        paused = self.repository.pause(
            reason="  <b>Provider maintenance</b>\nwindow  ",
            actor=" github-actions[bot]\x00 ",
            updated_at=NOW,
            previous=self.repository.load(),
        )

        self.assertTrue(paused.control.paused)
        self.assertEqual(paused.control.reason, "Provider maintenance window")
        self.assertEqual(paused.control.actor, "github-actions[bot]")
        self.assertEqual(
            paused.control.updated_at,
            datetime(2026, 8, 23, 12, 34, 56, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(paused.etag)

        stored = self.store.get(PUBLICATION_CONTROL_KEY)
        assert stored is not None
        self.assertLessEqual(len(stored.body), PUBLICATION_CONTROL_MAX_BYTES)
        self.assertEqual(stored.content_type, JSON_CONTENT_TYPE)
        self.assertEqual(stored.cache_control, POINTER_CACHE_CONTROL)
        self.assertEqual(
            set(json.loads(stored.body)),
            {"schemaVersion", "paused", "updatedAt", "reason", "actor"},
        )

    def test_resume_conditionally_replaces_pause(self):
        paused = self.repository.pause(
            reason="scheduled maintenance",
            actor="operator@example.com",
            updated_at=NOW,
            previous=self.repository.load(),
        )
        resumed = self.repository.resume(
            reason="source checks passed",
            actor="operator@example.com",
            updated_at=datetime(2026, 8, 23, 13, tzinfo=timezone.utc),
            previous=paused,
        )

        loaded = self.repository.load()
        self.assertEqual(loaded, resumed)
        self.assertTrue(loaded.control.enabled)
        self.assertEqual(loaded.control.reason, "source checks passed")
        self.assertNotEqual(paused.etag, resumed.etag)

    def test_invalid_or_corrupt_stored_values_fail_closed(self):
        invalid_bodies = (
            b'{"schemaVersion":1,"paused":false,"updatedAt":"2026-08-23T12:00:00Z",'
            b'"reason":"ok","actor":"one","actor":"two"}',
            b'{"schemaVersion":1,"paused":NaN,"updatedAt":"2026-08-23T12:00:00Z",'
            b'"reason":"ok","actor":"operator"}',
            b'{"schemaVersion":1,"paused":false,"updatedAt":"2026-08-23 12:00:00Z",'
            b'"reason":"ok","actor":"operator"}',
            b'{"schemaVersion":1,"paused":false,"updatedAt":"2026-08-23T12:00:00Z",'
            b'"reason":"<b>unsafe</b>","actor":"operator"}',
            b'{"schemaVersion":1,"paused":false,"updatedAt":"2026-08-23T12:00:00Z",'
            b'"reason":"ok","actor":"operator","unexpected":true}',
            b"x" * (PUBLICATION_CONTROL_MAX_BYTES + 1),
        )

        for index, body in enumerate(invalid_bodies):
            with self.subTest(index=index):
                previous = self.store.get(PUBLICATION_CONTROL_KEY)
                self.store.put(
                    PUBLICATION_CONTROL_KEY,
                    body,
                    content_type=JSON_CONTENT_TYPE,
                    cache_control=POINTER_CACHE_CONTROL,
                    if_match=previous.etag if previous is not None else None,
                    if_none_match=previous is None,
                )
                with self.assertRaises(PublicationControlIntegrityError):
                    self.repository.load()

    def test_stale_etag_conflict_fails_closed(self):
        paused = self.repository.pause(
            reason="first writer",
            actor="operator-one",
            updated_at=NOW,
            previous=self.repository.load(),
        )
        writer_one = self.repository.load()
        writer_two = self.repository.load()
        resumed = self.repository.resume(
            reason="checks passed",
            actor="operator-one",
            updated_at=datetime(2026, 8, 23, 13, tzinfo=timezone.utc),
            previous=writer_one,
        )

        with self.assertRaises(PublicationControlConflictError):
            self.repository.pause(
                reason="stale writer",
                actor="operator-two",
                updated_at=NOW,
                previous=writer_two,
            )

        self.assertNotEqual(paused.etag, resumed.etag)
        self.assertEqual(self.repository.load(), resumed)


if __name__ == "__main__":
    unittest.main()
