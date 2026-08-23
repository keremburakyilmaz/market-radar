from __future__ import annotations

import hashlib
import json
import re
import unittest
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Iterator, Tuple
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas"
EXAMPLE_DIR = ROOT / "examples"

SNAPSHOT_TOP_LEVEL_KEYS = {
    "schemaVersion",
    "id",
    "generatedAt",
    "validUntil",
    "pipeline",
    "macroConditions",
    "indicators",
    "priorityDevelopments",
    "stories",
    "calendar",
    "digest",
    "sources",
}
V1_INDICATOR_IDS = {
    "us-treasury-2y",
    "us-treasury-10y",
    "us-curve-2s10s",
    "fed-broad-usd",
    "cbrt-usd-try",
}
SNAPSHOT_PATH_RE = re.compile(
    r"^v1/snapshots/"
    r"(?P<year>[0-9]{4})/(?P<month>[0-9]{2})/(?P<day>[0-9]{2})/"
    r"(?P<timestamp>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}Z)-"
    r"(?P<sha256>[a-f0-9]{64})\.json$"
)
UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$"
)
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _reject_non_finite(value: str) -> None:
    raise ValueError("non-finite JSON number is forbidden: {0}".format(value))


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle, parse_constant=_reject_non_finite)
    if not isinstance(value, dict):
        raise AssertionError("expected a JSON object in {0}".format(path))
    return value


def canonical_json_bytes(value: Any) -> bytes:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (encoded + "\n").encode("utf-8")


def parse_utc(value: str) -> datetime:
    if not UTC_TIMESTAMP_RE.fullmatch(value):
        raise AssertionError("timestamp is not canonical UTC: {0}".format(value))
    return datetime.fromisoformat(value[:-1] + "+00:00")


def walk(value: Any, path: str = "$") -> Iterator[Tuple[str, Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, "{0}.{1}".format(path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, "{0}[{1}]".format(path, index))


def assert_exact_keys(
    case: unittest.TestCase,
    value: Dict[str, Any],
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> None:
    required_keys = set(required)
    allowed_keys = required_keys | set(optional)
    case.assertTrue(required_keys.issubset(value), required_keys - set(value))
    case.assertEqual(set(value) - allowed_keys, set())


def assert_https_url(case: unittest.TestCase, value: str) -> None:
    parsed = urlsplit(value)
    case.assertEqual(parsed.scheme, "https")
    case.assertTrue(parsed.netloc)
    case.assertIsNone(parsed.username)
    case.assertIsNone(parsed.password)


def schema_subset_errors(
    instance: Any,
    schema: Dict[str, Any],
    root_schema: Dict[str, Any],
    path: str = "$",
) -> list:
    """Validate the JSON Schema keywords used by these contracts with stdlib only."""

    errors = []
    reference = schema.get("$ref")
    if reference is not None:
        definition = reference.rsplit("/", 1)[-1]
        return schema_subset_errors(instance, root_schema["$defs"][definition], root_schema, path)

    branches = schema.get("oneOf")
    if branches is not None:
        branch_errors = [
            schema_subset_errors(instance, branch, root_schema, path) for branch in branches
        ]
        if sum(not branch for branch in branch_errors) != 1:
            errors.append("{0}: expected exactly one oneOf branch".format(path))
        return errors

    if "const" in schema and instance != schema["const"]:
        errors.append("{0}: expected const {1!r}".format(path, schema["const"]))
    if "enum" in schema and instance not in schema["enum"]:
        errors.append("{0}: value is not in enum".format(path))

    expected_type = schema.get("type")
    type_matches = {
        "object": isinstance(instance, dict),
        "array": isinstance(instance, list),
        "string": isinstance(instance, str),
        "integer": isinstance(instance, int) and not isinstance(instance, bool),
        "number": isinstance(instance, (int, float)) and not isinstance(instance, bool),
        "null": instance is None,
    }
    if expected_type is not None and not type_matches[expected_type]:
        errors.append("{0}: expected type {1}".format(path, expected_type))
        return errors

    if isinstance(instance, dict) and expected_type == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in instance:
                errors.append("{0}.{1}: required property is missing".format(path, key))
        if schema.get("additionalProperties") is False:
            for key in set(instance) - set(properties):
                errors.append("{0}.{1}: additional property is forbidden".format(path, key))
        for key, value in instance.items():
            if key in properties:
                errors.extend(
                    schema_subset_errors(value, properties[key], root_schema, "{0}.{1}".format(path, key))
                )

    if isinstance(instance, list) and expected_type == "array":
        if len(instance) < schema.get("minItems", 0):
            errors.append("{0}: array is shorter than minItems".format(path))
        if len(instance) > schema.get("maxItems", float("inf")):
            errors.append("{0}: array is longer than maxItems".format(path))
        if schema.get("uniqueItems"):
            encoded = [
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for item in instance
            ]
            if len(encoded) != len(set(encoded)):
                errors.append("{0}: array items are not unique".format(path))
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, value in enumerate(instance):
                errors.extend(
                    schema_subset_errors(
                        value,
                        item_schema,
                        root_schema,
                        "{0}[{1}]".format(path, index),
                    )
                )

    if isinstance(instance, str) and expected_type == "string":
        if len(instance) < schema.get("minLength", 0):
            errors.append("{0}: string is shorter than minLength".format(path))
        if len(instance) > schema.get("maxLength", float("inf")):
            errors.append("{0}: string is longer than maxLength".format(path))
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, instance) is None:
            errors.append("{0}: string does not match pattern".format(path))
        if schema.get("format") == "date-time":
            try:
                parse_utc(instance)
            except (AssertionError, ValueError):
                errors.append("{0}: invalid date-time".format(path))
        if schema.get("format") == "uri":
            parsed = urlsplit(instance)
            if not parsed.scheme or not parsed.netloc:
                errors.append("{0}: invalid URI".format(path))

    if expected_type in {"integer", "number"} and type_matches[expected_type]:
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append("{0}: number is below minimum".format(path))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append("{0}: number is above maximum".format(path))
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append("{0}: number is not above exclusiveMinimum".format(path))

    return errors


class SchemaShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest_schema = load_json(SCHEMA_DIR / "manifest.v1.schema.json")
        cls.snapshot_schema = load_json(SCHEMA_DIR / "snapshot.v1.schema.json")

    def test_schemas_use_draft_2020_12_and_close_every_object(self) -> None:
        for schema in (self.manifest_schema, self.snapshot_schema):
            self.assertEqual(
                schema["$schema"],
                "https://json-schema.org/draft/2020-12/schema",
            )
            for path, node in walk(schema):
                if isinstance(node, dict) and node.get("type") == "object":
                    self.assertIs(
                        node.get("additionalProperties"),
                        False,
                        "object is not closed at {0}".format(path),
                    )
                    self.assertIn("properties", node, path)
                    self.assertTrue(
                        set(node.get("required", ())).issubset(node["properties"]),
                        path,
                    )

    def test_all_local_references_resolve_and_patterns_compile(self) -> None:
        for schema in (self.manifest_schema, self.snapshot_schema):
            definitions = schema.get("$defs", {})
            for path, node in walk(schema):
                if not isinstance(node, dict):
                    continue
                reference = node.get("$ref")
                if reference is not None:
                    self.assertTrue(reference.startswith("#/$defs/"), path)
                    self.assertIn(reference.rsplit("/", 1)[-1], definitions, path)
                pattern = node.get("pattern")
                if pattern is not None:
                    re.compile(pattern)

    def test_manifest_schema_pins_the_content_addressed_publisher_key(self) -> None:
        self.assertEqual(
            set(self.manifest_schema["required"]),
            {"manifestVersion", "publishedAt", "snapshot"},
        )
        self.assertEqual(
            self.manifest_schema["properties"]["manifestVersion"]["const"], 1
        )
        pointer = self.manifest_schema["properties"]["snapshot"]
        self.assertEqual(pointer["properties"]["schemaVersion"]["const"], 1)
        self.assertEqual(pointer["properties"]["path"]["minLength"], 114)
        self.assertEqual(pointer["properties"]["path"]["maxLength"], 114)
        self.assertIn("[a-f0-9]{64}", pointer["properties"]["path"]["pattern"])
        self.assertEqual(
            self.manifest_schema["$defs"]["snapshotId"]["pattern"],
            "^mr-[0-9]{8}t[0-9]{6}z$",
        )

    def test_snapshot_schema_exposes_only_the_v1_public_sections(self) -> None:
        self.assertEqual(set(self.snapshot_schema["required"]), SNAPSHOT_TOP_LEVEL_KEYS)
        self.assertEqual(
            self.snapshot_schema["properties"]["schemaVersion"]["const"], 1
        )
        definitions = self.snapshot_schema["$defs"]
        self.assertIn("macroConditions", definitions)
        self.assertNotIn("marketRegime", definitions)
        self.assertEqual(
            definitions["macroConditions"]["properties"]["scoreScale"]
            ["properties"]["higherMeans"]["const"],
            "More restrictive macro-financial conditions",
        )
        self.assertEqual(
            definitions["officialSource"]["properties"]["sourceType"]["const"],
            "official",
        )
        indicator_required = set(definitions["indicator"]["required"])
        self.assertTrue(
            {
                "rawValue",
                "displayValue",
                "observedAt",
                "retrievedAt",
                "freshness",
                "source",
            }.issubset(indicator_required)
        )

    def test_every_array_and_public_text_field_is_bounded(self) -> None:
        for path, node in walk(self.snapshot_schema):
            if not isinstance(node, dict):
                continue
            if node.get("type") == "array":
                self.assertIn("maxItems", node, path)
            if node.get("type") == "string" and "enum" not in node:
                has_bound = "maxLength" in node or "pattern" in node
                self.assertTrue(has_bound, "unbounded string at {0}".format(path))


class CanonicalExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_json(EXAMPLE_DIR / "manifest.v1.json")
        cls.snapshot = load_json(EXAMPLE_DIR / "snapshot.v1.json")
        cls.snapshot_bytes = canonical_json_bytes(cls.snapshot)

    def test_examples_satisfy_their_schemas_without_external_dependencies(self) -> None:
        manifest_schema = load_json(SCHEMA_DIR / "manifest.v1.schema.json")
        snapshot_schema = load_json(SCHEMA_DIR / "snapshot.v1.schema.json")
        self.assertEqual(
            schema_subset_errors(self.manifest, manifest_schema, manifest_schema), []
        )
        self.assertEqual(
            schema_subset_errors(self.snapshot, snapshot_schema, snapshot_schema), []
        )

    def test_manifest_is_relative_content_addressed_and_self_consistent(self) -> None:
        assert_exact_keys(
            self,
            self.manifest,
            {"manifestVersion", "publishedAt", "snapshot"},
        )
        pointer = self.manifest["snapshot"]
        assert_exact_keys(
            self,
            pointer,
            {
                "schemaVersion",
                "id",
                "path",
                "generatedAt",
                "validUntil",
                "sizeBytes",
                "sha256",
            },
        )
        self.assertEqual(self.manifest["manifestVersion"], 1)
        self.assertEqual(pointer["schemaVersion"], 1)
        self.assertEqual(pointer["id"], self.snapshot["id"])
        self.assertEqual(pointer["generatedAt"], self.snapshot["generatedAt"])
        self.assertEqual(pointer["validUntil"], self.snapshot["validUntil"])

        path = PurePosixPath(pointer["path"])
        self.assertFalse(path.is_absolute())
        self.assertNotIn("..", path.parts)
        match = SNAPSHOT_PATH_RE.fullmatch(pointer["path"])
        self.assertIsNotNone(match)
        assert match is not None

        digest = hashlib.sha256(self.snapshot_bytes).hexdigest()
        self.assertEqual(pointer["sha256"], digest)
        self.assertEqual(pointer["sizeBytes"], len(self.snapshot_bytes))
        self.assertEqual(match.group("sha256"), digest)

        generated_at = parse_utc(pointer["generatedAt"])
        expected_timestamp = generated_at.strftime("%Y-%m-%dT%H-%M-%SZ")
        expected_id = generated_at.strftime("mr-%Y%m%dt%H%M%Sz")
        self.assertEqual(match.group("timestamp"), expected_timestamp)
        self.assertEqual(pointer["id"], expected_id)
        self.assertEqual(match.group("year"), generated_at.strftime("%Y"))
        self.assertEqual(match.group("month"), generated_at.strftime("%m"))
        self.assertEqual(match.group("day"), generated_at.strftime("%d"))
        self.assertLessEqual(generated_at, parse_utc(self.manifest["publishedAt"]))

    def test_snapshot_top_level_and_pipeline_coverage_are_truthful(self) -> None:
        self.assertEqual(set(self.snapshot), SNAPSHOT_TOP_LEVEL_KEYS)
        self.assertEqual(self.snapshot["schemaVersion"], 1)
        generated_at = parse_utc(self.snapshot["generatedAt"])
        self.assertLess(generated_at, parse_utc(self.snapshot["validUntil"]))

        pipeline = self.snapshot["pipeline"]
        assert_exact_keys(
            self,
            pipeline,
            {"runId", "status", "startedAt", "completedAt", "coverage"},
            {"publicNote"},
        )
        self.assertLessEqual(parse_utc(pipeline["startedAt"]), parse_utc(pipeline["completedAt"]))
        self.assertLessEqual(parse_utc(pipeline["completedAt"]), generated_at)
        coverage = pipeline["coverage"]
        assert_exact_keys(
            self,
            coverage,
            {
                "expectedSources",
                "successfulSources",
                "staleSources",
                "failedSources",
                "marketTags",
            },
        )
        accounted_for = (
            coverage["successfulSources"]
            + coverage["staleSources"]
            + coverage["failedSources"]
        )
        self.assertEqual(coverage["expectedSources"], accounted_for)
        self.assertEqual(coverage["expectedSources"], len(self.snapshot["sources"]))
        self.assertEqual(pipeline["status"], "healthy")
        self.assertEqual(coverage["staleSources"], 0)
        self.assertEqual(coverage["failedSources"], 0)

    def test_macro_score_is_transparent_and_derived_from_published_inputs(self) -> None:
        macro = self.snapshot["macroConditions"]
        assert_exact_keys(
            self,
            macro,
            {"score", "label", "summary", "scoreScale", "methodology", "drivers"},
        )
        self.assertEqual(macro["scoreScale"]["minimum"], 0)
        self.assertEqual(macro["scoreScale"]["maximum"], 100)
        self.assertEqual(macro["label"], "restrictive")

        methodology = macro["methodology"]
        baseline = methodology["baselineScore"]
        contributions = sum(driver["contributionPoints"] for driver in macro["drivers"])
        self.assertAlmostEqual(macro["score"], baseline + contributions)
        self.assertAlmostEqual(sum(driver["weight"] for driver in macro["drivers"]), 1.0)

        indicator_ids = {item["id"] for item in self.snapshot["indicators"]}
        driver_ids = {driver["indicatorId"] for driver in macro["drivers"]}
        self.assertEqual(driver_ids, indicator_ids)

    def test_v1_indicator_scope_has_no_equity_or_proxy_substitutes(self) -> None:
        indicator_ids = {item["id"] for item in self.snapshot["indicators"]}
        self.assertEqual(indicator_ids, V1_INDICATOR_IDS)
        serialized = json.dumps(self.snapshot, ensure_ascii=False).lower()
        for forbidden in ("vix", "dxy", "equity", "equities", "cds"):
            self.assertNotIn(forbidden, serialized)

    def test_indicators_have_values_freshness_and_official_provenance(self) -> None:
        generated_at = parse_utc(self.snapshot["generatedAt"])
        source_ids = {source["id"] for source in self.snapshot["sources"]}
        for indicator in self.snapshot["indicators"]:
            assert_exact_keys(
                self,
                indicator,
                {
                    "id",
                    "label",
                    "rawValue",
                    "displayValue",
                    "unit",
                    "change",
                    "observedAt",
                    "retrievedAt",
                    "freshness",
                    "macroSignal",
                    "source",
                    "marketTags",
                },
            )
            self.assertIsInstance(indicator["rawValue"], (int, float))
            self.assertTrue(indicator["displayValue"])
            observed_at = parse_utc(indicator["observedAt"])
            retrieved_at = parse_utc(indicator["retrievedAt"])
            self.assertLessEqual(observed_at, retrieved_at)
            self.assertLessEqual(retrieved_at, generated_at)

            freshness = indicator["freshness"]
            expected_age = int((generated_at - observed_at).total_seconds())
            self.assertEqual(freshness["ageSeconds"], expected_age)
            expected_status = (
                "fresh"
                if freshness["ageSeconds"] <= freshness["maxAgeSeconds"]
                else "stale"
            )
            self.assertEqual(freshness["status"], expected_status)

            provenance = indicator["source"]
            self.assertIn(provenance["sourceId"], source_ids)
            self.assertEqual(provenance["retrievedAt"], indicator["retrievedAt"])
            assert_https_url(self, provenance["sourceUrl"])

    def test_developments_stories_and_digest_retain_provenance(self) -> None:
        generated_at = parse_utc(self.snapshot["generatedAt"])
        source_ids = {source["id"] for source in self.snapshot["sources"]}
        story_ids = {story["id"] for story in self.snapshot["stories"]}

        for development in self.snapshot["priorityDevelopments"]:
            self.assertLessEqual(parse_utc(development["firstSeenAt"]), parse_utc(development["updatedAt"]))
            self.assertLessEqual(parse_utc(development["updatedAt"]), generated_at)
            self.assertTrue(development["marketTags"])
            for provenance in development["provenance"]:
                self.assertIn(provenance["sourceId"], source_ids)
                assert_https_url(self, provenance["sourceUrl"])

        for story in self.snapshot["stories"]:
            self.assertLessEqual(parse_utc(story["publishedAt"]), parse_utc(story["retrievedAt"]))
            self.assertLessEqual(parse_utc(story["retrievedAt"]), generated_at)
            assert_https_url(self, story["url"])
            for provenance in story["provenance"]:
                self.assertIn(provenance["sourceId"], source_ids)
                assert_https_url(self, provenance["sourceUrl"])

        digest = self.snapshot["digest"]
        self.assertLessEqual(parse_utc(digest["periodStart"]), parse_utc(digest["periodEnd"]))
        self.assertEqual(parse_utc(digest["periodEnd"]), parse_utc(digest["generatedAt"]))
        self.assertEqual(set(digest["storyIds"]), story_ids)
        self.assertEqual(digest["itemCount"], len(digest["storyIds"]))
        for highlight in digest["highlights"]:
            self.assertTrue(set(highlight["relatedStoryIds"]).issubset(story_ids))

    def test_calendar_is_official_and_uses_unambiguous_instants(self) -> None:
        source_ids = {source["id"] for source in self.snapshot["sources"]}
        for event in self.snapshot["calendar"]:
            parse_utc(event["scheduledAt"])
            self.assertRegex(event["countryCode"], r"^[A-Z]{2}$")
            self.assertIn("/", event["timezone"])
            self.assertEqual(event["source"]["sourceType"], "official")
            self.assertIn(event["source"]["sourceId"], source_ids)
            assert_https_url(self, event["source"]["sourceUrl"])

    def test_source_health_is_bounded_and_sanitized(self) -> None:
        allowed_keys = {
            "id",
            "name",
            "type",
            "url",
            "status",
            "lastAttemptAt",
            "lastSuccessAt",
            "itemsUsed",
            "marketTags",
            "publicMessage",
        }
        source_ids = set()
        for source in self.snapshot["sources"]:
            self.assertEqual(set(source) - allowed_keys, set())
            self.assertNotIn(source["id"], source_ids)
            source_ids.add(source["id"])
            assert_https_url(self, source["url"])
            parse_utc(source["lastAttemptAt"])
            if source["lastSuccessAt"] is not None:
                parse_utc(source["lastSuccessAt"])
            for key in source:
                lowered = key.lower()
                for forbidden in ("exception", "traceback", "secret", "token", "credential"):
                    self.assertNotIn(forbidden, lowered)

    def test_all_public_strings_are_html_free_and_tags_are_bounded(self) -> None:
        for path, value in walk(self.snapshot):
            if isinstance(value, str):
                self.assertNotIn("<", value, path)
                self.assertNotIn(">", value, path)
            if path.endswith(".marketTags"):
                self.assertIsInstance(value, list)
                self.assertGreaterEqual(len(value), 1)
                self.assertLessEqual(len(value), 12)
                self.assertEqual(len(value), len(set(value)))
                for tag in value:
                    self.assertRegex(tag, SLUG_RE)


if __name__ == "__main__":
    unittest.main()
