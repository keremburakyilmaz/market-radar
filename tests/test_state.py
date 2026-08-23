import json
import tempfile
import unittest
from pathlib import Path

from market_radar.state import RadarState, load_state, save_state


class StateTests(unittest.TestCase):
    def test_missing_state_bootstraps_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            state = load_state(Path(directory) / "state.json")
        self.assertIsNone(state.previous_snapshot_id)

    def test_state_round_trip_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            expected = RadarState(
                previous_snapshot_id="mr-test",
                indicator_values={"us-treasury-10y": 4.31},
                indicator_records={
                    "us-treasury-10y": {
                        "label": "US Treasury 10Y",
                        "value": 4.31,
                    }
                },
                seen_release_urls=tuple(f"https://example.com/{index}" for index in range(2005)),
                successful_slots=("2026-08-23T12",),
            )

            save_state(path, expected)
            actual = load_state(path)

            self.assertEqual(actual.previous_snapshot_id, "mr-test")
            self.assertEqual(actual.indicator_values["us-treasury-10y"], 4.31)
            self.assertEqual(actual.indicator_records["us-treasury-10y"]["value"], 4.31)
            self.assertEqual(len(actual.seen_release_urls), 2000)
            self.assertNotIn("https://example.com/0", actual.seen_release_urls)

    def test_unknown_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"stateVersion": 2}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported"):
                load_state(path)
