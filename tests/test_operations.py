from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from market_radar.cli import main
from market_radar.operations import OperationsService
from market_radar.publishing import (
    LATEST_KEY,
    LocalObjectStore,
    PublicationControlRepository,
    Publisher,
    PublishVerificationError,
)

NOW = datetime(2030, 1, 15, 13, 0, tzinfo=timezone.utc)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def valid_snapshot():
    return json.loads((PROJECT_ROOT / "examples" / "snapshot.v1.json").read_text(encoding="utf-8"))


class OperationsServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.public_store = LocalObjectStore(self.root / "public")
        self.control_repository = PublicationControlRepository(
            LocalObjectStore(self.root / "private")
        )
        self.publisher = Publisher(self.public_store, clock=lambda: NOW)
        self.service = OperationsService(
            self.control_repository,
            publisher=self.publisher,
            clock=lambda: NOW,
        )

    def test_pause_and_resume_are_explicit_private_transitions(self):
        paused = self.service.pause(reason="upstream investigation", actor="operator")
        resumed = self.service.resume(reason="checks passed", actor="operator")

        self.assertTrue(paused.control.control.paused)
        self.assertFalse(resumed.control.control.paused)
        self.assertEqual(self.control_repository.load(), resumed.control)
        self.assertIsNone(self.public_store.get(LATEST_KEY))

    def test_rollback_pauses_then_promotes_a_verified_snapshot(self):
        first_snapshot = valid_snapshot()
        first = self.publisher.publish(first_snapshot)
        second_snapshot = deepcopy(first_snapshot)
        second_snapshot["digest"]["summary"] = "Current interpretation before rollback."
        second = self.publisher.publish(second_snapshot)

        result = self.service.rollback(
            snapshot_key=first.snapshot_key,
            reason="bad upstream revision",
            actor="operator",
        )

        self.assertTrue(result.control.control.paused)
        self.assertIsNotNone(result.promotion)
        assert result.promotion is not None
        self.assertEqual(result.promotion.previous_snapshot_key, second.snapshot_key)
        latest = self.public_store.get(LATEST_KEY)
        assert latest is not None
        self.assertEqual(json.loads(latest.body)["snapshot"]["path"], first.snapshot_key)

    def test_failed_rollback_leaves_publication_paused(self):
        with self.assertRaises(PublishVerificationError):
            self.service.rollback(
                snapshot_key="unsafe.json",
                reason="suspected bad data",
                actor="operator",
            )

        self.assertTrue(self.control_repository.load().control.paused)
        self.assertIsNone(self.public_store.get(LATEST_KEY))

    def test_local_cli_pause_blocks_a_publishing_refresh_before_collection(self):
        control_dir = self.root / "cli-control"
        public_dir = self.root / "cli-public"
        output_dir = self.root / "cli-out"
        state_file = self.root / "cli-state.json"
        pause_code = main(
            [
                "ops",
                "pause",
                "--reason",
                "manual test",
                "--actor",
                "test-operator",
                "--state-object-store-dir",
                str(control_dir),
            ]
        )
        refresh_code = main(
            [
                "refresh",
                "--publish",
                "--target",
                "local",
                "--slot",
                "test-paused-slot",
                "--output-dir",
                str(output_dir),
                "--object-store-dir",
                str(public_dir),
                "--state-file",
                str(state_file),
                "--control-store-dir",
                str(control_dir),
            ]
        )

        self.assertEqual(pause_code, 0)
        self.assertEqual(refresh_code, 0)
        self.assertIsNone(LocalObjectStore(public_dir).get(LATEST_KEY))
        self.assertFalse(state_file.exists())
        reports = list((output_dir / "reports").glob("*.json"))
        self.assertEqual(len(reports), 1)
        self.assertEqual(json.loads(reports[0].read_text())["reason"], "publication-paused")
