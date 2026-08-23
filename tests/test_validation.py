import copy
import unittest

from market_radar.validation import ContractValidationError, validate_manifest, validate_snapshot


def valid_snapshot():
    source = {"id": "us-treasury", "name": "US Treasury", "url": "https://home.treasury.gov/"}
    return {
        "schemaVersion": 1,
        "id": "snapshot-test",
        "generatedAt": "2026-08-23T12:00:00Z",
        "validUntil": "2026-08-23T20:00:00Z",
        "pipeline": {"status": "healthy", "coverage": {"available": 3, "required": 2}},
        "macroConditions": {
            "score": 64,
            "label": "elevated",
            "summary": "Long rates are the primary pressure input.",
            "methodologyVersion": "macro-pressure-v1",
            "drivers": [],
        },
        "indicators": [
            {
                "id": "us-10y",
                "label": "US 10Y",
                "value": 4.31,
                "unit": "percent",
                "displayValue": "4.31%",
                "observedAt": "2026-08-22T19:30:00Z",
                "retrievedAt": "2026-08-23T11:58:00Z",
                "freshness": "fresh",
                "source": source,
            }
        ],
        "priorityDevelopments": [],
        "stories": [],
        "calendar": [],
        "digest": {"summary": "Rates remain restrictive.", "watch": []},
        "sources": [{"id": "us-treasury", "name": "US Treasury", "url": source["url"]}],
    }


class SnapshotValidationTests(unittest.TestCase):
    def test_valid_snapshot_is_accepted(self):
        self.assertEqual(validate_snapshot(valid_snapshot())["schemaVersion"], 1)

    def test_unknown_schema_and_unsafe_url_are_rejected(self):
        candidate = valid_snapshot()
        candidate["schemaVersion"] = 2
        candidate["indicators"][0]["source"]["url"] = "javascript:alert(1)"

        with self.assertRaises(ContractValidationError) as raised:
            validate_snapshot(candidate)

        self.assertIn("unsupported schema version", str(raised.exception))
        self.assertIn("absolute HTTPS URL", str(raised.exception))

    def test_duplicate_indicator_ids_are_rejected(self):
        candidate = valid_snapshot()
        candidate["indicators"].append(copy.deepcopy(candidate["indicators"][0]))

        with self.assertRaisesRegex(ContractValidationError, "must be unique"):
            validate_snapshot(candidate)


class ManifestValidationTests(unittest.TestCase):
    def test_traversal_path_is_rejected(self):
        manifest = {
            "manifestVersion": 1,
            "publishedAt": "2026-08-23T12:01:00Z",
            "snapshot": {
                "schemaVersion": 1,
                "id": "snapshot-test",
                "path": "../snapshot.json",
                "generatedAt": "2026-08-23T12:00:00Z",
                "validUntil": "2026-08-23T20:00:00Z",
                "sizeBytes": 100,
                "sha256": "a" * 64,
            },
        }

        with self.assertRaisesRegex(ContractValidationError, "safe immutable"):
            validate_manifest(manifest)

