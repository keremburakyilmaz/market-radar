import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from market_radar.domain import (
    CollectedCalendarEvent,
    CollectedIndicator,
    CollectedRelease,
    CollectedSourceHealth,
    CollectionBundle,
    SourceDescriptor,
)
from market_radar.snapshot import build_snapshot
from market_radar.state import RadarState

NOW = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
TREASURY = SourceDescriptor(
    "us-treasury",
    "US Treasury",
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates",
    "public-domain",
)
FED = SourceDescriptor(
    "federal-reserve",
    "Federal Reserve",
    "https://fred.stlouisfed.org/series/DTWEXBGS",
    "public-domain-with-attribution",
)
GDELT = SourceDescriptor(
    "gdelt-global-discovery",
    "GDELT discovery",
    "https://www.gdeltproject.org/",
    "open-dataset-discovery-only",
)


def observed(indicator_id, value, source=TREASURY, days_ago=1):
    observation_time = NOW - timedelta(days=days_ago)
    return CollectedIndicator(
        indicator_id=indicator_id,
        label=indicator_id,
        value=Decimal(str(value)),
        unit="index" if indicator_id == "fed-broad-usd" else "percent",
        display_value=str(value),
        observed_at=observation_time,
        retrieved_at=NOW - timedelta(minutes=1),
        freshness="fresh",
        source=source,
    )


class SnapshotBuilderTests(unittest.TestCase):
    def bundle(self):
        indicators = (
            observed("us-treasury-2y", 4.0),
            observed("us-treasury-10y", 4.3),
            observed("fed-broad-usd", 119, FED),
        )
        releases = (
            CollectedRelease(
                release_id="fed-release-1",
                title="Federal Reserve issues FOMC statement",
                url="https://www.federalreserve.gov/newsevents/pressreleases/monetary20260823a.htm",
                published_at=NOW - timedelta(hours=1),
                retrieved_at=NOW,
                source=FED,
                category="central-bank",
            ),
        )
        calendar = (
            CollectedCalendarEvent(
                event_id="bls-cpi",
                name="US Consumer Price Index",
                scheduled_at=NOW + timedelta(days=2),
                authority="BLS",
                region="US",
                source_url="https://www.bls.gov/schedule/news_release/cpi.htm",
                checked_at=NOW,
            ),
        )
        health = (
            CollectedSourceHealth(TREASURY, "ok", NOW, 2),
            CollectedSourceHealth(FED, "ok", NOW, 2),
        )
        usd_history = (
            observed("fed-broad-usd", 115, FED, days_ago=30),
            observed("fed-broad-usd", 119, FED),
        )
        return CollectionBundle(
            indicators,
            releases,
            calendar,
            health,
            {"fed-broad-usd": usd_history},
        )

    def test_builds_truthful_snapshot_and_next_state(self):
        result = build_snapshot(
            self.bundle(),
            RadarState(indicator_values={"us-treasury-10y": 4.2}),
            generated_at=NOW,
            successful_slot="2026-08-23T12",
        )

        snapshot = result.snapshot
        self.assertEqual(snapshot["schemaVersion"], 1)
        self.assertEqual(snapshot["pipeline"]["status"], "healthy")
        self.assertEqual(snapshot["pipeline"]["coverage"]["failedSources"], 0)
        self.assertEqual(len(snapshot["macroConditions"]["drivers"]), 3)
        self.assertEqual(snapshot["priorityDevelopments"][0]["impact"], "high")
        commentary = snapshot["digest"]["commentary"]
        self.assertEqual(commentary["generation"]["mode"], "deterministic")
        self.assertIn(
            f"{snapshot['macroConditions']['score']:.1f}/100",
            commentary["dataRead"]["body"],
        )
        self.assertEqual(
            commentary["newsRead"]["evidenceIds"],
            [snapshot["stories"][0]["id"]],
        )
        self.assertEqual(
            commentary["watchNext"]["evidenceIds"][0],
            snapshot["calendar"][0]["id"],
        )
        self.assertEqual(result.next_state.previous_snapshot_id, snapshot["id"])
        self.assertIn("us-treasury-10y", result.next_state.indicator_records)
        self.assertIn("2026-08-23T12", result.next_state.successful_slots)

    def test_seen_release_is_not_repeated(self):
        bundle = self.bundle()
        result = build_snapshot(
            bundle,
            RadarState(seen_release_urls=(bundle.releases[0].url,)),
            generated_at=NOW,
        )
        self.assertEqual(result.snapshot["priorityDevelopments"], [])
        self.assertEqual(result.snapshot["stories"], [])

    def test_missing_broad_usd_publishes_degraded(self):
        bundle = self.bundle()
        reduced = CollectionBundle(
            indicators=tuple(
                item for item in bundle.indicators if item.indicator_id != "fed-broad-usd"
            ),
            releases=bundle.releases,
            calendar=bundle.calendar,
            source_health=(
                bundle.source_health[0],
                CollectedSourceHealth(FED, "error", NOW, 0, "AUTH_ERROR"),
            ),
        )
        result = build_snapshot(reduced, RadarState(), generated_at=NOW)
        self.assertEqual(result.snapshot["pipeline"]["status"], "degraded")
        self.assertEqual(result.snapshot["pipeline"]["coverage"]["failedSources"], 1)

    def test_discovery_news_requires_specific_macro_relevance(self):
        bundle = self.bundle()
        relevant = CollectedRelease(
            release_id="gdelt-relevant",
            title="Federal Reserve interest rate outlook returns to focus",
            url="https://example.com/federal-reserve-outlook",
            published_at=NOW - timedelta(minutes=10),
            retrieved_at=NOW,
            source=GDELT,
            publisher="example.com",
            kind="discovery",
        )
        irrelevant = CollectedRelease(
            release_id="gdelt-irrelevant",
            title="Meeting employment demand is a challenge for education supply",
            url="https://example.com/education-employment",
            published_at=NOW - timedelta(minutes=5),
            retrieved_at=NOW,
            source=GDELT,
            publisher="example.com",
            kind="discovery",
        )
        with_discovery = CollectionBundle(
            indicators=bundle.indicators,
            releases=(*bundle.releases, relevant, irrelevant),
            calendar=bundle.calendar,
            source_health=(
                *bundle.source_health,
                CollectedSourceHealth(GDELT, "ok", NOW, 2),
            ),
            histories=bundle.histories,
        )

        snapshot = build_snapshot(with_discovery, RadarState(), generated_at=NOW).snapshot
        story_ids = {story["id"] for story in snapshot["stories"]}

        self.assertIn("gdelt-relevant", story_ids)
        self.assertNotIn("gdelt-irrelevant", story_ids)

    def test_last_good_indicator_is_retained_as_stale(self):
        first = build_snapshot(self.bundle(), RadarState(), generated_at=NOW)
        bundle = self.bundle()
        reduced = CollectionBundle(
            indicators=tuple(
                item for item in bundle.indicators if item.indicator_id != "fed-broad-usd"
            ),
            releases=bundle.releases,
            calendar=bundle.calendar,
            source_health=(
                bundle.source_health[0],
                CollectedSourceHealth(FED, "error", NOW, 0, "NETWORK_ERROR"),
            ),
        )

        second = build_snapshot(
            reduced,
            first.next_state,
            generated_at=NOW + timedelta(hours=1),
        )

        broad_usd = next(
            item for item in second.snapshot["indicators"] if item["id"] == "fed-broad-usd"
        )
        self.assertEqual(broad_usd["freshness"]["status"], "stale")
        self.assertEqual(second.snapshot["pipeline"]["status"], "degraded")
