from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from market_radar.sources.base import (  # noqa: E402
    Freshness,
    HttpResponse,
    MAX_RESPONSE_BYTES,
    ReleaseKind,
    SourceStatus,
    error_result,
    retrieve,
)
from market_radar.sources.calendar import (  # noqa: E402
    bea_calendar_adapter,
    bls_calendar_adapter,
)
from market_radar.sources.cbrt import CbrtUsdTryAdapter  # noqa: E402
from market_radar.sources.feeds import (  # noqa: E402
    cbrt_press_adapter,
    ecb_press_adapter,
    fed_press_adapter,
)
from market_radar.sources.fred import FredBroadUsdAdapter  # noqa: E402
from market_radar.sources.gdelt import GdeltDocAdapter  # noqa: E402
from market_radar.sources.treasury import TreasuryYieldAdapter  # noqa: E402


UTC = timezone.utc
RETRIEVED = datetime(2026, 8, 23, 15, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "sources"


class FixtureClient:
    def __init__(self, fixture: str = "", status: int = 200, error=None):
        self.fixture = fixture
        self.status = status
        self.error = error
        self.calls = []

    def get(self, url, *, headers=None, timeout=20.0):
        self.calls.append((url, dict(headers or {}), timeout))
        if self.error is not None:
            raise self.error
        body = (FIXTURES / self.fixture).read_bytes() if self.fixture else b""
        return HttpResponse(self.status, body, {"Content-Type": "fixture"})


class IndicatorSourceTests(unittest.TestCase):
    def test_treasury_yields_and_curve_keep_observation_dates(self):
        result = TreasuryYieldAdapter(FixtureClient("treasury_yield.xml")).fetch(
            RETRIEVED
        )

        self.assert_result_ok(result)
        self.assertEqual(len(result.items), 6)
        latest = {
            item.indicator_id: item
            for item in result.items
            if item.observed_at.date().isoformat() == "2026-08-21"
        }
        self.assertEqual(latest["us-treasury-2y"].value, Decimal("3.80"))
        self.assertEqual(latest["us-treasury-10y"].value, Decimal("4.25"))
        self.assertEqual(latest["us-curve-2s10s"].value, Decimal("45.00"))
        self.assertEqual(latest["us-curve-2s10s"].unit, "basis_points")

    def test_treasury_marks_old_observations_stale(self):
        retrieved = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        result = TreasuryYieldAdapter(FixtureClient("treasury_yield.xml")).fetch(
            retrieved
        )

        self.assertEqual(result.status, SourceStatus.DEGRADED)
        self.assertEqual(result.freshness, Freshness.STALE)
        self.assertEqual(result.error_code, "STALE_DATA")
        self.assertTrue(result.items)

    def test_treasury_reads_current_grouped_xml_shape(self):
        retrieved = datetime(2026, 8, 4, 21, 0, tzinfo=UTC)
        result = TreasuryYieldAdapter(
            FixtureClient("treasury_yield_current.xml")
        ).fetch(retrieved)

        self.assertEqual(result.status, SourceStatus.OK)
        self.assertEqual(len(result.items), 6)
        latest = {
            item.indicator_id: item
            for item in result.items
            if item.observed_at.date().isoformat() == "2026-08-04"
        }
        self.assertEqual(latest["us-treasury-2y"].value, Decimal("4.20"))
        self.assertEqual(latest["us-treasury-10y"].value, Decimal("4.68"))
        self.assertEqual(latest["us-curve-2s10s"].value, Decimal("48.00"))
        self.assertEqual(
            latest["us-curve-2s10s"].observed_at,
            datetime(2026, 8, 4, 19, 30, tzinfo=UTC),
        )

    def test_cbrt_reads_only_usd_forex_buying(self):
        result = CbrtUsdTryAdapter(FixtureClient("cbrt_today.xml")).fetch(RETRIEVED)

        self.assert_result_ok(result)
        self.assertEqual(len(result.items), 1)
        observation = result.items[0]
        self.assertEqual(observation.indicator_id, "cbrt-usd-try")
        self.assertEqual(observation.value, Decimal("40.9876"))
        self.assertEqual(observation.unit, "TRY_per_USD")
        self.assertEqual(observation.observed_at.date().isoformat(), "2026-08-21")

    def test_fred_requires_key_and_does_not_expose_it_in_result(self):
        missing_client = FixtureClient("fred_dtwexbgs.json")
        missing = FredBroadUsdAdapter(missing_client, None).fetch(RETRIEVED)
        self.assertEqual(missing.status, SourceStatus.ERROR)
        self.assertEqual(missing.error_code, "AUTH_MISSING")
        self.assertEqual(missing_client.calls, [])

        client = FixtureClient("fred_dtwexbgs.json")
        result = FredBroadUsdAdapter(client, "fixture-secret").fetch(RETRIEVED)
        self.assert_result_ok(result)
        self.assertEqual(len(result.items), 3)
        self.assertTrue(
            all(item.indicator_id == "fed-broad-usd" for item in result.items)
        )
        self.assertEqual(result.items[-1].value, Decimal("120.9987"))
        self.assertEqual(result.items[-1].observed_at.date().isoformat(), "2026-08-21")
        request_query = parse_qs(urlparse(client.calls[0][0]).query)
        self.assertEqual(request_query["api_key"], ["fixture-secret"])
        self.assertEqual(request_query["sort_order"], ["desc"])
        self.assertNotIn("fixture-secret", result.source_url)

    def assert_result_ok(self, result):
        self.assertEqual(result.retrieved_at, RETRIEVED)
        self.assertTrue(result.source_url.startswith("https://"))
        self.assertEqual(result.freshness, Freshness.FRESH)
        self.assertEqual(result.status, SourceStatus.OK)
        self.assertIsNone(result.error_code)


class ReleaseSourceTests(unittest.TestCase):
    def test_fed_rss_metadata(self):
        result = fed_press_adapter(FixtureClient("fed_rss.xml")).fetch(RETRIEVED)
        self.assert_result_ok(result)
        release = result.items[0]
        self.assertEqual(release.title, "Federal Reserve issues FOMC statement")
        self.assertEqual(release.publisher, "Federal Reserve Board")
        self.assertEqual(release.category, "Monetary Policy")
        self.assertEqual(release.published_at, datetime(2026, 8, 22, 18, tzinfo=UTC))

    def test_ecb_atom_relative_link_and_time(self):
        result = ecb_press_adapter(FixtureClient("ecb_atom.xml")).fetch(RETRIEVED)
        self.assert_result_ok(result)
        release = result.items[0]
        self.assertEqual(release.title, "Monetary policy decisions")
        self.assertEqual(release.publisher, "European Central Bank")
        self.assertEqual(release.category, "Monetary policy")
        self.assertEqual(
            release.url,
            "https://www.ecb.europa.eu/press/pr/date/2026/html/"
            "ecb.mp260821~example.en.html",
        )
        self.assertEqual(release.published_at, datetime(2026, 8, 21, 12, 15, tzinfo=UTC))

    def test_cbrt_rss_namespaced_date_and_relative_link(self):
        result = cbrt_press_adapter(FixtureClient("cbrt_rss.xml")).fetch(RETRIEVED)
        self.assert_result_ok(result)
        release = result.items[0]
        self.assertEqual(release.title, "Press Release on Interest Rates")
        self.assertEqual(
            release.publisher, "Central Bank of the Republic of Turkey"
        )
        self.assertEqual(release.published_at, datetime(2026, 8, 20, 8, tzinfo=UTC))
        self.assertTrue(release.url.startswith("https://www.tcmb.gov.tr/"))

    def test_gdelt_keeps_discovery_metadata_only_and_deduplicates(self):
        adapter = GdeltDocAdapter(
            FixtureClient("gdelt_doc.json"), "central bank OR inflation"
        )
        result = adapter.fetch(RETRIEVED)
        self.assert_result_ok(result)
        self.assertEqual(len(result.items), 2)
        first = result.items[0]
        self.assertEqual(first.kind, ReleaseKind.DISCOVERY)
        self.assertEqual(first.seen_at, datetime(2026, 8, 23, 14, 30, tzinfo=UTC))
        self.assertEqual(first.url, "https://example.com/macro/story?id=7")
        self.assertEqual(first.language, "English")
        self.assertFalse(hasattr(first, "body"))
        self.assertFalse(hasattr(first, "image"))
        query = parse_qs(urlparse(result.source_url).query)
        self.assertEqual(query["mode"], ["artlist"])
        self.assertEqual(query["format"], ["json"])

    def test_network_exception_collapses_to_safe_code(self):
        client = FixtureClient(error=RuntimeError("api_key=do-not-leak"))
        result = fed_press_adapter(client).fetch(RETRIEVED)
        self.assertEqual(result.status, SourceStatus.ERROR)
        self.assertEqual(result.freshness, Freshness.UNKNOWN)
        self.assertEqual(result.error_code, "NETWORK_ERROR")
        self.assertNotIn("do-not-leak", repr(result))

    def test_unknown_error_detail_is_reduced_to_generic_code(self):
        result = error_result(
            "https://example.com/source", RETRIEVED, "api_key=do-not-leak"
        )

        self.assertEqual(result.error_code, "SOURCE_ERROR")
        self.assertNotIn("do-not-leak", repr(result))

    def test_oversized_response_is_rejected_before_parsing(self):
        client = FixtureClient()
        client.get = lambda *args, **kwargs: HttpResponse(
            200, b"x" * (MAX_RESPONSE_BYTES + 1), {}
        )

        body, error = retrieve(client, "https://example.com/large")

        self.assertIsNone(body)
        self.assertEqual(error, "RESPONSE_TOO_LARGE")

    def assert_result_ok(self, result):
        self.assertEqual(result.retrieved_at, RETRIEVED)
        self.assertEqual(result.freshness, Freshness.FRESH)
        self.assertEqual(result.status, SourceStatus.OK)
        self.assertIsNone(result.error_code)


class CalendarSourceTests(unittest.TestCase):
    def test_bls_calendar_converts_eastern_time_and_skips_cancelled(self):
        result = bls_calendar_adapter(FixtureClient("bls.ics")).fetch(RETRIEVED)
        self.assert_result_ok(result)
        self.assertEqual(len(result.items), 1)
        event = result.items[0]
        self.assertEqual(event.title, "Consumer Price Index")
        self.assertEqual(event.scheduled_at, datetime(2026, 8, 11, 12, 30, tzinfo=UTC))
        self.assertEqual(event.ends_at, datetime(2026, 8, 11, 13, 0, tzinfo=UTC))
        self.assertEqual(event.authority, "U.S. Bureau of Labor Statistics")
        self.assertFalse(event.tentative)

    def test_bea_calendar_handles_windows_timezone_folded_text_and_utc(self):
        result = bea_calendar_adapter(FixtureClient("bea.ics")).fetch(RETRIEVED)
        self.assert_result_ok(result)
        self.assertEqual(len(result.items), 2)
        gdp, pce = result.items
        self.assertEqual(
            gdp.title,
            "Gross Domestic Product, 2nd Estimate and Corporate Profits "
            "(Preliminary)",
        )
        self.assertEqual(gdp.scheduled_at, datetime(2026, 8, 27, 12, 30, tzinfo=UTC))
        self.assertEqual(pce.scheduled_at, datetime(2026, 8, 28, 12, 30, tzinfo=UTC))
        self.assertTrue(pce.tentative)

    def assert_result_ok(self, result):
        self.assertEqual(result.retrieved_at, RETRIEVED)
        self.assertTrue(result.source_url.startswith("https://"))
        self.assertEqual(result.freshness, Freshness.FRESH)
        self.assertEqual(result.status, SourceStatus.OK)
        self.assertIsNone(result.error_code)


if __name__ == "__main__":
    unittest.main()
