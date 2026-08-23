import copy
import json
import unittest
from pathlib import Path

from market_radar.validation import ContractValidationError, validate_manifest, validate_snapshot


def valid_snapshot():
    path = Path(__file__).resolve().parents[1] / "examples" / "snapshot.v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


class SnapshotValidationTests(unittest.TestCase):
    def test_valid_snapshot_is_accepted(self):
        self.assertEqual(validate_snapshot(valid_snapshot())["schemaVersion"], 1)

    def test_unknown_schema_and_unsafe_url_are_rejected(self):
        candidate = valid_snapshot()
        candidate["schemaVersion"] = 2
        candidate["indicators"][0]["source"]["sourceUrl"] = "javascript:alert(1)"

        with self.assertRaises(ContractValidationError) as raised:
            validate_snapshot(candidate)

        self.assertIn("expected const 1", str(raised.exception))
        self.assertIn("string does not match pattern", str(raised.exception))

    def test_duplicate_indicator_ids_are_rejected(self):
        candidate = valid_snapshot()
        candidate["indicators"].append(copy.deepcopy(candidate["indicators"][0]))

        with self.assertRaisesRegex(ContractValidationError, "must be unique"):
            validate_snapshot(candidate)


class ManifestValidationTests(unittest.TestCase):
    def test_traversal_path_is_rejected(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "manifest.v1.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["snapshot"]["path"] = "../snapshot.json"

        with self.assertRaisesRegex(ContractValidationError, "string does not match pattern"):
            validate_manifest(manifest)
