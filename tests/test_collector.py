import unittest
from datetime import datetime, timezone
from decimal import Decimal

from market_radar.collector import CollectionJob, collect_sources, default_jobs
from market_radar.domain import SourceDescriptor
from market_radar.sources import (
    CalendarEvent,
    Freshness,
    IndicatorObservation,
    Release,
    ReleaseKind,
    SourceResult,
    SourceStatus,
)

NOW = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
SOURCE = SourceDescriptor("official", "Official", "https://example.gov/feed", "public")


class FakeAdapter:
    def __init__(self, result=None, error=False):
        self.result = result
        self.error = error

    def fetch(self, at):
        if self.error:
            raise RuntimeError("secret detail")
        return self.result


def success(items):
    return SourceResult(
        items=tuple(items),
        retrieved_at=NOW,
        source_url=SOURCE.url,
        freshness=Freshness.FRESH,
        status=SourceStatus.OK,
    )


class CollectorTests(unittest.TestCase):
    def test_default_jobs_do_not_automate_the_blocked_bls_calendar(self):
        jobs = default_jobs(client=object(), fred_api_key=None)
        job_ids = {job.job_id for job in jobs}

        self.assertNotIn("bls-calendar", job_ids)
        self.assertIn("bea-calendar", job_ids)

    def test_normalizes_and_filters_source_outputs(self):
        indicator_job = CollectionJob(
            "indicators",
            "indicator",
            FakeAdapter(
                success([IndicatorObservation("us-treasury-10y", Decimal("4.31"), "percent", NOW)])
            ),
            SOURCE,
        )
        release_job = CollectionJob(
            "discovery",
            "release",
            FakeAdapter(
                success(
                    [
                        Release(
                            "Secure story",
                            "https://publisher.example/story",
                            "Publisher",
                            seen_at=NOW,
                            kind=ReleaseKind.DISCOVERY,
                        ),
                        Release(
                            "Insecure story",
                            "http://publisher.example/story",
                            "Publisher",
                            seen_at=NOW,
                            kind=ReleaseKind.DISCOVERY,
                        ),
                    ]
                )
            ),
            SOURCE,
        )
        calendar_job = CollectionJob(
            "calendar",
            "calendar",
            FakeAdapter(
                success(
                    [
                        CalendarEvent("cpi", "Consumer Price Index", NOW, "BLS", "US"),
                        CalendarEvent("ppi", "Producer Price Index", NOW, "BLS", "US"),
                    ]
                )
            ),
            SOURCE,
            calendar_keywords=("consumer price index",),
        )

        result = collect_sources(
            at=NOW,
            fred_api_key=None,
            jobs=(indicator_job, release_job, calendar_job),
        )

        self.assertEqual(result.indicators[0].display_value, "4.31%")
        self.assertEqual(len(result.releases), 1)
        self.assertEqual(result.releases[0].kind, "discovery")
        self.assertEqual(len(result.calendar), 1)
        self.assertEqual(len(result.source_health), 3)

    def test_adapter_exception_is_sanitized(self):
        job = CollectionJob("broken", "release", FakeAdapter(error=True), SOURCE)

        result = collect_sources(at=NOW, fred_api_key=None, jobs=(job,))

        self.assertEqual(result.releases, ())
        self.assertEqual(result.source_health[0].error_code, "ADAPTER_ERROR")
        self.assertNotIn("secret", str(result.source_health[0]))
