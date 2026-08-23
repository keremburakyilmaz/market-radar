import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from market_radar.domain import (
    CollectedIndicator,
    CollectedSourceHealth,
    CollectionBundle,
    SourceDescriptor,
)
from market_radar.pipeline import PipelineRunner
from market_radar.publishing import LocalObjectStore, Publisher


NOW = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
TREASURY = SourceDescriptor(
    "us-treasury-yields",
    "US Treasury",
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates",
    "us-government-public-domain",
)
FED = SourceDescriptor(
    "fed-broad-usd",
    "Federal Reserve via FRED",
    "https://fred.stlouisfed.org/series/DTWEXBGS",
    "us-government-public-domain-with-fred-attribution",
)


def indicator(indicator_id, value, source, observed_at):
    return CollectedIndicator(
        indicator_id=indicator_id,
        label=indicator_id,
        value=Decimal(str(value)),
        unit="index" if indicator_id == "fed-broad-usd" else "percent",
        display_value=str(value),
        observed_at=observed_at,
        retrieved_at=NOW,
        freshness="fresh",
        source=source,
        market_tags=("global",),
    )


def bundle():
    observations = (
        indicator("us-treasury-2y", 4.0, TREASURY, NOW - timedelta(days=1)),
        indicator("us-treasury-10y", 4.3, TREASURY, NOW - timedelta(days=1)),
        indicator("fed-broad-usd", 115.0, FED, NOW - timedelta(days=30)),
        indicator("fed-broad-usd", 119.0, FED, NOW - timedelta(days=1)),
    )
    return CollectionBundle(
        indicators=observations,
        releases=(),
        calendar=(),
        source_health=(
            CollectedSourceHealth(TREASURY, "ok", NOW, 2),
            CollectedSourceHealth(FED, "ok", NOW, 2),
        ),
        histories={"fed-broad-usd": observations[-2:]},
    )


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = LocalObjectStore(self.root / "objects")
        self.calls = 0

    def collect(self, at):
        self.calls += 1
        return bundle()

    def runner(self):
        return PipelineRunner(
            collector=self.collect,
            output_dir=self.root / "out",
            publisher=Publisher(self.store, clock=lambda: NOW),
            local_state_path=self.root / "state" / "state.json",
            clock=lambda: NOW,
        )

    def test_dry_run_writes_candidate_without_advancing_state(self):
        outcome = self.runner().run(publish=False)

        self.assertFalse(outcome.published)
        self.assertTrue(outcome.candidate_path.is_file())
        self.assertFalse((self.root / "state" / "state.json").exists())
        self.assertIsNone(self.store.get("v1/latest.json"))

    def test_publication_smokes_and_advances_state(self):
        outcome = self.runner().run(publish=True, slot="2026-08-23T12")

        self.assertTrue(outcome.published)
        self.assertIsNotNone(self.store.get("v1/latest.json"))
        state = json.loads((self.root / "state" / "state.json").read_text())
        self.assertIn("2026-08-23T12", state["successfulSlots"])
        report = json.loads(outcome.report_path.read_text())
        self.assertEqual(report["status"], "success")
        self.assertNotIn("FRED_API_KEY", str(report))

    def test_repeated_successful_slot_is_a_no_op(self):
        runner = self.runner()
        runner.run(publish=True, slot="2026-08-23T12")
        outcome = runner.run(publish=True, slot="2026-08-23T12")

        self.assertTrue(outcome.no_op)
        self.assertEqual(self.calls, 1)

