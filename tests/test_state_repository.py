import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from market_radar.publishing import (
    JSON_CONTENT_TYPE,
    POINTER_CACHE_CONTROL,
    LocalObjectStore,
    StateConflictError,
    StateIntegrityError,
    StateRepository,
)
from market_radar.state import RadarState

NOW = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)


class StateRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = LocalObjectStore(Path(self.temporary_directory.name))
        self.repository = StateRepository(self.store)

    def test_bootstrap_save_and_load(self):
        previous = self.repository.load()
        state = RadarState(
            previous_snapshot_id="mr-20260823t120000z",
            indicator_values={"us-treasury-10y": 4.31},
        )

        saved = self.repository.save(
            state,
            created_at=NOW,
            source_snapshot_key="v1/snapshots/2026/08/23/test-{}{}.json".format("a" * 32, "a" * 32),
            previous=previous,
        )
        loaded = self.repository.load()

        self.assertTrue(saved.checkpoint_created)
        self.assertEqual(loaded.state, state)
        self.assertEqual(loaded.checkpoint_key, saved.checkpoint_key)
        self.assertIsNotNone(loaded.pointer_etag)

    def test_corrupt_checkpoint_fails_closed(self):
        previous = self.repository.load()
        saved = self.repository.save(
            RadarState(previous_snapshot_id="mr-test"),
            created_at=NOW,
            source_snapshot_key="v1/snapshots/2026/08/23/test.json",
            previous=previous,
        )
        checkpoint = self.store.get(saved.checkpoint_key)
        assert checkpoint is not None
        self.store.put(
            saved.checkpoint_key,
            checkpoint.body + b"x",
            content_type=checkpoint.content_type or JSON_CONTENT_TYPE,
            cache_control=checkpoint.cache_control or POINTER_CACHE_CONTROL,
            if_match=checkpoint.etag,
        )

        with self.assertRaisesRegex(StateIntegrityError, "size mismatch"):
            self.repository.load()

    def test_stale_pointer_etag_fails_closed(self):
        previous = self.repository.load()
        first = self.repository.save(
            RadarState(previous_snapshot_id="mr-one"),
            created_at=NOW,
            source_snapshot_key="v1/snapshots/2026/08/23/one.json",
            previous=previous,
        )
        current = self.repository.load()
        pointer = self.store.get("state/latest.json")
        assert pointer is not None
        replacement = json.loads(pointer.body)
        replacement["actor"] = "other"
        self.store.put(
            "state/latest.json",
            json.dumps(replacement).encode("utf-8"),
            content_type=JSON_CONTENT_TYPE,
            cache_control=POINTER_CACHE_CONTROL,
            if_match=pointer.etag,
        )

        with self.assertRaises(StateConflictError):
            self.repository.save(
                RadarState(previous_snapshot_id="mr-two"),
                created_at=NOW,
                source_snapshot_key="v1/snapshots/2026/08/23/two.json",
                previous=current,
            )
        self.assertTrue(first.checkpoint_key.startswith("state/checkpoints/"))
