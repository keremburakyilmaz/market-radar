import unittest
from datetime import datetime, timezone
from decimal import Decimal

from market_radar.domain import CollectedIndicator, SourceDescriptor
from market_radar.scoring import latest_indicators, score_macro_conditions

SOURCE = SourceDescriptor(
    source_id="official",
    name="Official source",
    url="https://example.gov/data",
    license_class="public-domain",
)


def indicator(indicator_id, value, day=23):
    timestamp = datetime(2026, 8, day, 12, tzinfo=timezone.utc)
    return CollectedIndicator(
        indicator_id=indicator_id,
        label=indicator_id,
        value=Decimal(str(value)),
        unit="index" if indicator_id == "fed-broad-usd" else "percent",
        display_value=str(value),
        observed_at=timestamp,
        retrieved_at=datetime(2026, 8, 23, 13, tzinfo=timezone.utc),
        freshness="fresh",
        source=SOURCE,
    )


class MacroScoringTests(unittest.TestCase):
    def test_score_uses_available_official_drivers(self):
        observations = [
            indicator("us-treasury-2y", 4.06),
            indicator("us-treasury-10y", 4.31),
            indicator("us-curve-2s10s", 25),
        ]

        result = score_macro_conditions(latest_indicators(observations), {})

        self.assertEqual(result.label, "moderate pressure")
        self.assertEqual(len(result.drivers), 2)
        self.assertIn("US 10Y", result.summary)

    def test_broad_usd_momentum_is_a_third_driver(self):
        observations = [
            indicator("us-treasury-10y", 4.7),
            indicator("us-curve-2s10s", -20),
        ]
        history = {
            "fed-broad-usd": [
                indicator("fed-broad-usd", 115, day=2),
                indicator("fed-broad-usd", 119, day=23),
            ]
        }

        result = score_macro_conditions(latest_indicators(observations), history)

        self.assertEqual(len(result.drivers), 3)
        self.assertGreater(result.score, 60)

    def test_one_driver_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "at least two"):
            score_macro_conditions(latest_indicators([indicator("us-treasury-10y", 4.1)]), {})
